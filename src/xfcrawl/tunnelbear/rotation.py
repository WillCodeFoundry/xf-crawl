"""ServerRotation: a TunnelBear account as the crawler's proxy route.

It uses every proxy server TunnelBear has, in every country, in a shuffled order: one server for
``REQUESTS_PER_SERVER`` requests, then the next, and the next at once when one fails. The VPN token is
renewed every ``renew_every`` seconds and the server list every ``relist_every``. Anything that goes wrong
getting a token or the server list becomes ProxyDown, so the crawl stops rather than going direct.
"""

import logging
import random
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import requests

from xfcrawl.proxy import Proxies, ProxyDown, refused_credentials
from xfcrawl.tunnelbear.account import Endpoints, Server, TunnelBearError

REQUESTS_PER_SERVER = 8000
"""How many requests go through one server before moving on to the next. Change it here."""

_RENEW_EVERY_SECONDS = 300.0
_RELIST_EVERY_SECONDS = 6 * 3600.0
"""Listing every server takes a request per country, so it is done now and then, not with each token."""
_MOST_SERVERS_TRIED = 10
"""How many servers a request tries before the route is taken to be down."""
_LOGGER = logging.getLogger(__name__)


def proxy_url(host: str, token: str, port: int) -> str:
    quoted = urllib.parse.quote(token, safe="")
    return f"https://{quoted}:{quoted}@{host}:{port}"


class ServerSource(Protocol):
    """What the rotation needs of a TunnelBear account."""

    @property
    def endpoints(self) -> Endpoints: ...

    def vpn_token(self) -> str: ...

    def all_servers(self) -> list[Server]: ...


@dataclass(frozen=True)
class Turns:
    """How long a server, a token and a server list each last."""

    requests_per_server: int = REQUESTS_PER_SERVER
    renew_every: float = _RENEW_EVERY_SECONDS
    """Seconds."""
    relist_every: float = _RELIST_EVERY_SECONDS
    """Seconds."""


_DEFAULT_TURNS = Turns()


class ServerRotation:
    def __init__(
        self,
        account: ServerSource,
        turns: Turns = _DEFAULT_TURNS,
        *,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        """Gets a token and the server list now; raises ProxyDown if it can't."""
        self._account = account
        self._turns = turns
        self._clock = clock
        self._rng = rng or random.Random()
        self.servers: list[Server] = []
        self.token = ""
        self._index = 0
        self._sent = 0
        self._renewed_at = self._listed_at = 0.0
        self.renew()
        self.relist()

    def renew(self) -> None:
        """A fresh VPN token. Raises ProxyDown when TunnelBear can't give one."""
        try:
            self.token = self._account.vpn_token()
        except (TunnelBearError, requests.RequestException) as error:
            raise ProxyDown(f"could not renew the TunnelBear token: {error}") from error
        self._renewed_at = self._clock()

    def relist(self) -> None:
        """Every server TunnelBear has, shuffled, staying on the current one if it is still listed. Raises
        ProxyDown when TunnelBear can't list them."""
        current = self.host if self.servers else None
        try:
            servers = self._account.all_servers()
        except (TunnelBearError, requests.RequestException) as error:
            raise ProxyDown(f"could not list the TunnelBear servers: {error}") from error
        self._rng.shuffle(servers)
        self.servers = servers
        hosts = [server.host for server in servers]
        self._index = hosts.index(current) if current in hosts else 0
        self._listed_at = self._clock()

    @property
    def hosts(self) -> list[str]:
        return [server.host for server in self.servers]

    @property
    def host(self) -> str:
        return self.servers[self._index].host

    @property
    def port(self) -> int:
        return self._account.endpoints.proxy_port

    # -- ProxyRoute ---------------------------------------------------------------------------------
    @property
    def max_failures(self) -> int:
        # each server tried may fail once for a stale token, once for real
        return 2 * min(len(self.servers), _MOST_SERVERS_TRIED) + 1

    def take_turn(self) -> None:
        """Count one more request through the current server, renewing the token or the server list when
        due, and moving to the next server once this one has had its share. Raises ProxyDown."""
        if self._clock() - self._renewed_at > self._turns.renew_every:
            self.renew()
        if self._clock() - self._listed_at > self._turns.relist_every:
            self.relist()
        if self._sent >= self._turns.requests_per_server:
            self._move_on()
            _LOGGER.info("After %s requests, moving to TunnelBear server %s", f"{self._turns.requests_per_server:,}",
                         self._where())
        self._sent += 1

    def proxies(self) -> Proxies:
        self.take_turn()
        url = proxy_url(self.host, self.token, self.port)
        return {"http": url, "https": url}

    def failed(self, *, auth_problem: bool) -> None:
        if auth_problem:
            self.renew()
        self._move_on()

    def next_server(self) -> str | None:
        self._move_on()
        return self._where()

    def describe(self) -> str:
        countries = len({server.country for server in self.servers})
        return f"TunnelBear, {len(self.servers)} servers in {countries} countries, using {self._where()}"

    def _move_on(self) -> None:
        self._index = (self._index + 1) % len(self.servers)
        self._sent = 0

    def _where(self) -> str:
        server = self.servers[self._index]
        return f"{server.host} ({server.country})"


def fetch_through(rotation: ServerRotation, session: requests.Session, url: str) -> requests.Response:
    """One GET through TunnelBear, moving on until a server answers; a refused token is renewed first."""
    last_error: object = None
    for _ in range(rotation.max_failures):
        try:
            response = session.get(url, proxies=rotation.proxies(), timeout=60)
        except (requests.exceptions.ProxyError, requests.exceptions.SSLError,
                requests.exceptions.ConnectTimeout) as error:
            last_error = error
            rotation.failed(auth_problem=refused_credentials(error))
            continue
        if response.status_code != 407:  # a plain-http target gets the proxy's 407 as a response
            return response
        last_error = "407 Proxy Authentication Required"
        rotation.failed(auth_problem=True)
    raise TunnelBearError(f"no TunnelBear server would take the request; last error: {last_error}")

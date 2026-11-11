"""ServerRotation: a TunnelBear account as the crawler's proxy route.

It keeps one of the country's proxy servers in use, renews the VPN token every ``renew_every`` seconds,
and moves to the next server when told the current one failed. Anything that goes wrong getting a token or
the server list becomes ProxyDown, so the crawl stops rather than going direct.
"""

import time
import urllib.parse
from collections.abc import Callable

import requests

from xfcrawl.proxy import Proxies, ProxyDown, refused_credentials
from xfcrawl.tunnelbear.account import TunnelBearAccount, TunnelBearError

_RENEW_EVERY_SECONDS = 300.0


def proxy_url(host: str, token: str, port: int) -> str:
    quoted = urllib.parse.quote(token, safe="")
    return f"https://{quoted}:{quoted}@{host}:{port}"


class ServerRotation:
    def __init__(
        self,
        account: TunnelBearAccount,
        country: str | None,
        *,
        renew_every: float = _RENEW_EVERY_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Gets a token and the server list now; raises ProxyDown if it can't."""
        self._account = account
        self._country = country
        self._renew_every = renew_every
        self._clock = clock
        self.hosts: list[str] = []
        self.token = ""
        self._index = 0
        self._renewed_at = 0.0
        self.renew()

    def renew(self) -> None:
        """A fresh token and server list, staying on the current server if it is still listed. Raises
        ProxyDown when TunnelBear can't give them."""
        current = self.host if self.hosts else None
        try:
            self.token = self._account.vpn_token()
            self.hosts = self._account.servers(self._country)
        except (TunnelBearError, requests.RequestException) as error:
            raise ProxyDown(f"could not renew the TunnelBear token: {error}") from error
        self._index = self.hosts.index(current) if current in self.hosts else 0
        self._renewed_at = self._clock()

    @property
    def host(self) -> str:
        return self.hosts[self._index]

    @property
    def port(self) -> int:
        return self._account.endpoints.proxy_port

    # -- ProxyRoute ---------------------------------------------------------------------------------
    @property
    def max_failures(self) -> int:
        return 2 * len(self.hosts) + 1  # each server may fail once for a stale token, once for real

    def proxies(self) -> Proxies:
        if self._clock() - self._renewed_at > self._renew_every:
            self.renew()
        url = proxy_url(self.host, self.token, self.port)
        return {"http": url, "https": url}

    def failed(self, *, auth_problem: bool) -> None:
        if auth_problem:
            self.renew()
        self._index = (self._index + 1) % len(self.hosts)

    def describe(self) -> str:
        where = self._country.upper() if self._country else "closest region"
        return f"TunnelBear {where}, {len(self.hosts)} servers, using {self.host}"


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

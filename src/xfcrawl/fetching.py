"""Fetching pages politely: the only module that sends the crawler's requests.

``PoliteFetcher`` waits between requests, retries what may pass (429 and 5xx, timeouts, dropped
connections), and goes through a proxy when given one. With a proxy, it fails closed: a proxy failure moves
to the proxy's next server and is never blamed on the page, and when the proxy can't be used at all it
raises ProxyDown. Nothing is ever sent without the proxy.
"""

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import requests

from xfcrawl.proxy import ProxyDown, ProxyRoute, refused_credentials

USER_AGENT = "xf-crawl (+https://github.com/WillCodeFoundry/xf-crawl)"
DEFAULT_DELAY = 1.0
DEFAULT_JITTER = 0.5
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_TRIES = 5
_TIMEOUT_SECONDS = 30
_LONGEST_RETRY_WAIT = 300.0

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Response:
    status: int
    url: str
    """The URL that answered, after redirects."""
    text: str


class FetchFailed(Exception):
    """The page could not be fetched; ``reason`` is ``http-<status>``, ``timeout`` or ``network``."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason} {detail}".strip())
        self.reason = reason
        self.detail = detail


class Fetcher(Protocol):
    def get(self, url: str) -> Response:
        """The site's answer; raises FetchFailed or ProxyDown."""
        ...


class Pacing:
    """How requests are spaced out: ``delay`` plus up to ``jitter`` seconds between one and the next.
    ``pause`` and ``clock`` are the seams for tests that shouldn't wait."""

    def __init__(
        self,
        delay: float,
        jitter: float,
        pause: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.delay = delay
        self._jitter = jitter
        self.pause = pause
        self._clock = clock
        self._rng = random.Random()
        self._last_request = float("-inf")

    def slow_down_to(self, delay: float) -> None:
        """Wait at least ``delay`` seconds between requests from now on, as robots.txt may ask."""
        self.delay = max(self.delay, delay)

    def wait_turn(self) -> None:
        wait = self._last_request + self.delay + self._rng.uniform(0, self._jitter) - self._clock()
        if wait > 0:
            self.pause(wait)

    def request_done(self) -> None:
        self._last_request = self._clock()


class PoliteFetcher:
    def __init__(self, session: requests.Session, pacing: Pacing, route: ProxyRoute | None) -> None:
        self._session = session
        self.pacing = pacing
        self._route = route

    def get(self, url: str) -> Response:
        retry_wait = max(self.pacing.delay, 1.0) * 2
        tries = proxy_failures = 0
        while True:
            tries += 1
            self.pacing.wait_turn()
            route = self._route
            proxies = None if route is None else _checked(route.proxies())
            try:
                response = self._session.get(url, timeout=_TIMEOUT_SECONDS, proxies=proxies)
            except (requests.exceptions.ProxyError, requests.exceptions.SSLError) as error:
                self.pacing.request_done()
                if route is None:
                    raise FetchFailed("network", str(error)) from error
                proxy_failures += 1
                _switch(route, error, proxy_failures, auth_problem=refused_credentials(error))
                tries -= 1  # not the page's fault
                continue
            except requests.RequestException as error:
                self.pacing.request_done()
                failure = FetchFailed("timeout" if isinstance(error, requests.Timeout) else "network", str(error))
            else:
                self.pacing.request_done()
                if response.status_code == 407 and route is not None:
                    proxy_failures += 1
                    _switch(route, "407 Proxy Authentication Required", proxy_failures, auth_problem=True)
                    tries -= 1
                    continue
                if response.status_code not in _RETRY_STATUSES:
                    return Response(response.status_code, response.url, response.text)
                failure = FetchFailed(f"http-{response.status_code}")
                retry_after = response.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    retry_wait = max(retry_wait, float(retry_after))
            if tries == _TRIES:
                raise failure
            _LOGGER.info("    ! %s; retry %d/%d in %.0fs", failure, tries, _TRIES - 1, retry_wait)
            self.pacing.pause(retry_wait)
            retry_wait = min(retry_wait * 2, _LONGEST_RETRY_WAIT)



def _checked(proxies: dict[str, str]) -> dict[str, str]:
    """Never let a request go out with proxies missing: requests would send it directly."""
    if not proxies.get("http") or not proxies.get("https"):
        raise ProxyDown("proxy settings are empty")
    return proxies


def _switch(route: ProxyRoute, error: object, failures: int, *, auth_problem: bool) -> None:
    if failures >= route.max_failures:
        raise ProxyDown(f"{route.describe()}: {error}")
    route.failed(auth_problem=auth_problem)
    _LOGGER.info("    ! proxy problem (%s); now %s", type(error).__name__, route.describe())

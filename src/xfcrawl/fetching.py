"""Fetching pages politely: the only module that sends the crawler's requests.

``PoliteFetcher`` waits between requests and goes through a proxy when given one. It asks for a page once:
whatever the site answers is returned, and what to do about a failure is the crawl's to decide (trouble.py).
Two things are asked again at once, as neither is the page's doing: a proxy failure, on the proxy's next
server, and a site's cookie check (cookie_check.py). With a proxy, it fails closed: when the proxy can't be
used at all it raises ProxyDown. Nothing is ever sent without the proxy.

Whenever the proxy's server changes, for whatever reason (its turn is up, it failed, the crawl asked for
another), the fetcher starts a new visitor before the next request: a site that saw a visitor's cookies from
one address shouldn't see them from another.
"""

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import requests

from xfcrawl.browser import followed_from
from xfcrawl.cookie_check import asked_for
from xfcrawl.proxy import ProxyDown, ProxyRoute, refused_credentials

DEFAULT_DELAY = 1.0
DEFAULT_JITTER = 0.5
_TIMEOUT_SECONDS = 30
BREAK_CHANCE = 0.01
"""How often a gap between requests is a break, as when someone stops to read."""
BREAK_LENGTH = (5.0, 20.0)
"""How long a break lasts, in delays: 5 to 20 seconds with the usual one-second delay."""

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Response:
    status: int
    url: str
    """The URL that answered, after redirects."""
    text: str
    retry_after: float | None = None
    """Seconds the site asked to wait before asking again (Retry-After), if it did."""


class FetchFailed(Exception):
    """No answer came: ``reason`` is ``timeout`` or ``network``."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason} {detail}".strip())
        self.reason = reason
        self.detail = detail


class Fetcher(Protocol):
    def get(self, url: str, referer: str | None = None) -> Response:
        """The site's answer, asked for as if following a link on ``referer``; raises FetchFailed or
        ProxyDown."""
        ...


def average_gap(delay: float, jitter: float) -> float:
    """The seconds between requests on average, breaks included."""
    return delay + jitter / 2 + BREAK_CHANCE * delay * sum(BREAK_LENGTH) / 2


class Pacing:
    """How requests are spaced out: ``delay`` plus up to ``jitter`` seconds between one and the next, and now
    and then a break of several delays. ``pause``, ``clock`` and ``rng`` are the seams for tests."""

    def __init__(
        self,
        delay: float,
        jitter: float,
        pause: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self.delay = delay
        self._jitter = jitter
        self.pause = pause
        self._clock = clock
        self._rng = rng or random.Random()
        self._last_request = float("-inf")

    def slow_down_to(self, delay: float) -> None:
        """Wait at least ``delay`` seconds between requests from now on, as robots.txt may ask."""
        self.delay = max(self.delay, delay)

    def wait_turn(self) -> None:
        gap = self.delay + self._rng.uniform(0, self._jitter)
        if self._rng.random() < BREAK_CHANCE:
            gap += self.delay * self._rng.uniform(*BREAK_LENGTH)
        wait = self._last_request + gap - self._clock()
        if wait > 0:
            self.pause(wait)

    def request_done(self) -> None:
        self._last_request = self._clock()


class PoliteFetcher:
    def __init__(
        self,
        session: requests.Session,
        pacing: Pacing,
        route: ProxyRoute | None,
        new_visitor: Callable[[], str] | None = None,
    ) -> None:
        """``new_visitor`` changes the session's browser and drops its cookies, saying which browser."""
        self._session = session
        self.pacing = pacing
        self._route = route
        self._new_visitor = new_visitor
        self._server: str | None = None

    def get(self, url: str, referer: str | None = None) -> Response:
        response = self._get(url, referer)
        if response.status != 200 and self._set_asked_cookie(response):
            response = self._get(url, referer)  # what a browser does without anyone noticing
        return response

    def _get(self, url: str, referer: str | None) -> Response:
        headers = followed_from(referer)
        proxy_failures = 0
        while True:
            self.pacing.wait_turn()
            route = self._route
            proxies = None if route is None else _checked(route.proxies())
            if route is not None:
                self._same_visitor_only_on_the_same_server(route.server)
            try:
                response = self._session.get(url, headers=headers, timeout=_TIMEOUT_SECONDS, proxies=proxies)
            except (requests.exceptions.ProxyError, requests.exceptions.SSLError) as error:
                self.pacing.request_done()
                if route is None:
                    raise FetchFailed("network", str(error)) from error
                proxy_failures += 1
                _switch(route, error, proxy_failures, auth_problem=refused_credentials(error))
                continue  # not the page's fault
            except requests.RequestException as error:
                self.pacing.request_done()
                self._server = None if route is None else route.server  # it may have reached the site
                reason = "timeout" if isinstance(error, requests.Timeout) else "network"
                raise FetchFailed(reason, str(error)) from error
            self.pacing.request_done()
            if response.status_code == 407 and route is not None:
                proxy_failures += 1
                _switch(route, "407 Proxy Authentication Required", proxy_failures, auth_problem=True)
                continue  # the proxy refused it: the site never saw it
            self._server = None if route is None else route.server  # the site has seen us from here
            return Response(response.status_code, response.url, response.text, _retry_after(response))

    def _same_visitor_only_on_the_same_server(self, server: str) -> None:
        """A new visitor if the site would see this one from another server than it last did. A server that
        failed before reaching the site (a proxy error) doesn't count: the site never saw it."""
        if self._server is not None and server != self._server and self._new_visitor is not None:
            _LOGGER.info("    now through %s: new visitor (%s)", server, self._new_visitor())
            self._server = server

    def _set_asked_cookie(self, response: Response) -> bool:
        """Whether ``response`` is a page checking for a browser by asking for a cookie; if so, the cookie
        is set, so that asking again gets the page."""
        cookie = asked_for(response.text, response.url)
        if cookie is None:
            return False
        self._session.cookies.set(cookie.name, cookie.value, domain=cookie.domain, path=cookie.path)
        _LOGGER.info("    the site asked for cookie %s (HTTP %d); set it, asking again", cookie.name, response.status)
        return True


def _retry_after(response: requests.Response) -> float | None:
    value = response.headers.get("Retry-After", "")
    return float(value) if value.isdigit() else None


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

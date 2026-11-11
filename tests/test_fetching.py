import pytest
import requests

from tests.support.mock_forum import MockForum
from xfcrawl.fetching import FetchFailed, Pacing, PoliteFetcher
from xfcrawl.proxy import FixedProxy, Proxies, ProxyDown


class Waits:
    """A pause that records instead of sleeping, and a clock that only it moves."""

    def __init__(self) -> None:
        self.paused: list[float] = []
        self.now = 0.0

    def pause(self, seconds: float) -> None:
        self.paused.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now


class BrokenRoute:
    """A proxy route that can't give settings, or gives empty ones."""

    max_failures = 3

    def __init__(self, proxies: Proxies | None) -> None:
        self._proxies = proxies

    def proxies(self) -> Proxies:
        if self._proxies is None:
            raise ProxyDown("could not get proxy settings: TunnelBear is down")
        return self._proxies

    def failed(self, *, auth_problem: bool) -> None:
        raise AssertionError("never reached: nothing was sent")

    def describe(self) -> str:
        return "a broken route"


def fetcher(waits: Waits, route=None, delay: float = 0.0) -> PoliteFetcher:
    return PoliteFetcher(requests.Session(), Pacing(delay, 0.0, waits.pause, waits.clock), route)


def test_requests_are_spaced_out(forum: MockForum):
    waits = Waits()
    polite = fetcher(waits, delay=1.5)
    polite.get(forum.url)
    polite.get(forum.url)
    assert waits.paused == [1.5]


def test_a_busy_site_is_retried_with_its_retry_after(forum: MockForum):
    forum.busy_once.add(11)
    waits = Waits()
    response = fetcher(waits).get(forum.url + "threads/thread-11.11/")
    assert response.status == 200
    assert waits.paused == [2.0]                      # the default first wait; Retry-After: 0 is shorter


def test_a_page_that_stays_down_fails_after_five_tries(forum: MockForum):
    forum.stop()
    waits = Waits()
    with pytest.raises(FetchFailed) as raised:
        fetcher(waits).get(forum.url)
    assert raised.value.reason == "network"
    assert waits.paused == [2.0, 4.0, 8.0, 16.0]


def test_a_redirect_reports_where_it_went(forum: MockForum):
    response = fetcher(Waits()).get(forum.url + "threads/thread-12.12/page-9")
    assert response.url.endswith("/threads/thread-12.12/page-2")


@pytest.mark.parametrize("route", [BrokenRoute(None), BrokenRoute({}), BrokenRoute({"http": "http://p:1"})])
def test_without_usable_proxy_settings_nothing_is_sent(forum: MockForum, route: BrokenRoute):
    with pytest.raises(ProxyDown):
        fetcher(Waits(), route).get(forum.url)
    assert forum.hits == []


def test_an_unreachable_fixed_proxy_is_proxy_down_not_a_page_failure(forum: MockForum):
    with pytest.raises(ProxyDown):
        fetcher(Waits(), FixedProxy("http://127.0.0.1:9")).get(forum.url)
    assert forum.hits == []

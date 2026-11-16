import random

import pytest
import requests

from tests.support.mock_forum import MockForum
from xfcrawl.fetching import FetchFailed, Pacing, PoliteFetcher, average_gap
from xfcrawl.proxy import FixedProxy, Proxies, ProxyDown, refused_credentials


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


class NoBreaks(random.Random):
    def random(self) -> float:
        return 0.5


class AlwaysBreaks(random.Random):
    def random(self) -> float:
        return 0.0


def fetcher(waits: Waits, route=None, delay: float = 0.0) -> PoliteFetcher:
    return PoliteFetcher(requests.Session(), Pacing(delay, 0.0, waits.pause, waits.clock, NoBreaks()), route)


def test_requests_are_spaced_out(forum: MockForum):
    waits = Waits()
    polite = fetcher(waits, delay=1.5)
    polite.get(forum.url)
    polite.get(forum.url)
    assert waits.paused == [1.5]


def test_now_and_then_a_gap_is_a_break_of_several_delays(forum: MockForum):
    waits = Waits()
    polite = PoliteFetcher(requests.Session(), Pacing(2.0, 0.0, waits.pause, waits.clock, AlwaysBreaks()), None)
    polite.get(forum.url)
    polite.get(forum.url)
    assert 2.0 + 5 * 2.0 <= waits.paused[-1] <= 2.0 + 20 * 2.0


def test_breaks_are_rare_and_add_little_on_average():
    pacing = Pacing(1.0, 0.0, rng=random.Random(3))
    gaps: list[float] = []
    for _ in range(5000):
        pacing.pause = gaps.append
        pacing._last_request = pacing._clock()
        pacing.wait_turn()
    breaks = [gap for gap in gaps if gap > 2]
    assert 20 < len(breaks) < 100
    assert average_gap(1.0, 0.0) == pytest.approx(1.125)


def test_a_page_followed_from_another_says_so(forum: MockForum):
    polite = fetcher(Waits())
    polite.get(forum.url)
    polite.get(forum.url + "forums/general.2/", referer=forum.url)
    typed, followed = forum.hits
    assert "Referer" not in typed.headers
    assert followed.headers["Referer"] == forum.url
    assert followed.headers["Sec-Fetch-Site"] == "same-origin"


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


@pytest.mark.parametrize(("message", "refused"), [
    ("Tunnel connection failed: 407 Proxy Authentication Required", True),
    ("407 Proxy Authentication Required", True),
    ("hostname mismatch, certificate is not valid for '127.0.0.1' (127.0.0.1:40701)", False),
    ("HTTPSConnectionPool(host='localhost', port=14079): Max retries exceeded", False),
])
def test_only_a_407_status_counts_as_refused_credentials(message, refused):
    assert refused_credentials(requests.exceptions.ProxyError(message)) is refused

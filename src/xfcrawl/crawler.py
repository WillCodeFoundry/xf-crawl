"""The crawl loop: fetch what the plan says, store what arrives, record what fails.

A page is stored only if it is the page asked for and a whole, real XenForo page. Everything else is
recorded as a failure of that page, and nothing of it is stored. Progress goes to this module's logger.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from xfcrawl import xenforo
from xfcrawl.failures import Failure
from xfcrawl.fetching import Fetcher, FetchFailed, PoliteFetcher, Response
from xfcrawl.plan import CrawlPlan
from xfcrawl.proxy import ProxyDown
from xfcrawl.robots import RobotsRules
from xfcrawl.store import PageStore
from xfcrawl.urls import Kind, PageRef, parse_page_url, slug_of

_LOGGER = logging.getLogger(__name__)
_FAILURES_IN_A_ROW_TO_STOP = 5
_REPORT_EVERY_SECONDS = 30.0


@dataclass(frozen=True)
class Store:
    """Store the response: it is the page asked for, whole."""

    first_page_url: str | None
    """Where the site serves the forum or thread now (None for the index): its current URL and slug."""


@dataclass(frozen=True)
class Reject:
    """Record a failure instead; ``message`` is what the log says."""

    reason: str
    detail: str
    message: str
    redirect_target: str | None = None


def judge(ref: PageRef, response: Response) -> Store | Reject:
    """What to do with the site's answer to a request for ``ref``."""
    if response.status != 200:
        return Reject(f"http-{response.status}", "", f"http-{response.status}")
    link = parse_page_url(response.url)
    if ref.kind is not Kind.INDEX and link is not None and link.ref != ref:
        # a page past the end, or a moved or merged thread: the page it went to is wanted instead
        return Reject("redirect", response.url, f"redirected to {response.url}", redirect_target=response.url)
    page_template = xenforo.template(response.text)
    if page_template in xenforo.ERROR_TEMPLATES or not xenforo.is_complete(response.text):
        return Reject("error-page", f"template={page_template}",
                      f"got an error or login page (template={page_template})")
    return Store(link.first_page_url if link is not None and ref.kind is not Kind.INDEX else None)


@dataclass
class CrawlSummary:
    saved: int = 0
    errors: int = 0
    stopped: str | None = None
    """Why the run stopped early, if it did."""
    waiting: dict[PageRef, datetime] = field(default_factory=dict)
    to_fetch: int = 0


@dataclass(frozen=True)
class Connection:
    fetcher: Fetcher
    robots: RobotsRules


def connect(fetcher: PoliteFetcher, start_url: str, *, obey_robots: bool) -> Connection:
    """Ready to crawl: the site's robots.txt read (through the fetcher, so through any proxy), and the
    fetcher slowed to its Crawl-delay if it asks for one."""
    robots = RobotsRules.fetch(fetcher, start_url) if obey_robots else RobotsRules.allow_all()
    delay = robots.crawl_delay()
    if delay is not None:
        _LOGGER.info("robots.txt asks for a Crawl-delay of %ss", delay)
        fetcher.pacing.slow_down_to(delay)
    return Connection(fetcher, robots)


@dataclass(frozen=True)
class Crawl:
    """One run of the loop, with everything it uses. ``connect`` is called only once there is something
    to fetch, so a run with nothing missing never contacts the site, or the proxy."""

    plan: CrawlPlan
    store: PageStore
    connect: Callable[[], Connection]
    now: Callable[[], datetime]
    clock: Callable[[], float]

    def run(self, *, max_fetches: int | None) -> CrawlSummary:
        summary = CrawlSummary(to_fetch=self.plan.to_fetch_count(), waiting=self.plan.waiting())
        if summary.to_fetch == 0:
            return summary
        try:
            connection = self.connect()
        except ProxyDown as error:
            summary.stopped = f"proxy not working: {error}"
            return summary
        self._loop(connection, summary, max_fetches)
        summary.waiting = self.plan.waiting()
        summary.to_fetch = self.plan.to_fetch_count()
        return summary

    def _loop(self, connection: Connection, summary: CrawlSummary, max_fetches: int | None) -> None:
        fetches = in_a_row = 0
        last_report = self.clock()
        stage = None
        while (ref := self.plan.next_page()) is not None:
            if max_fetches is not None and fetches >= max_fetches:
                self.plan.put_back(ref)
                summary.stopped = f"reached --max-fetches {max_fetches}"
                break
            if ref.stage is not stage:
                stage = ref.stage
                _LOGGER.info("Now fetching %s: %d pages to fetch", stage.describe(), self.plan.to_fetch_count() + 1)
            url = self.plan.url_of(ref)
            if not connection.robots.allows(url):
                self._fail(ref, url, Reject("robots", "", "blocked by robots.txt"))
                continue
            _LOGGER.info("GET %-20s %s", ref.key, url)
            try:
                response = connection.fetcher.get(url)
            except ProxyDown as error:
                self.plan.put_back(ref)
                summary.stopped = f"proxy not working (nothing recorded against {ref.key}): {error}"
                break
            except FetchFailed as error:
                fetches += 1
                summary.errors += 1
                in_a_row += 1
                self._fail(ref, url, Reject(error.reason, error.detail, f"{error} (after retries)"))
                if in_a_row >= _FAILURES_IN_A_ROW_TO_STOP:
                    summary.stopped = f"{in_a_row} failures in a row; is the site down?"
                    break
                continue
            fetches += 1
            verdict = judge(ref, response)
            if isinstance(verdict, Reject):
                summary.errors += 1
                self._fail(ref, url, verdict)
                continue
            in_a_row = 0
            self._save(ref, url, response, verdict)
            summary.saved += 1
            if self.clock() - last_report > _REPORT_EVERY_SECONDS:
                _LOGGER.info("    %s: %d left in this stage, %d known to fetch in all", ref.stage.describe(),
                             self.plan.to_fetch_in(ref.stage), self.plan.to_fetch_count())
                last_report = self.clock()

    def _save(self, ref: PageRef, url: str, response: Response, verdict: Store) -> None:
        if verdict.first_page_url is not None:
            self.plan.seen_at(ref.item, verdict.first_page_url)
        self.store.save(ref, response.text, slug_of(verdict.first_page_url) if verdict.first_page_url else None)
        added = self.plan.discover(ref, response.text, url)
        if added:
            _LOGGER.info("    %d to fetch (+%d new)", self.plan.to_fetch_count(), added)

    def _fail(self, ref: PageRef, url: str, reject: Reject) -> None:
        if reject.redirect_target is not None:
            self.plan.note_link(reject.redirect_target)
        failure = Failure(self.now(), reject.reason, url, reject.detail)
        history = self.store.record_failure(ref, failure, self.plan.slug(ref.item))
        next_try = self.plan.retry_policy.next_try(history)
        _LOGGER.info("    ! %s; not stored; attempt %d, next try after %s",
                     reject.message, len(history), next_try.astimezone().strftime("%Y-%m-%d %H:%M"))

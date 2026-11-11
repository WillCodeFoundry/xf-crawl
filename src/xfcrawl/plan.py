"""CrawlPlan: which pages are still to fetch, worked out from the pages stored.

There is no queue on disk. Each run rebuilds the plan from the store:

* the index and every stored forum page: every link to a forum or thread page on the same site is wanted;
* page 1 of every stored thread: its page navigation says how many pages the thread has
  (``1 2 3 ... 48`` means pages 1 to 48 are wanted);
* for any forum or thread, if page N is known, pages 1 to N are wanted.

A wanted page that is neither stored nor waiting out its retry back-off is to fetch. Fetching a page can
reveal more (``discover``), so the plan grows as the crawl goes.
"""

import heapq
from datetime import datetime
from urllib.parse import urljoin, urlparse

from xfcrawl import xenforo
from xfcrawl.failures import RetryPolicy
from xfcrawl.store import PageStore
from xfcrawl.urls import (
    INDEX_PAGE,
    ItemRef,
    Kind,
    PageLink,
    PageRef,
    bare_first_page_url,
    page_url,
    parse_page_url,
    site_prefix,
    slug_of,
)


class CrawlPlan:
    def __init__(
        self, start_url: str, store: PageStore, retry: RetryPolicy, now: datetime, *, ignore_backoff: bool = False
    ) -> None:
        self._start_url = start_url
        self._host = urlparse(start_url).netloc
        self._store = store
        self._retry = retry
        self._now = now
        self._ignore_backoff = ignore_backoff
        self._first_page_urls: dict[ItemRef, str] = {}
        self._last_pages: dict[ItemRef, int] = {}
        self._queue: list[tuple[tuple[int, int, int], PageRef]] = []
        self._queued: set[PageRef] = set()
        self._waiting: dict[PageRef, datetime] = {}
        self._site_prefix: str | None = None

    # -- building it --------------------------------------------------------------------------------
    def rebuild(self) -> int:
        """Work out the plan from the store; returns how many stored pages it read to do so."""
        self._want(INDEX_PAGE)
        for ref in self._store.stored_pages():
            if ref.kind is not Kind.INDEX:
                self._know(ref.item, ref.page, None)
        to_read = sorted(
            (ref for ref in self._store.stored_pages() if ref.kind is not Kind.THREAD or ref.page == 1),
            key=PageRef.crawl_order,
        )
        for ref in to_read:
            self.discover(ref, self._store.read(ref), self.url_of(ref))
        for failures in self._store.failing_pages().values():
            last = failures[-1]
            if last.reason == "redirect" and last.detail:
                self.note_link(last.detail)  # what a redirected page pointed to
        return len(to_read)

    def discover(self, ref: PageRef, page_html: str, url: str) -> int:
        """Add what a page links to; returns how many pages that added to fetch.

        Everything on the index and forum pages counts. On a thread, only page 1's links to the
        thread's own pages do: its page navigation.
        """
        if ref.kind is Kind.THREAD and ref.page != 1:
            return 0
        added = 0
        for href in xenforo.links(page_html):
            link = self._on_this_site(urljoin(url, href))
            if link is None or (ref.kind is Kind.THREAD and link.ref.item != ref.item):
                continue
            added += self._know(link.ref.item, link.ref.page, link.first_page_url)
        return added

    def note_link(self, url: str) -> int:
        link = self._on_this_site(url)
        return 0 if link is None else self._know(link.ref.item, link.ref.page, link.first_page_url)

    def seen_at(self, item: ItemRef, first_page_url: str) -> None:
        """The site just served this item from ``first_page_url``: use that URL from now on."""
        self._first_page_urls[item] = first_page_url

    # -- reading it ---------------------------------------------------------------------------------
    @property
    def retry_policy(self) -> RetryPolicy:
        return self._retry

    def next_page(self) -> PageRef | None:
        if not self._queue:
            return None
        _, ref = heapq.heappop(self._queue)
        return ref

    def put_back(self, ref: PageRef) -> None:
        """Return a page taken with next_page but not attempted."""
        self._queued.discard(ref)
        self._want(ref)

    def to_fetch(self) -> list[PageRef]:
        return [ref for _, ref in sorted(self._queue)]

    def to_fetch_count(self) -> int:
        return len(self._queue)

    def waiting(self) -> dict[PageRef, datetime]:
        """Pages that failed and wait out their back-off, with when each may be tried again."""
        return dict(self._waiting)

    def known_items(self) -> dict[ItemRef, int]:
        """Every forum and thread known, with its highest page known."""
        return dict(self._last_pages)

    def url_of(self, ref: PageRef) -> str:
        if ref.kind is Kind.INDEX:
            return self._start_url
        first = self._first_page_urls.get(ref.item) or bare_first_page_url(
            self._site_prefix or self._start_url, ref.item
        )
        return page_url(first, ref.page)

    def slug(self, item: ItemRef) -> str | None:
        first = self._first_page_urls.get(item)
        return slug_of(first) if first else None

    # -- internals ----------------------------------------------------------------------------------
    def _on_this_site(self, url: str) -> PageLink | None:
        link = parse_page_url(url)
        if link is None or urlparse(link.first_page_url).netloc != self._host:
            return None
        return link

    def _know(self, item: ItemRef, page: int, first_page_url: str | None) -> int:
        if first_page_url and item not in self._first_page_urls:
            self._first_page_urls[item] = first_page_url
            self._site_prefix = self._site_prefix or site_prefix(first_page_url)
        last = self._last_pages.get(item, 0)
        if page <= last:
            return 0
        self._last_pages[item] = page
        return sum(self._want(item.page(p)) for p in range(last + 1, page + 1))

    def _want(self, ref: PageRef) -> int:
        if ref in self._queued or self._store.is_stored(ref):
            return 0
        failures = self._store.failures(ref)
        if failures and not self._ignore_backoff:
            next_try = self._retry.next_try(failures)
            if self._now < next_try:
                self._waiting[ref] = next_try
                return 0
        heapq.heappush(self._queue, (ref.crawl_order(), ref))
        self._queued.add(ref)
        return 1

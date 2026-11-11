"""CrawlPlan: which pages are still to fetch, worked out from the pages stored.

There is no queue on disk. Each run rebuilds the plan from the store:

* the index and every stored forum page: every link to a forum or thread page on the same site is wanted;
* page 1 of every stored thread: its page navigation says how many pages the thread has
  (``1 2 3 ... 48`` means pages 1 to 48 are wanted);
* for any forum or thread, if page N is known, pages 1 to N are wanted.

A wanted page that is neither stored nor waiting out its retry back-off is to fetch. Fetching a page can
reveal more (``discover``), so the plan grows as the crawl goes.

What each page links to is kept in the LinkIndex as the page is stored, so a rebuild reads the index rather
than the pages, and reads a page only when the index has nothing for it.

With ``only_forums``, the plan keeps to those forums: only their thread lists, and only the threads they list.
Other forums and threads are left out, even when already stored: they stay on disk, outside the plan.
"""

import heapq
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime
from urllib.parse import urljoin, urlparse, urlsplit

from xfcrawl import xenforo
from xfcrawl.failures import Backoff, RetryPolicy
from xfcrawl.link_index import ItemLink, LinkIndex, PageLinks
from xfcrawl.store import PageStore
from xfcrawl.urls import (
    INDEX_PAGE,
    ItemRef,
    Kind,
    PageLink,
    PageRef,
    Stage,
    bare_first_page_url,
    page_url,
    parse_page_url,
    site_prefix,
    slug_of,
)


@dataclass(frozen=True)
class Rebuilt:
    from_index: int
    """Stored pages whose links came from the link index."""
    from_pages: int
    """Stored pages read because the index had nothing for them."""


class CrawlPlan:
    def __init__(
        self,
        start_url: str,
        store: PageStore,
        links: LinkIndex,
        backoff: Backoff,
        only_forums: frozenset[int] | None = None,
    ) -> None:
        self._start_url = start_url
        site = urlparse(start_url)
        self._host = site.netloc
        self._origin = f"{site.scheme}://{site.netloc}"
        self._store = store
        self._links = links
        self._only_forums = only_forums
        self._backoff = backoff
        self._first_page_urls: dict[ItemRef, str] = {}
        self._last_pages: dict[ItemRef, int] = {}
        self._queue: list[tuple[tuple[int, int, int], PageRef]] = []
        self._queued: set[PageRef] = set()
        self._waiting: dict[PageRef, datetime] = {}
        self._site_prefix: str | None = None
        self._queued_by_stage: Counter[Stage] = Counter()
        self._listed_in: dict[ItemRef, ItemRef] = {}
        self._titles: dict[ItemRef, str] = {}

    # -- building it --------------------------------------------------------------------------------
    def rebuild(self) -> Rebuilt:
        """Work out the plan from the store and the link index."""
        self._want(INDEX_PAGE)
        for forum_id in sorted(self._only_forums or ()):  # wanted even if only an unlisted forum links them
            self._know(ItemRef(Kind.FORUM, forum_id), 1, None)
        indexed = self._links.load()
        to_learn_from = sorted(
            (ref for ref in self._store.stored_pages() if ref.kind is not Kind.THREAD or ref.page == 1),
            key=PageRef.crawl_order,
        )
        missing: list[PageLinks] = []
        for ref in to_learn_from:
            entry = indexed.get(ref)
            if entry is None:
                entry = self.links_on(ref, self._store.read(ref), self.url_of(ref))
                missing.append(entry)
            self._learn(entry)
        self._links.record_all(missing)
        for ref in self._store.stored_pages():  # a stored page past what any link says is known too
            if ref.kind is not Kind.INDEX and self._in_scope(ref.item, ref):
                self._know(ref.item, ref.page, None)
        for failures in self._store.failing_pages().values():
            last = failures[-1]
            if last.reason == "redirect" and last.detail:
                self.note_link(last.detail)  # what a redirected page pointed to
        return Rebuilt(len(to_learn_from) - len(missing), len(missing))

    def discover(self, ref: PageRef, page_html: str, url: str) -> int:
        """Add what a stored page links to, and record it in the link index; returns how many pages that
        added to fetch. A thread's later pages add nothing: its first page's navigation already did."""
        if ref.kind is Kind.THREAD and ref.page != 1:
            return 0
        entry = self.links_on(ref, page_html, url)
        self._links.record(entry)
        return self._learn(entry)

    def links_on(self, ref: PageRef, page_html: str, url: str) -> PageLinks:
        """What counts on a page: everything on the index and forum pages; on a thread's first page, only
        its links to its own pages (its page navigation). Grouped by forum or thread."""
        grouped: dict[ItemRef, ItemLink] = {}
        for href in xenforo.links(page_html):
            link = self._on_this_site(urljoin(url, href))
            if link is None or (ref.kind is Kind.THREAD and link.ref.item != ref.item):
                continue
            known = grouped.get(link.ref.item)
            if known is None:
                grouped[link.ref.item] = ItemLink(link.ref.item, link.ref.page, _path(link.first_page_url))
            elif link.ref.page > known.last_page:
                grouped[link.ref.item] = replace(known, last_page=link.ref.page)
        title = xenforo.title(page_html) if ref.kind is Kind.FORUM and ref.page == 1 else None
        return PageLinks(ref, title, tuple(grouped.values()))

    def note_link(self, url: str) -> int:
        """A page some page pointed to, such as where it redirected."""
        link = self._on_this_site(url)
        if link is None or not self._in_scope(link.ref.item, link.ref):
            return 0
        return self._know(link.ref.item, link.ref.page, link.first_page_url)

    def seen_at(self, item: ItemRef, first_page_url: str) -> None:
        """The site just served this item from ``first_page_url``: use that URL from now on."""
        self._first_page_urls[item] = first_page_url

    # -- reading it ---------------------------------------------------------------------------------
    @property
    def retry_policy(self) -> RetryPolicy:
        return self._backoff.policy

    def next_page(self) -> PageRef | None:
        if not self._queue:
            return None
        _, ref = heapq.heappop(self._queue)
        self._queued_by_stage[ref.stage] -= 1
        return ref

    def put_back(self, ref: PageRef) -> None:
        """Return a page taken with next_page but not attempted."""
        self._queued.discard(ref)
        self._want(ref)

    def to_fetch(self) -> list[PageRef]:
        return [ref for _, ref in sorted(self._queue)]

    def to_fetch_count(self) -> int:
        return len(self._queue)

    def to_fetch_in(self, stage: Stage) -> int:
        return self._queued_by_stage[stage]

    def listed_in(self, thread: ItemRef) -> ItemRef | None:
        """The forum whose thread list first showed the thread."""
        return self._listed_in.get(thread)

    def title(self, forum: ItemRef) -> str | None:
        return self._titles.get(forum)

    def is_stored(self, ref: PageRef) -> bool:
        return self._store.is_stored(ref)

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
            self._site_prefix or _with_trailing_slash(self._start_url), ref.item
        )
        return page_url(first, ref.page)

    def slug(self, item: ItemRef) -> str | None:
        first = self._first_page_urls.get(item)
        return slug_of(first) if first else None

    # -- internals ----------------------------------------------------------------------------------
    def _learn(self, entry: PageLinks) -> int:
        if entry.title:
            self._titles[entry.page.item] = entry.title
        added = 0
        for link in entry.links:
            if not self._in_scope(link.item, entry.page):
                continue
            if entry.page.kind is Kind.FORUM and link.item.kind is Kind.THREAD:
                self._listed_in.setdefault(link.item, entry.page.item)
            added += self._know(link.item, link.last_page, self._origin + link.path)
        return added

    def _in_scope(self, item: ItemRef, seen_on: PageRef) -> bool:
        """Whether ``item``, linked from ``seen_on``, belongs in the plan: with only_forums, a forum on the
        list, or a thread one of those forums lists."""
        if self._only_forums is None:
            return True
        if item.kind is Kind.FORUM:
            return item.item_id in self._only_forums
        if item in self._last_pages:
            return True  # already listed by an allowed forum
        return seen_on.kind is Kind.FORUM and seen_on.item_id in self._only_forums

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
        waiting_until = self._backoff.waiting_until(self._store.failures(ref))
        if waiting_until is not None:
            self._waiting[ref] = waiting_until
            return 0
        heapq.heappush(self._queue, (ref.crawl_order(), ref))
        self._queued.add(ref)
        self._queued_by_stage[ref.stage] += 1
        return 1


def _with_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"


def _path(url: str) -> str:
    """A URL without its scheme and host: the path, and the query (which holds the route when XenForo's
    friendly URLs are off)."""
    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")

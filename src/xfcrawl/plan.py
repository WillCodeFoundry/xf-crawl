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
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import datetime
from itertools import groupby
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
        self._first_page_paths: dict[ItemRef, str] = {}  # paths, not URLs: one per thread, so size counts
        self._last_pages: dict[ItemRef, int] = {}
        # Runs of consecutive pages of one forum or thread, all to fetch: (stage, number, first page, last
        # page). Ordered as the pages are. A run per thread rather than an entry per page keeps the plan's
        # size to the number of threads, not pages.
        self._queue: list[tuple[Stage, int, int, int]] = []
        self._waiting: dict[PageRef, datetime] = {}
        self._site_prefix: str | None = None
        self._queued_by_stage: Counter[Stage] = Counter()
        self._listed_in: dict[int, int] = {}  # thread number -> forum number
        self._titles: dict[ItemRef, str] = {}

    # -- building it --------------------------------------------------------------------------------
    def rebuild(self) -> Rebuilt:
        """Work out the plan from the store and the link index.

        In two passes over the index, which is read as a stream, never held: first the index's and forums'
        pages, which say which threads are in scope; then the threads' first pages. In each, a stored page the
        index has nothing for is read, and added to it."""
        self._want_pages(INDEX_PAGE.item, 1, 1)
        for forum_id in sorted(self._only_forums or ()):  # wanted even if only an unlisted forum links them
            self._know(ItemRef(Kind.FORUM, forum_id), 1, None)
        from_index = from_pages = 0
        for kinds in _LEARNING_PASSES:
            indexed = self._learn_from_index(kinds)
            from_index += sum(pages.bit_count() for pages in indexed.values())
            from_pages += self._learn_from_pages(kinds, indexed)
        for ref in self._store.stored_pages():  # a stored page past what any link says is known too
            if ref.kind is not Kind.INDEX and self._in_scope(ref.item, ref):
                self._know(ref.item, ref.page, None)
        for failures in self._store.failing_pages().values():
            last = failures[-1]
            if last.reason == "redirect" and last.detail:
                self.note_link(last.detail)  # what a redirected page pointed to
        return Rebuilt(from_index, from_pages)

    def _learn_from_index(self, kinds: frozenset[Kind]) -> dict[ItemRef, int]:
        """Learn from the index's entries for stored pages of these kinds; returns which pages it had, as a
        bitmask per forum or thread (bit n: page n)."""
        indexed: dict[ItemRef, int] = {}
        for entry in self._links.entries():
            ref = entry.page
            if ref.kind in kinds and self._store.is_stored(ref):
                indexed[ref.item] = indexed.get(ref.item, 0) | 1 << ref.page
                self._learn(entry)
        return indexed

    def _learn_from_pages(self, kinds: frozenset[Kind], indexed: dict[ItemRef, int]) -> int:
        """Read the stored pages of these kinds the index has nothing for, learn from them, and add them to
        the index; returns how many. Recorded in batches, so a first run on a big store stays small too."""
        batch: list[PageLinks] = []
        read = 0
        for ref in self._store.stored_pages():
            if ref.kind not in kinds or (ref.kind is Kind.THREAD and ref.page != 1):
                continue
            if indexed.get(ref.item, 0) >> ref.page & 1:
                continue
            entry = self.links_on(ref, self._store.read(ref), self.url_of(ref))
            self._learn(entry)
            batch.append(entry)
            read += 1
            if len(batch) == _RECORD_BATCH:
                self._links.record_all(batch)
                batch = []
        self._links.record_all(batch)
        return read

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
        self._first_page_paths[item] = _path(first_page_url)

    # -- reading it ---------------------------------------------------------------------------------
    @property
    def retry_policy(self) -> RetryPolicy:
        return self._backoff.policy

    def next_page(self) -> PageRef | None:
        if not self._queue:
            return None
        stage, item_id, first, last = heapq.heappop(self._queue)
        if first < last:
            heapq.heappush(self._queue, (stage, item_id, first + 1, last))
        self._queued_by_stage[stage] -= 1
        return PageRef(stage.kind, item_id, first)

    def put_back(self, ref: PageRef) -> None:
        """Return a page taken with next_page but not attempted."""
        self._push(ref.stage, ref.item_id, ref.page, ref.page)

    def to_fetch(self) -> Iterator[PageRef]:
        """The pages to fetch, in the order they will be."""
        for stage, item_id, first, last in sorted(self._queue):
            for page in range(first, last + 1):
                yield PageRef(stage.kind, item_id, page)

    def to_fetch_count(self) -> int:
        return sum(self._queued_by_stage.values())

    def to_fetch_in(self, stage: Stage) -> int:
        return self._queued_by_stage[stage]

    def listed_in(self, thread: ItemRef) -> ItemRef | None:
        """The forum whose thread list first showed the thread."""
        forum_id = self._listed_in.get(thread.item_id) if thread.kind is Kind.THREAD else None
        return None if forum_id is None else ItemRef(Kind.FORUM, forum_id)

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
        return page_url(self._first_page_url(ref.item), ref.page)

    def slug(self, item: ItemRef) -> str | None:
        path = self._first_page_paths.get(item)
        return slug_of(self._origin + path) if path else None

    # -- internals ----------------------------------------------------------------------------------
    def _learn(self, entry: PageLinks) -> int:
        if entry.title:
            self._titles[entry.page.item] = entry.title
        added = 0
        for link in entry.links:
            if not self._in_scope(link.item, entry.page):
                continue
            if entry.page.kind is Kind.FORUM and link.item.kind is Kind.THREAD:
                self._listed_in.setdefault(link.item.item_id, entry.page.item_id)
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

    def _first_page_url(self, item: ItemRef) -> str:
        path = self._first_page_paths.get(item)
        if path is not None:
            return self._origin + path
        return bare_first_page_url(self._site_prefix or _with_trailing_slash(self._start_url), item)

    def _know(self, item: ItemRef, page: int, first_page_url: str | None) -> int:
        if first_page_url and item not in self._first_page_paths:
            self._first_page_paths[item] = _path(first_page_url)
            self._site_prefix = self._site_prefix or site_prefix(first_page_url)
        last = self._last_pages.get(item, 0)
        if page <= last:
            return 0
        self._last_pages[item] = page
        return self._want_pages(item, last + 1, page)

    def _want_pages(self, item: ItemRef, first: int, last: int) -> int:
        """Queue those of the item's pages first to last that are to fetch now, as runs; returns how many.
        Each page is considered once: an item's pages are only ever added above its last known page."""
        wanted = [page for page in range(first, last + 1) if self._to_fetch_now(item.page(page))]
        consecutive = groupby(enumerate(wanted), key=lambda entry: (entry[1] - entry[0], item.page(entry[1]).stage))
        for (_, stage), run in consecutive:
            pages = [page for _, page in run]
            self._push(stage, item.item_id, pages[0], pages[-1])
        return len(wanted)

    def _to_fetch_now(self, ref: PageRef) -> bool:
        if self._store.is_stored(ref):
            return False
        waiting_until = self._backoff.waiting_until(self._store.failures(ref))
        if waiting_until is not None:
            self._waiting[ref] = waiting_until
            return False
        return True

    def _push(self, stage: Stage, item_id: int, first: int, last: int) -> None:
        heapq.heappush(self._queue, (stage, item_id, first, last))
        self._queued_by_stage[stage] += last - first + 1


_LEARNING_PASSES = (frozenset({Kind.INDEX, Kind.FORUM}), frozenset({Kind.THREAD}))
_RECORD_BATCH = 5000


def _with_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"


def _path(url: str) -> str:
    """A URL without its scheme and host: the path, and the query (which holds the route when XenForo's
    friendly URLs are off)."""
    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")

"""Statistics about a crawl: forums, threads, posts, and how complete the download is."""

from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urljoin

from xfcrawl.mining import pages
from xfcrawl.plan import CrawlPlan
from xfcrawl.store import PageStore
from xfcrawl.urls import ItemRef, Kind, parse_page_url


@dataclass
class ForumStats:
    forum_id: int
    title: str
    pages_stored: int
    pages_total: int
    threads_listed: int
    threads_downloaded: int = 0


@dataclass(frozen=True)
class IncompleteThread:
    thread_id: int
    pages_stored: int
    pages_total: int


@dataclass
class Stats:
    forum: str
    forums: int = 0
    threads_known: int = 0
    threads_listed: int = 0
    threads_downloaded: int = 0
    threads_complete: int = 0
    threads_incomplete: int = 0
    thread_pages_stored: int = 0
    posts_stored: int = 0
    replies_per_listings: int = 0
    replies_approx: bool = False
    pages_stored: int = 0
    pages_to_crawl: int = 0
    pages_failed: dict[str, int] = field(default_factory=dict)
    pages_failed_waiting_retry: int = 0
    per_forum: list[ForumStats] = field(default_factory=list)
    incomplete_threads: list[IncompleteThread] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def compute(start_url: str, store: PageStore, plan: CrawlPlan) -> Stats:
    """``plan`` must be rebuilt from ``store``: it says which forums and threads exist, and how long."""
    stats = Stats(forum=start_url)
    forums, listed = _forums(start_url, store, plan)
    _add_threads(stats, store, plan, listed, forums)
    stats.forums = len(forums)
    stats.threads_listed = len(listed)
    stats.replies_per_listings = sum(t.replies.value for t in listed.values() if t.replies)
    stats.replies_approx = any(t.replies.approximate for t in listed.values() if t.replies)
    stats.pages_stored = store.stored_count()
    stats.pages_to_crawl = plan.to_fetch_count()
    stats.pages_failed = dict(sorted(Counter(f[-1].reason for f in store.failing_pages().values()).items()))
    stats.pages_failed_waiting_retry = len(plan.waiting())
    stats.per_forum = sorted(forums.values(), key=lambda forum: -forum.threads_listed)
    return stats


@dataclass(frozen=True)
class _Listed:
    forum_id: int
    replies: pages.Count | None


def _forums(start_url: str, store: PageStore, plan: CrawlPlan) -> tuple[dict[int, ForumStats], dict[int, _Listed]]:
    """Each forum, from its stored listing pages; and each thread listed, with the first forum listing it."""
    forums: dict[int, ForumStats] = {}
    listed: dict[int, _Listed] = {}
    for item, last_page in sorted(plan.known_items().items(), key=lambda entry: entry[0].item_id):
        if item.kind is not Kind.FORUM:
            continue
        stored = [item.page(p) for p in range(1, last_page + 1) if store.is_stored(item.page(p))]
        title = None
        threads_here: set[int] = set()
        for ref in stored:
            page = pages.parse(store.read(ref))
            if ref.page == 1:
                title = pages.title(page)
            for thread in pages.listed_threads(page):
                link = parse_page_url(urljoin(start_url, thread.href))
                if link is None or link.ref.kind is not Kind.THREAD or link.ref.item_id in threads_here:
                    continue
                threads_here.add(link.ref.item_id)
                listed.setdefault(link.ref.item_id, _Listed(item.item_id, thread.replies))
        forums[item.item_id] = ForumStats(
            forum_id=item.item_id, title=title or f"forum {item.item_id}", pages_stored=len(stored),
            pages_total=last_page, threads_listed=len(threads_here),
        )
    return forums, listed


def _add_threads(
    stats: Stats, store: PageStore, plan: CrawlPlan, listed: dict[int, _Listed], forums: dict[int, ForumStats]
) -> None:
    threads = sorted(
        ((item, last) for item, last in plan.known_items().items() if item.kind is Kind.THREAD),
        key=lambda entry: entry[0].item_id,
    )
    stats.threads_known = len(threads)
    for item, last_page in threads:
        stored = [item.page(p) for p in range(1, last_page + 1) if store.is_stored(item.page(p))]
        if not stored:
            continue
        expected = _pages_expected(store, item, last_page)
        stats.threads_downloaded += 1
        stats.thread_pages_stored += len(stored)
        stats.posts_stored += sum(pages.post_count(pages.parse(store.read(ref))) for ref in stored)
        if len(stored) == expected:
            stats.threads_complete += 1
        else:
            stats.incomplete_threads.append(IncompleteThread(item.item_id, len(stored), expected))
        forum = listed.get(item.item_id)
        if forum is not None and forum.forum_id in forums:
            forums[forum.forum_id].threads_downloaded += 1
    stats.threads_incomplete = len(stats.incomplete_threads)


def _pages_expected(store: PageStore, item: ItemRef, last_page: int) -> int:
    """Pages 1 to last_page, except those that 404'd or redirected elsewhere (such as stale links past the
    end): those don't exist."""
    expected = 0
    for page in range(1, last_page + 1):
        failures = store.failures(item.page(page))
        if store.is_stored(item.page(page)) or not failures or not failures[-1].is_permanent:
            expected += 1
    return expected

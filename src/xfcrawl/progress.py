"""How far a crawl has got: by stage, by forum, and how long the rest may take.

Worked out from the store and the rebuilt plan alone, so it is quick however big the crawl: no page is
parsed. Counts grow as the crawl goes: a forum's thread list tells of its threads, and a thread's first page
of its other pages, so the last stage's size is only known once the stage before it is done.
"""

from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from xfcrawl.plan import CrawlPlan
from xfcrawl.store import PageStore
from xfcrawl.urls import ItemRef, Kind, PageRef, Stage


@dataclass(frozen=True)
class StageProgress:
    stage: Stage
    known: int
    """Pages of this stage known so far."""
    stored: int
    failing: int
    """Pages that failed and aren't stored."""
    to_fetch: int
    """Pages the next run would fetch now."""

    @property
    def done(self) -> bool:
        return self.to_fetch == 0 and self.stored + self.failing >= self.known


@dataclass
class ForumProgress:
    forum_id: int
    title: str
    list_pages_known: int
    list_pages_stored: int
    threads: int = 0
    first_pages_stored: int = 0
    thread_pages_known: int = 0
    thread_pages_stored: int = 0


@dataclass(frozen=True)
class Estimate:
    pages_left: int
    """Known pages not yet stored, plus the threads' later pages still to be discovered, estimated from the
    threads whose first page is stored."""
    seconds_per_page: float

    @property
    def seconds(self) -> float:
        return self.pages_left * self.seconds_per_page


@dataclass
class Progress:
    start_url: str
    stages: list[StageProgress]
    forums: list[ForumProgress] = field(default_factory=list)
    threads_known: int = 0
    longest_thread: int = 0
    estimate: Estimate | None = None

    @property
    def current(self) -> StageProgress | None:
        """The first stage that isn't done; None when the whole crawl is."""
        return next((stage for stage in self.stages if not stage.done), None)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["stages"] = [{**asdict(s), "stage": s.stage.name.lower(), "done": s.done} for s in self.stages]
        current = self.current
        data["current_stage"] = current.stage.name.lower() if current else None
        return data


def compute(start_url: str, store: PageStore, plan: CrawlPlan, seconds_per_page: float) -> Progress:
    """``plan`` must be rebuilt from ``store``."""
    known_items = plan.known_items()
    stored_by_item = Counter(ref.item for ref in store.stored_pages())
    progress = Progress(start_url, _stages(store, plan, known_items))
    progress.forums = _forums(plan, known_items, stored_by_item)
    threads = {item: last for item, last in known_items.items() if item.kind is Kind.THREAD}
    progress.threads_known = len(threads)
    progress.longest_thread = max(threads.values(), default=0)
    progress.estimate = Estimate(_pages_left(progress.stages, store, threads), seconds_per_page)
    return progress


def _stages(store: PageStore, plan: CrawlPlan, known_items: dict[ItemRef, int]) -> list[StageProgress]:
    known: Counter[Stage] = Counter({Stage.FORUM_LIST: 1})
    for item, last_page in known_items.items():
        if item.kind is Kind.FORUM:
            known[Stage.THREAD_LISTS] += last_page
        elif item.kind is Kind.THREAD:
            known[Stage.THREAD_FIRST_PAGES] += 1
            known[Stage.THREAD_OTHER_PAGES] += last_page - 1
    stored = Counter(ref.stage for ref in store.stored_pages())
    failing = Counter(ref.stage for ref in store.failing_pages())
    return [StageProgress(stage, known[stage], stored[stage], failing[stage], plan.to_fetch_in(stage))
            for stage in Stage]


def _forums(plan: CrawlPlan, known_items: dict[ItemRef, int], stored_by_item: Counter[ItemRef]) -> list[ForumProgress]:
    forums = {
        item: ForumProgress(item.item_id, plan.title(item) or f"forum {item.item_id}", last, stored_by_item[item])
        for item, last in known_items.items() if item.kind is Kind.FORUM
    }
    for item, last_page in known_items.items():
        listed_in = plan.listed_in(item) if item.kind is Kind.THREAD else None
        forum = forums.get(listed_in) if listed_in is not None else None
        if forum is None:
            continue
        forum.threads += 1
        forum.first_pages_stored += plan.is_stored(item.page(1))
        forum.thread_pages_known += last_page
        forum.thread_pages_stored += stored_by_item[item]
    return sorted(forums.values(), key=lambda forum: (-forum.threads, forum.forum_id))


def _pages_left(stages: list[StageProgress], store: PageStore, threads: dict[ItemRef, int]) -> int:
    left = sum(max(stage.known - stage.stored - stage.failing, 0) for stage in stages)
    opened = [last for item, last in threads.items() if store.is_stored(item.page(1))]
    unopened = sum(1 for item in threads if not store.is_stored(item.page(1)) and not _gone(store, item.page(1)))
    if opened and unopened:
        later_pages_per_thread = sum(last - 1 for last in opened) / len(opened)
        left += round(unopened * later_pages_per_thread)
    return left


def _gone(store: PageStore, ref: PageRef) -> bool:
    """Whether the page's last failure says it is gone for good (a 404, say): it won't be opened."""
    failures = store.failures(ref)
    return bool(failures) and failures[-1].is_permanent

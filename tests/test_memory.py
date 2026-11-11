"""How much memory planning takes for each thread: what decides whether a forum with millions of threads fits.

Measured with tracemalloc around opening the store and rebuilding the plan, on stores of small pages (the
pages' size doesn't change what is held). What is checked is the cost of each extra thread: the plan for
2,000 threads less the plan for 1,000, divided by 1,000, so costs that don't grow with the crawl cancel out.
Before runs of pages and bitmasks, a thread cost about 3,400 bytes after planning and 4,000 at the peak.
"""

import tracemalloc
from datetime import datetime, timezone
from pathlib import Path

import pytest

from xfcrawl.failures import Backoff, RetryPolicy
from xfcrawl.layout import StoreLayout
from xfcrawl.link_index import LinkIndex
from xfcrawl.plan import CrawlPlan
from xfcrawl.store import PageStore
from xfcrawl.urls import INDEX_PAGE, ItemRef, Kind

STEP = 1000           # threads added between the two measurements
PAGES = 9
STEADY_BOUND = 1000   # bytes per extra thread, after planning
PEAK_BOUND = 1400


def add_threads(layout: StoreLayout, first: int, count: int, *, every_page: bool) -> None:
    store = PageStore.open(layout)
    store.save(INDEX_PAGE, "<html></html>", None)
    for thread_id in range(first, first + count):
        thread = ItemRef(Kind.THREAD, thread_id)
        navigation = "".join(f'<a href="/threads/t.{thread_id}/page-{p}">{p}</a>' for p in (2, 3, PAGES))
        store.save(thread.page(1), f"<html>{navigation}</html>", "t")
        for page in range(2, PAGES + 1) if every_page else ():
            store.save(thread.page(page), "<html></html>", "t")
    plan(layout)                                     # writes the link index, as a crawl would have


def plan(layout: StoreLayout) -> CrawlPlan:
    built = CrawlPlan("http://forum.example/", PageStore.open(layout), LinkIndex(layout.link_index),
                      Backoff(RetryPolicy(), datetime.now(timezone.utc)))
    built.rebuild()
    return built


def measure(layout: StoreLayout) -> tuple[int, int, CrawlPlan]:
    tracemalloc.start()
    try:
        built = plan(layout)
        steady, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return steady, peak, built


@pytest.mark.parametrize("every_page", [False, True], ids=["first-pages-stored", "every-page-stored"])
def test_each_thread_costs_well_under_a_kilobyte_and_a_half(every_page):
    layout = StoreLayout.in_directory(Path.cwd())
    add_threads(layout, 1, STEP, every_page=every_page)
    steady_before, peak_before, _ = measure(layout)
    add_threads(layout, STEP + 1, STEP, every_page=every_page)
    steady_after, peak_after, built = measure(layout)
    assert built.to_fetch_count() == (0 if every_page else 2 * STEP * (PAGES - 1))
    assert (steady_after - steady_before) / STEP < STEADY_BOUND
    assert (peak_after - peak_before) / STEP < PEAK_BOUND

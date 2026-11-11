from datetime import datetime, timedelta, timezone
from pathlib import Path

from xfcrawl.failures import Failure, RetryPolicy
from xfcrawl.layout import StoreLayout
from xfcrawl.plan import CrawlPlan
from xfcrawl.store import PageStore
from xfcrawl.urls import INDEX_PAGE, ItemRef, Kind

SITE = "http://forum.example/"
NOW = datetime(2026, 11, 9, 16, 0, tzinfo=timezone.utc)
THREAD = ItemRef(Kind.THREAD, 10)


def page(*links: str) -> str:
    return "<html><body>" + "".join(f'<a href="{href}">x</a>' for href in links) + "</body></html>"


def plan_for(store: PageStore, now: datetime = NOW, *, ignore_backoff: bool = False) -> CrawlPlan:
    plan = CrawlPlan(SITE, store, RetryPolicy(), now, ignore_backoff=ignore_backoff)
    plan.rebuild()
    return plan


def new_store() -> PageStore:
    return PageStore(StoreLayout.in_directory(Path.cwd()))


def test_an_empty_store_starts_with_the_index():
    assert [ref.key for ref in plan_for(new_store()).to_fetch()] == ["index:0:1"]


def test_page_navigation_gaps_are_filled():
    store = new_store()
    store.save(INDEX_PAGE, page("/forums/f.2/"), None)
    store.save(ItemRef(Kind.FORUM, 2).page(1), page("/threads/t.10/"), "f")
    store.save(THREAD.page(1), page("/threads/t.10/page-2", "/threads/t.10/page-3", "/threads/t.10/page-7"), "t")
    assert [ref.key for ref in plan_for(store).to_fetch()] == [f"thread:10:{n}" for n in range(2, 8)]


def test_only_a_threads_first_page_and_its_own_pages_count():
    store = new_store()
    store.save(INDEX_PAGE, page("/threads/t.10/"), None)
    store.save(THREAD.page(1), page("/threads/other.99/", "/threads/t.10/page-2"), "t")
    store.save(THREAD.page(2), page("/threads/t.10/page-3", "/threads/elsewhere.98/"), "t")
    assert [ref.key for ref in plan_for(store).to_fetch()] == []


def test_other_sites_are_ignored():
    store = new_store()
    store.save(INDEX_PAGE, page("http://other.example/threads/t.1/", "/threads/here.2/"), None)
    assert [ref.key for ref in plan_for(store).to_fetch()] == ["thread:2:1"]


def test_a_failed_page_waits_out_its_back_off():
    store = new_store()
    store.save(INDEX_PAGE, page("/threads/t.10/"), None)
    store.record_failure(THREAD.page(1), Failure(NOW, "http-503"), None)
    waiting = plan_for(store, NOW + timedelta(minutes=30))
    assert waiting.to_fetch() == []
    assert waiting.waiting() == {THREAD.page(1): NOW + timedelta(hours=1)}
    assert [ref.key for ref in plan_for(store, NOW + timedelta(hours=1, seconds=1)).to_fetch()] == ["thread:10:1"]
    assert [ref.key for ref in plan_for(store, NOW, ignore_backoff=True).to_fetch()] == ["thread:10:1"]


def test_a_redirect_target_is_wanted():
    store = new_store()
    store.save(INDEX_PAGE, page(), None)
    store.record_failure(THREAD.page(9), Failure(NOW, "redirect", detail="http://forum.example/threads/t.10/page-3"),
                         None)
    assert [ref.key for ref in plan_for(store).to_fetch()] == ["thread:10:1", "thread:10:2", "thread:10:3"]


def test_urls_use_the_address_the_site_last_served():
    store = new_store()
    store.save(INDEX_PAGE, page("/threads/old-words.10/"), None)
    plan = plan_for(store)
    assert plan.url_of(THREAD.page(2)) == "http://forum.example/threads/old-words.10/page-2"
    plan.seen_at(THREAD, "http://forum.example/threads/new-words.10/")
    assert plan.url_of(THREAD.page(2)) == "http://forum.example/threads/new-words.10/page-2"
    assert plan.slug(THREAD) == "new-words"


def test_put_back_returns_a_page_to_the_front():
    store = new_store()
    plan = plan_for(store)
    ref = plan.next_page()
    assert ref == INDEX_PAGE
    plan.put_back(ref)
    assert plan.to_fetch() == [INDEX_PAGE]

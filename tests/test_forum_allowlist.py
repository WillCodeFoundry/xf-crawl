"""xf crawl --only-forums: keeping a crawl to some forums."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.test_plan import new_store, page
from xfcrawl.failures import Backoff, RetryPolicy
from xfcrawl.link_index import LinkIndex
from xfcrawl.plan import CrawlPlan
from xfcrawl.urls import INDEX_PAGE, ItemRef, Kind

STORE = Path(".crawl-store")
GENERAL = {"/forums/general.2/", "/forums/general.2/page-2"}
GENERAL_THREADS = {10, 11, 12, 13, 14}


def threads_fetched(forum):
    return {int(hit.path.split(".")[1].split("/")[0]) for hit in forum.hits if hit.path.startswith("/threads/")}


def test_only_the_listed_forum_and_its_threads_are_crawled(forum, xf):
    result = xf("crawl", forum.url, "--only-forums", "5")
    assert result.code == 0, result.err
    assert "Only forums 5" in result.err
    assert threads_fetched(forum) == {20, 21}
    assert not GENERAL & {hit.path for hit in forum.hits}
    assert json.loads((STORE / "config.json").read_text())["only_forums"] == [5]


def test_a_listed_forum_is_found_even_when_only_its_parent_links_it(forum, xf):
    xf("crawl", forum.url, "--only-forums", "5")      # only General (2) links to Sub (5)
    assert forum.count("/forums/5/") == 1               # reached by number, redirected to its address
    assert (STORE / "pages" / "forums" / "sub.5" / "page-0001.html.gz").exists()


def test_sub_forums_are_not_included(forum, xf):
    xf("crawl", forum.url, "--only-forums", "2")      # Sub (5) is General's sub-forum
    assert forum.count("/forums/sub.5/") == 0
    assert threads_fetched(forum) == GENERAL_THREADS


def test_the_list_is_remembered_and_can_be_dropped(forum, xf):
    xf("crawl", forum.url, "--only-forums", "5", "--max-fetches", "2")
    xf("crawl")
    assert threads_fetched(forum) == {20, 21}
    assert "only forums 5" in xf("status").out
    xf("crawl", "--all-forums")
    assert threads_fetched(forum) == {20, 21} | GENERAL_THREADS
    assert "only_forums" not in json.loads((STORE / "config.json").read_text())


def test_narrowing_a_crawl_keeps_the_pages_but_leaves_them_out(forum, xf):
    xf("crawl", forum.url)
    xf("crawl", "--only-forums", "5")
    status = json.loads(xf("status", "--json").out)
    assert status["threads_known"] == 2
    assert [f["forum_id"] for f in status["forums"]] == [5]
    assert xf("pending").out.strip() == ""
    assert len(list((STORE / "pages" / "threads").rglob("*.html.gz"))) == 14   # still on disk


def test_a_thread_linked_from_outside_the_listed_forums_is_left_out():
    store = new_store()
    store.save(INDEX_PAGE, page("/forums/a.2/", "/forums/b.3/", "/threads/latest-post.99/"), None)
    store.save(ItemRef(Kind.FORUM, 2).page(1), page("/threads/in-a.10/", "/forums/b.3/"), "a")
    store.save(ItemRef(Kind.FORUM, 3).page(1), page("/threads/in-b.11/"), "b")
    plan = CrawlPlan("http://forum.example/", store, LinkIndex(STORE / "links.jsonl"),
                     Backoff(RetryPolicy(), datetime.now(timezone.utc)), frozenset({2}))
    plan.rebuild()
    assert [ref.key for ref in plan.to_fetch()] == ["thread:10:1"]


@pytest.mark.parametrize("bad", ["abc", "46,x", ","])
def test_forum_numbers_must_be_numbers(xf, forum, bad):
    with pytest.raises(SystemExit) as done:
        xf("crawl", forum.url, "--only-forums", bad)
    assert done.value.code == 2
    assert not (STORE / "config.json").exists()


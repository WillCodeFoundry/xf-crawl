"""xf mine, against a crawl of the fake forum."""

import json
from pathlib import Path

POSTS = (6 * 20 + 7) + 7 + (20 + 7) + 7 + 7 + 7   # threads 10, 11, 12, 14, 20, 21: full pages hold 20, last 7


def test_statistics(forum, xf):
    forum.gone.add(13)
    xf("crawl", forum.url)
    stats = json.loads(xf("mine", "--json").out)
    assert stats["forums"] == 2
    assert stats["threads_known"] == 7
    assert stats["threads_downloaded"] == 6
    assert stats["threads_complete"] == 6
    assert stats["thread_pages_stored"] == 13
    assert stats["posts_stored"] == POSTS
    assert stats["replies_approx"] is True
    assert stats["pages_failed"] == {"http-404": 1}
    assert {(f["title"], f["threads_listed"]) for f in stats["per_forum"]} == {("General", 5), ("Sub", 2)}


def test_the_report(forum, xf):
    xf("crawl", forum.url, "--max-fetches", "5")
    report = xf("mine").out
    assert "Threads per forum" in report and "General" in report
    assert "crawl not finished" in report


def test_a_thread_missing_pages_is_listed(forum, xf):
    xf("crawl", forum.url)
    (Path(".crawl-store/pages/threads/0/thread-10.10") / "page-0004.html.gz").unlink()
    assert "thread 10: 6 of 7 pages" in xf("mine").out


def test_mine_without_a_crawl(xf):
    result = xf("mine")
    assert result.code == 1
    assert "no crawl in" in result.err

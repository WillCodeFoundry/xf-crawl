"""xf status, against crawls of the fake forum."""

import json

import pytest

from xfcrawl.commands.status import duration


def test_part_way_through_the_first_pages(forum, xf):
    xf("crawl", forum.url, "--max-fetches", "8")      # the index, 3 thread-list pages, 4 thread first pages
    status = json.loads(xf("status", "--json").out)
    stages = {s["stage"]: (s["stored"], s["known"], s["to_fetch"]) for s in status["stages"]}
    assert stages == {"forum_list": (1, 1, 0), "thread_lists": (3, 3, 0), "thread_first_pages": (4, 7, 3),
                      "thread_other_pages": (0, 7, 7)}
    assert status["current_stage"] == "thread_first_pages"
    assert status["threads_known"] == 7
    # threads 10 to 13 are open: 6 + 0 + 1 + 0 later pages, so 1.75 each for the other 3: 5 more
    assert status["estimate"]["pages_left"] == 3 + 7 + 5


def test_the_report_and_the_forums(forum, xf):
    xf("crawl", forum.url, "--max-fetches", "8")
    report = xf("status", "--forums").out
    assert "Now on stage 3: every thread's first page." in report
    assert "pages left" in report
    general = next(line for line in report.splitlines() if " General " in line)
    assert general.split()[:4] == ["2", "General", "5", "80%"]   # forum 2: 5 threads, 4 of them opened


def test_a_complete_crawl(forum, xf):
    forum.gone.add(13)
    xf("crawl", forum.url)
    report = xf("status").out
    assert "Complete." in report
    assert "pages left" not in report
    assert json.loads(xf("status", "--json").out)["stages"][2]["failing"] == 1


def test_failed_pages_waiting_are_mentioned(forum, xf):
    forum.gone.add(13)
    xf("crawl", forum.url)
    assert "1 failed pages are waiting to be retried" in xf("status").out


@pytest.mark.parametrize(("seconds", "text"), [
    (30, "under a minute"), (60, "about 1 minute"), (3 * 86400 + 4 * 3600 + 59, "about 3 days 4 hours"),
    (2 * 3600 + 30 * 60, "about 2 hours 30 minutes"), (86400 + 5, "about 1 day 0 hours"),
])
def test_duration(seconds, text):
    assert duration(seconds) == text

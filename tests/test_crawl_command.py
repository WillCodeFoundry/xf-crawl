"""xf crawl and xf pending, end to end against the fake forum."""

import gzip
import json
from pathlib import Path

from xfcrawl.failures import parse_failures

STORE = Path(".crawl-store")
PAGES = STORE / "pages"
THREADS = PAGES / "threads" / "0"


def stored() -> list[str]:
    return sorted(str(p.relative_to(PAGES)) for p in PAGES.rglob("*.html.gz"))


def reasons(failure_file: Path) -> list[str]:
    return [f.reason for f in parse_failures(failure_file.read_text())]


def test_a_full_crawl(forum, xf):
    result = xf("crawl", forum.url)
    assert result.code == 0, result.err
    assert "Run finished: 18 downloaded, 0 errors, 0 still to fetch." in result.out   # 1 + 3 listings + 14
    files = stored()
    assert {"index.html.gz", "forums/general.2/page-0001.html.gz", "forums/general.2/page-0002.html.gz",
            "forums/sub.5/page-0001.html.gz"} <= set(files)
    # thread 10's navigation links only 1 2 3 7: pages 4 to 6 are inferred
    assert [f for f in files if f.startswith("threads/0/thread-10.10/")] == [
        f"threads/0/thread-10.10/page-{n:04d}.html.gz" for n in range(1, 8)]
    assert json.loads((STORE / "config.json").read_text())["start_url"] == forum.url


def test_it_crawls_level_by_level(forum, xf):
    result = xf("crawl", forum.url)
    fetched = [hit.path for hit in forum.hits if hit.path != "/robots.txt"]
    kinds = ["index" if p == "/" else "forum" if p.startswith("/forums/") else
             "first" if not p.split("/")[-1].startswith("page-") else "other" for p in fetched]
    assert kinds == sorted(kinds, key=["index", "forum", "first", "other"].index)
    stages = [line.split("Now fetching ")[1].split(":")[0]
              for line in result.err.splitlines() if "Now fetching" in line]
    assert stages == ["the forum list", "every forum's thread list", "every thread's first page",
                      "the threads' other pages"]


def test_each_page_is_fetched_once_though_the_sticky_repeats(forum, xf):
    xf("crawl", forum.url)
    assert forum.count("/threads/thread-10.10/") == 1


def test_carrying_on_fetches_only_whats_missing(forum, xf):
    assert "Stopped: reached --max-fetches 4" in xf("crawl", forum.url, "--max-fetches", "4").out
    assert len(stored()) == 4                        # robots.txt isn't one of the four
    xf("crawl")                                      # the URL is remembered
    requests_so_far = len(forum.hits)
    result = xf("crawl")
    assert "0 downloaded" in result.out
    assert len(forum.hits) == requests_so_far        # nothing left: not even robots.txt is fetched


def test_pending_lists_what_is_left_without_fetching(forum, xf):
    xf("crawl", forum.url, "--max-fetches", "3")
    requests_so_far = len(forum.hits)
    result = xf("pending")
    assert result.code == 0
    assert result.out.splitlines()[0] == f"forum:5:1\t{forum.url}forums/sub.5/"
    assert len(forum.hits) == requests_so_far


def test_a_deleted_page_is_fetched_again(forum, xf):
    xf("crawl", forum.url)
    (THREADS / "thread-10.10" / "page-0005.html.gz").unlink()
    assert "1 downloaded" in xf("crawl").out
    assert (THREADS / "thread-10.10" / "page-0005.html.gz").exists()


def test_failures_are_recorded_and_nothing_of_them_stored(forum, xf):
    forum.gone.add(13)
    forum.error_once.add(14)
    assert "2 errors" in xf("crawl", forum.url).out
    assert reasons(THREADS / "thread-13.13" / "page-0001.failed.jsonl") == ["http-404"]
    assert reasons(THREADS / "thread-14.14" / "page-0001.failed.jsonl") == ["error-page"]
    assert not list((THREADS / "thread-13.13").glob("*.html.gz"))
    assert not list((THREADS / "thread-14.14").glob("*.html.gz"))


def test_failed_pages_wait_unless_retried_now(forum, xf):
    forum.gone.add(13)
    forum.error_once.add(14)
    xf("crawl", forum.url)
    result = xf("crawl")
    assert "0 downloaded" in result.out
    assert "2 failed pages are waiting to be retried" in result.out
    assert "1 downloaded, 1 errors" in xf("crawl", "--retry-failed").out   # 14 works now; 13 is still gone
    assert not (THREADS / "thread-14.14" / "page-0001.failed.jsonl").exists()
    assert reasons(THREADS / "thread-13.13" / "page-0001.failed.jsonl") == ["http-404", "http-404"]


def test_a_link_past_a_threads_end_is_recorded_as_a_redirect(forum, xf):
    xf("crawl", forum.url)
    page = PAGES / "forums" / "sub.5" / "page-0001.html.gz"
    html = gzip.decompress(page.read_bytes()).decode()
    page.write_bytes(gzip.compress(html.replace("</body>", '<a href="/threads/thread-12.12/page-5">x</a></body>')
                                   .encode()))
    assert "redirected to" in xf("crawl").err
    assert reasons(THREADS / "thread-12.12" / "page-0003.failed.jsonl") == ["redirect"]
    assert not (THREADS / "thread-12.12" / "page-0003.html.gz").exists()


def test_a_thread_renamed_on_the_site_has_its_folder_renamed(forum, xf):
    xf("crawl", forum.url)
    (THREADS / "thread-10.10" / "page-0006.html.gz").unlink()
    forum.slugs[10] = "a-new-title"
    assert "1 downloaded" in xf("crawl").out
    assert not (THREADS / "thread-10.10").exists()
    assert len(list((THREADS / "a-new-title.10").glob("*.html.gz"))) == 7


def test_a_store_belongs_to_one_forum(forum, xf):
    xf("crawl", forum.url, "--max-fetches", "1")
    result = xf("crawl", "http://elsewhere.example/")
    assert result.code == 1
    assert "is a crawl of" in result.err


def test_a_url_that_isnt_one_is_refused_at_once(xf):
    result = xf("crawl", "Traceback (most recent call last):")
    assert result.code == 1
    assert "is not a forum address" in result.err
    assert not Path(".crawl-store/config.json").exists()


def test_crawl_without_a_url_and_no_store_explains(xf):
    result = xf("crawl")
    assert result.code == 1
    assert "give the forum's URL" in result.err


def test_robots_txt_is_obeyed(forum, xf):
    forum.robots = "User-agent: *\nDisallow: /threads/thread-11.11/\nCrawl-delay: 0\n"
    result = xf("crawl", forum.url)
    assert forum.count("/threads/thread-11.11/") == 0
    assert reasons(THREADS / "thread-11.11" / "page-0001.failed.jsonl") == ["robots"]
    assert "blocked by robots.txt" in result.err

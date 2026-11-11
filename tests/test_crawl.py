import json
from pathlib import Path

from xfcrawl.common import read_failures

STORE = Path(".crawl-store")
PAGES = STORE / "pages"


def stored_files():
    return sorted(str(p.relative_to(PAGES)) for p in PAGES.rglob("*.html.gz"))


def test_full_crawl_layout(forum, run):
    r = run("xf-crawl", forum.url)
    assert r.code == 0, r.err
    assert "0 still to fetch" in r.err
    files = stored_files()
    assert "index.html.gz" in files
    assert "forums/general.2/page-0001.html.gz" in files
    assert "forums/general.2/page-0002.html.gz" in files
    assert "forums/sub.5/page-0001.html.gz" in files
    # thread 10's navigation only links 1 2 3 7; pages 4-6 are inferred
    assert [f for f in files if f.startswith("threads/0/thread-10.10/")] == [
        f"threads/0/thread-10.10/page-{n:04d}.html.gz" for n in range(1, 8)]
    assert json.loads((STORE / "config.json").read_text())["start_url"] == forum.url


def test_each_page_fetched_once_despite_sticky(forum, run):
    run("xf-crawl", forum.url)
    assert forum.count("/threads/thread-10.10/") == 1
    assert forum.count("/forums/general.2/") == 1


def test_resume_and_no_refetch(forum, run):
    r = run("xf-crawl", forum.url, "--max-fetches", "4")
    assert "Reached --max-fetches 4" in r.err
    assert len(stored_files()) == 4
    run("xf-crawl")                                  # URL remembered
    total = len(forum.hits)
    r = run("xf-crawl")
    assert "0 downloaded" in r.err
    assert len(forum.hits) == total                  # nothing requested at all


def test_list_is_offline(forum, run):
    run("xf-crawl", forum.url, "--max-fetches", "2")
    before = len(forum.hits)
    r = run("xf-crawl", "--list")
    assert r.code == 0
    assert "forum:2:2" in r.out and "forum:5:1" in r.out
    assert len(forum.hits) == before


def test_deleted_page_is_refetched(forum, run):
    run("xf-crawl", forum.url)
    victim = PAGES / "threads/0/thread-10.10/page-0005.html.gz"
    victim.unlink()
    r = run("xf-crawl")
    assert "1 downloaded" in r.err
    assert victim.exists()


def test_failures_are_recorded_not_stored(forum, run):
    forum.gone.add(13)
    forum.error_once.add(14)
    r = run("xf-crawl", forum.url)
    assert "2 errors" in r.err
    gone = PAGES / "threads/0/thread-13.13/page-0001.failed.jsonl"
    err = PAGES / "threads/0/thread-14.14/page-0001.failed.jsonl"
    assert [x[1] for x in read_failures(gone)] == ["http-404"]
    assert [x[1] for x in read_failures(err)] == ["error-page"]
    assert not (PAGES / "threads/0/thread-13.13/page-0001.html.gz").exists()
    assert not (PAGES / "threads/0/thread-14.14/page-0001.html.gz").exists()
    rec = json.loads(gone.read_text().splitlines()[0])
    assert rec["url"].endswith("/threads/thread-13.13/") and "time" in rec


def test_backoff_then_retry_failed(forum, run):
    forum.gone.add(13)
    forum.error_once.add(14)
    run("xf-crawl", forum.url)
    r = run("xf-crawl")                              # immediately again: both waiting
    assert "2 failed pages are waiting" in r.err
    assert "0 downloaded" in r.err
    r = run("xf-crawl", "--retry-failed")            # 14 works now, 13 still 404
    assert "1 downloaded, 1 errors" in r.err
    assert (PAGES / "threads/0/thread-14.14/page-0001.html.gz").exists()
    assert not (PAGES / "threads/0/thread-14.14/page-0001.failed.jsonl").exists()
    assert len(read_failures(PAGES / "threads/0/thread-13.13/page-0001.failed.jsonl")) == 2


def test_transient_503_is_retried_within_the_run(forum, run):
    forum.busy_once.add(11)
    r = run("xf-crawl", forum.url)
    assert "HTTP 503; retry 1/4" in r.err
    assert (PAGES / "threads/0/thread-11.11/page-0001.html.gz").exists()


def test_page_past_the_end_redirect(forum, run):
    run("xf-crawl", forum.url)
    page1 = PAGES / "forums/sub.5/page-0001.html.gz"
    import gzip
    html = gzip.decompress(page1.read_bytes()).decode()
    html = html.replace("</body>", '<a href="/threads/thread-12.12/page-5">stale</a></body>')
    page1.write_bytes(gzip.compress(html.encode()))
    r = run("xf-crawl")
    assert "redirected to" in r.err
    reasons = [x[1] for x in read_failures(PAGES / "threads/0/thread-12.12/page-0003.failed.jsonl")]
    assert reasons == ["redirect"]
    assert not (PAGES / "threads/0/thread-12.12/page-0003.html.gz").exists()


def test_renamed_thread_folder_follows(forum, run):
    run("xf-crawl", forum.url)
    (PAGES / "threads/0/thread-10.10/page-0006.html.gz").unlink()
    forum.slugs[10] = "a-new-title"
    r = run("xf-crawl")
    assert "1 downloaded" in r.err
    assert not (PAGES / "threads/0/thread-10.10").exists()
    assert len(list((PAGES / "threads/0/a-new-title.10").glob("*.html.gz"))) == 7


def test_duplicate_folders_are_merged(forum, run):
    run("xf-crawl", forum.url)
    bare = PAGES / "threads/0/20"
    bare.mkdir()
    (PAGES / "threads/0/thread-20.20/page-0001.html.gz").rename(bare / "page-0001.html.gz")
    r = run("xf-crawl")
    assert "0 downloaded" in r.err
    assert not bare.exists()
    assert (PAGES / "threads/0/thread-20.20/page-0001.html.gz").exists()


def test_other_store_cannot_be_hijacked(forum, run):
    run("xf-crawl", forum.url, "--max-fetches", "1")
    r = run("xf-crawl", "http://elsewhere.example/")
    assert r.code == 2
    assert "belongs to a crawl of" in r.err


def test_mine_reports(forum, run):
    forum.gone.add(13)
    run("xf-crawl", forum.url)
    r = run("xf-mine", "--json")
    s = r.json()
    assert s["forums"] == 2
    assert s["threads_known"] == 7
    assert s["threads_downloaded"] == 6
    assert s["threads_complete"] == 6
    assert s["thread_pages_stored"] == 13
    assert s["posts_stored"] == (6 * 20 + 7) + 7 + (20 + 7) + 7 + 7 + 7   # threads 10, 11, 12, 14, 20, 21
    assert s["replies_approx"] is True
    assert s["pages_failed"] == {"http-404": 1}
    assert {f["title"] for f in s["per_forum"]} == {"General", "Sub"}
    text = run("xf-mine").out
    assert "Threads per forum" in text and "General" in text

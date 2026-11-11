import json
import re
import shutil
from pathlib import Path

from xfcrawl.common import read_failures

STORE = Path(".crawl-store")
PAGES = STORE / "pages"


def make_old_layout(root=PAGES):
    """Turn a current store into the earlier layout: forums/<id>/ and threads/<id>/."""
    for d in list((root / "forums").iterdir()):
        d.rename(root / "forums" / re.sub(r"^.*\.(\d+)$", r"\1", d.name))
    for group in list((root / "threads").iterdir()):     # test threads are all in group "0"
        for d in list(group.iterdir()):
            d.rename(root / "threads" / re.sub(r"^.*\.(\d+)$", r"\1", d.name))
        group.rmdir()


def tree():
    return sorted(str(p.relative_to(PAGES)) for p in PAGES.rglob("*") if p.is_file())


def test_old_store_is_refused_until_migrated(forum, run):
    run("xf-crawl", forum.url)
    make_old_layout()
    hits = len(forum.hits)
    for cmd in ("xf-crawl", "xf-mine"):
        r = run(cmd)
        assert r.code == 1
        assert "uv run xf-migrate" in r.err
    assert len(forum.hits) == hits


def test_migrate_restores_named_grouped_layout(forum, run):
    run("xf-crawl", forum.url)
    expected = tree()
    make_old_layout()
    assert (PAGES / "threads/10/page-0001.html.gz").exists()
    r = run("xf-migrate")
    assert r.code == 0, r.err
    assert "threads moved into groups of 1000" in r.out
    assert "folders named after the site's URLs" in r.out
    assert tree() == expected
    hits = len(forum.hits)
    r = run("xf-crawl")
    assert "0 downloaded" in r.err
    assert len(forum.hits) == hits                       # nothing downloaded again
    assert run("xf-mine", "--json").json()["threads_downloaded"] == 7
    assert run("xf-migrate").out.strip() == "Already up to date; nothing to migrate."


def test_migrate_oldest_format(forum, run):
    forum.gone.add(13)
    run("xf-crawl", forum.url)
    make_old_layout()
    # pretend thread 11 and page 2 of thread 12 had failed, in the two older formats
    (PAGES / "threads/11/page-0001.html.gz").unlink()
    (PAGES / "threads/12/page-0002.html.gz").unlink()
    (PAGES / "threads/12/page-0002.failed").write_text("2026-11-09T16:18:09+00:00\thttp-503\t\n")
    (PAGES / "threads/13/page-0001.failed.jsonl").unlink()
    (PAGES / "threads/13/page-0001.gone.json").write_text(json.dumps({
        "key": "thread:13:1", "url": "u", "time": "2026-11-09T16:11:40+00:00",
        "status": "gone", "error": "HTTP 404", "attempts": 0}))
    (STORE / "errors.jsonl").write_text(json.dumps({
        "key": "thread:11:1", "url": "http://x/threads/thread-11.11/", "time": "2026-11-09T16:11:40+00:00",
        "status": "failed", "error": "ReadTimeout: x"}) + "\n")
    # and the store still lived in .state/cache
    STORE.rename(".state")
    Path(".state/pages").rename(".state/cache")

    r = run("xf-crawl")
    assert r.code == 1 and ".state" in r.err

    r = run("xf-migrate")
    assert r.code == 0, r.err
    assert not Path(".state").exists()
    assert not (STORE / "errors.jsonl").exists()
    assert not list(PAGES.rglob("*.json"))
    assert not list(PAGES.rglob("*.failed"))
    assert [x[1] for x in read_failures(PAGES / "threads/0/thread-11.11/page-0001.failed.jsonl")] == ["network"]
    assert [x[1] for x in read_failures(PAGES / "threads/0/thread-12.12/page-0002.failed.jsonl")] == ["http-503"]
    assert [x[1] for x in read_failures(PAGES / "threads/0/thread-13.13/page-0001.failed.jsonl")] == ["http-404"]

    r = run("xf-crawl", "--retry-failed")
    assert "2 downloaded, 1 errors" in r.err, r.err      # 11 and 12/2 come back; 13 is still 404


def test_interrupted_migration_is_finished(forum, run):
    run("xf-crawl", forum.url)
    expected = tree()
    make_old_layout()
    (PAGES / "threads/21").rename(PAGES / "threads/_moving.21")   # as if killed mid-way
    assert "unfinished migration" in run("xf-crawl").err
    run("xf-migrate")
    assert tree() == expected


def test_migrate_nothing_here(run):
    r = run("xf-migrate")
    assert r.code == 1
    assert "nothing to migrate" in r.err


def test_backup_restore_roundtrip(forum, run, tmp_path):
    """The README says: back up, then migrate. Make sure a restored backup still migrates."""
    run("xf-crawl", forum.url)
    make_old_layout()
    shutil.copytree(STORE, tmp_path / "backup")
    run("xf-migrate")
    shutil.rmtree(STORE)
    shutil.copytree(tmp_path / "backup", STORE)
    assert run("xf-migrate").code == 0
    assert "0 downloaded" in run("xf-crawl").err

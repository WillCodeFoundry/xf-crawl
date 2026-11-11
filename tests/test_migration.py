"""xf migrate, and the refusal to use a store in an older layout, against real crawls made older."""

import json
import re
import shutil
from pathlib import Path

from xfcrawl.failures import parse_failures

STORE = Path(".crawl-store")
PAGES = STORE / "pages"
THREADS = PAGES / "threads"


def make_old_layout() -> None:
    """Turn a current store into the earliest layout: forums/<id>/ and threads/<id>/."""
    for folder in list((PAGES / "forums").iterdir()):
        folder.rename(PAGES / "forums" / re.sub(r"^.*\.(\d+)$", r"\1", folder.name))
    for group in list(THREADS.iterdir()):           # the test threads are all in group 0
        for folder in list(group.iterdir()):
            folder.rename(THREADS / re.sub(r"^.*\.(\d+)$", r"\1", folder.name))
        group.rmdir()


def tree() -> list[str]:
    return sorted(str(p.relative_to(PAGES)) for p in PAGES.rglob("*") if p.is_file())


def reasons(path: Path) -> list[str]:
    return [f.reason for f in parse_failures(path.read_text())]


def test_an_old_store_is_refused_until_migrated(forum, xf):
    xf("crawl", forum.url)
    make_old_layout()
    requests_so_far = len(forum.hits)
    for command in ("crawl", "mine", "pending"):
        result = xf(command)
        assert result.code == 1
        assert "uv run xf migrate" in result.err
    assert len(forum.hits) == requests_so_far


def test_migrating_restores_the_current_layout_without_downloading(forum, xf):
    xf("crawl", forum.url)
    expected = tree()
    make_old_layout()
    assert (THREADS / "10" / "page-0001.html.gz").exists()
    result = xf("migrate")
    assert result.code == 0, result.err
    assert "threads moved into groups of 1000" in result.out
    assert "folders named after the site's URLs" in result.out
    assert tree() == expected
    requests_so_far = len(forum.hits)
    assert "0 downloaded" in xf("crawl").out
    assert len(forum.hits) == requests_so_far
    assert xf("migrate").out.strip() == "Already up to date; nothing to migrate."


def test_the_oldest_format_is_converted(forum, xf):
    forum.gone.add(13)
    xf("crawl", forum.url)
    make_old_layout()
    (THREADS / "11" / "page-0001.html.gz").unlink()
    (THREADS / "12" / "page-0002.html.gz").unlink()
    (THREADS / "12" / "page-0002.failed").write_text("2026-11-09T16:18:09+00:00\thttp-503\t\n")
    (THREADS / "13" / "page-0001.failed.jsonl").unlink()
    (THREADS / "13" / "page-0001.gone.json").write_text(json.dumps({
        "key": "thread:13:1", "url": "u", "time": "2026-11-09T16:11:40+00:00",
        "status": "gone", "error": "HTTP 404", "attempts": 0}))
    (STORE / "errors.jsonl").write_text(json.dumps({
        "key": "thread:11:1", "url": "http://x/threads/thread-11.11/", "time": "2026-11-09T16:11:40+00:00",
        "status": "failed", "error": "ReadTimeout: x"}) + "\n")
    STORE.rename(".state")
    Path(".state/pages").rename(".state/cache")

    assert ".state" in xf("crawl").err

    assert xf("migrate").code == 0
    assert not Path(".state").exists()
    assert not (STORE / "errors.jsonl").exists()
    assert not list(PAGES.rglob("*.json")) and not list(PAGES.rglob("*.failed"))
    assert reasons(THREADS / "0" / "thread-11.11" / "page-0001.failed.jsonl") == ["network"]
    assert reasons(THREADS / "0" / "thread-12.12" / "page-0002.failed.jsonl") == ["http-503"]
    assert reasons(THREADS / "0" / "thread-13.13" / "page-0001.failed.jsonl") == ["http-404"]
    assert "2 downloaded, 1 errors" in xf("crawl", "--retry-failed").out   # 11 and 12/2 are back; 13 is gone


def test_an_interrupted_migration_is_finished(forum, xf):
    xf("crawl", forum.url)
    expected = tree()
    make_old_layout()
    (THREADS / "21").rename(THREADS / "_moving.21")   # as if killed half way
    assert "an unfinished migration" in xf("crawl").err
    xf("migrate")
    assert tree() == expected


def test_a_restored_backup_migrates_too(forum, xf, tmp_path):
    xf("crawl", forum.url)
    make_old_layout()
    shutil.copytree(STORE, tmp_path / "backup")
    xf("migrate")
    shutil.rmtree(STORE)
    shutil.copytree(tmp_path / "backup", STORE)
    assert xf("migrate").code == 0
    assert "0 downloaded" in xf("crawl").out


def test_nothing_to_migrate_here(xf):
    result = xf("migrate")
    assert result.code == 1
    assert "nothing to migrate" in result.err

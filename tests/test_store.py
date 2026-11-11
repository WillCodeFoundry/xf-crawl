from datetime import datetime, timezone
from pathlib import Path

from xfcrawl.failures import Failure
from xfcrawl.layout import StoreLayout
from xfcrawl.store import PageStore
from xfcrawl.urls import INDEX_PAGE, ItemRef, Kind

T0 = datetime(2026, 11, 9, 16, 0, tzinfo=timezone.utc)
THREAD = ItemRef(Kind.THREAD, 1234567)
FORUM = ItemRef(Kind.FORUM, 46)


def layout() -> StoreLayout:
    return StoreLayout.in_directory(Path.cwd())


def files() -> list[str]:
    return sorted(str(p.relative_to(layout().pages_dir)) for p in layout().pages_dir.rglob("*") if p.is_file())


def test_pages_go_in_folders_named_like_the_site_with_threads_grouped_by_1000():
    store = PageStore(layout())
    store.save(INDEX_PAGE, "<html>index</html>", None)
    store.save(FORUM.page(1), "<html>forum</html>", "french-english")
    store.save(THREAD.page(2), "<html>thread</html>", "parler")
    assert files() == ["forums/french-english.46/page-0001.html.gz", "index.html.gz",
                       "threads/1234/parler.1234567/page-0002.html.gz"]
    reopened = PageStore.open(layout())
    assert reopened.read(THREAD.page(2)) == "<html>thread</html>"
    assert {ref.key for ref in reopened.stored_pages()} == {"index:0:1", "forum:46:1", "thread:1234567:2"}


def test_saving_under_new_words_renames_the_folder():
    store = PageStore(layout())
    store.save(THREAD.page(1), "<html>1</html>", "old-title")
    store.save(THREAD.page(2), "<html>2</html>", "new-title")
    assert files() == ["threads/1234/new-title.1234567/page-0001.html.gz",
                       "threads/1234/new-title.1234567/page-0002.html.gz"]


def test_a_failure_names_only_a_bare_folder():
    store = PageStore(layout())
    store.save(THREAD.page(1), "<html>1</html>", None)
    store.record_failure(THREAD.page(2), Failure(T0, "http-503"), "known-words")
    assert (layout().threads_dir / "1234" / "known-words.1234567").is_dir()
    store.record_failure(THREAD.page(3), Failure(T0, "http-503"), "other-words")
    assert (layout().threads_dir / "1234" / "known-words.1234567").is_dir()


def test_failures_are_kept_until_the_page_is_saved():
    store = PageStore(layout())
    store.record_failure(THREAD.page(1), Failure(T0, "http-503"), "t")
    history = store.record_failure(THREAD.page(1), Failure(T0, "timeout"), "t")
    assert [f.reason for f in history] == ["http-503", "timeout"]
    assert [f.reason for f in PageStore.open(layout()).failures(THREAD.page(1))] == ["http-503", "timeout"]
    store.save(THREAD.page(1), "<html>ok</html>", "t")
    assert files() == ["threads/1234/t.1234567/page-0001.html.gz"]
    assert PageStore.open(layout()).failing_pages() == {}


def test_opening_tidies_what_a_crash_left():
    folder = layout().threads_dir / "1234"
    (folder / "1234567").mkdir(parents=True)                  # an interrupted rename: two folders
    (folder / "1234567" / "page-0002.html.gz").write_bytes(b"")
    (folder / "t.1234567").mkdir()
    (folder / "t.1234567" / "page-0001.html.gz").write_bytes(b"")
    (folder / "t.1234567" / ".page-0003.html.gz.abc123").write_bytes(b"half")   # a partial write
    (folder / "t.1234567" / "page-0001.failed.jsonl").write_text(Failure(T0, "http-503").to_json_line())
    store = PageStore.open(layout())
    assert files() == ["threads/1234/t.1234567/page-0001.html.gz", "threads/1234/t.1234567/page-0002.html.gz"]
    assert store.failing_pages() == {}

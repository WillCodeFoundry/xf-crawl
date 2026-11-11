"""PageStore: the pages a crawl has fetched, and the failed fetches of those it couldn't.

The only module that reads or writes ``.crawl-store/pages`` (``migration`` aside, which converts older
layouts into this one). Pages are kept gzipped, each forum and thread in a folder named like its URL; see
``layout``. Opening a store reads the whole folder tree once; after that the store knows every stored page
and every failure without touching the disk again.
"""

import gzip
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

from xfcrawl.atomic_files import PARTIAL_PREFIX, append_line, atomic_write
from xfcrawl.failures import Failure, parse_failures
from xfcrawl.layout import ItemDirName, PageFileName, StoreLayout
from xfcrawl.urls import INDEX_PAGE, ItemRef, Kind, PageRef


class PageStore:
    def __init__(self, layout: StoreLayout) -> None:
        """An empty store; use ``open`` for one that reads what is on disk."""
        self._layout = layout
        self._item_dirs: dict[ItemRef, Path] = {}
        self._stored: set[PageRef] = set()
        self._failures: dict[PageRef, list[Failure]] = {}

    @classmethod
    def open(cls, layout: StoreLayout) -> "PageStore":
        """Read what is stored. Tidies as it goes: removes partial files left by a crash, merges the two
        folders an interrupted rename can leave, and drops the failures of pages that were fetched later."""
        store = cls(layout)
        store._scan()
        return store

    # -- what is stored ------------------------------------------------------------------------------
    def is_stored(self, ref: PageRef) -> bool:
        return ref in self._stored

    def stored_pages(self) -> Iterator[PageRef]:
        return iter(self._stored)

    def stored_count(self) -> int:
        return len(self._stored)

    def failures(self, ref: PageRef) -> list[Failure]:
        """The page's failed fetches, oldest first; empty if it never failed or was fetched since."""
        return list(self._failures.get(ref, []))

    def failing_pages(self) -> dict[PageRef, list[Failure]]:
        return {ref: list(failures) for ref, failures in self._failures.items()}

    def read(self, ref: PageRef) -> str:
        return gzip.decompress(self._page_path(ref).read_bytes()).decode("utf-8", errors="replace")

    def item_dir(self, item: ItemRef) -> Path | None:
        return self._item_dirs.get(item)

    # -- changing it ---------------------------------------------------------------------------------
    def save(self, ref: PageRef, html: str, current_slug: str | None) -> None:
        """Store a fetched page, in a folder named after ``current_slug``, the slug of the URL the site
        just served it from: a folder with other words was renamed on the site, and is renamed to match."""
        if ref.kind is not Kind.INDEX:
            self._name_folder(ref.item, current_slug, rename_named=True)
        atomic_write(self._page_path(ref), gzip.compress(html.encode("utf-8"), compresslevel=6, mtime=0))
        self._stored.add(ref)
        if self._failures.pop(ref, None) is not None:
            self._failure_path(ref).unlink(missing_ok=True)

    def record_failure(self, ref: PageRef, failure: Failure, known_slug: str | None) -> list[Failure]:
        """Add a failed fetch to the page's history and return the history."""
        if ref.kind is not Kind.INDEX:
            self.name_folder(ref.item, known_slug)
        append_line(self._failure_path(ref), failure.to_json_line())
        history = self._failures.setdefault(ref, [])
        history.append(failure)
        return list(history)

    def name_folder(self, item: ItemRef, slug: str | None) -> None:
        """Give a folder still called just ``<id>`` its words. A folder that has words keeps them: only
        ``save``, which knows the URL the site uses now, renames those."""
        self._name_folder(item, slug, rename_named=False)

    # -- internals -----------------------------------------------------------------------------------
    def _name_folder(self, item: ItemRef, slug: str | None, *, rename_named: bool) -> None:
        current = self._item_dirs.get(item)
        if current is None:
            self._item_dirs[item] = self._layout.item_dir(item, slug)
            return
        name = ItemDirName.parse(current.name)
        has_words = name is not None and name.slug is not None
        if slug is None or (has_words and not rename_named):
            return
        wanted = self._layout.item_dir(item, slug)
        if current != wanted and current.is_dir():
            if wanted.exists():
                merge_dirs(current, wanted)
            else:
                current.rename(wanted)
        self._item_dirs[item] = wanted

    def _dir(self, ref: PageRef) -> Path:
        return self._item_dirs.setdefault(ref.item, self._layout.item_dir(ref.item, None))

    def _page_path(self, ref: PageRef) -> Path:
        if ref == INDEX_PAGE:
            return self._layout.index_file(failure=False)
        return self._layout.page_file(self._dir(ref), ref.page, failure=False)

    def _failure_path(self, ref: PageRef) -> Path:
        if ref == INDEX_PAGE:
            return self._layout.index_file(failure=True)
        return self._layout.page_file(self._dir(ref), ref.page, failure=True)

    def _scan(self) -> None:
        self._find_item_dirs()
        self._read_dir(INDEX_PAGE.item, self._layout.pages_dir)
        for item, path in list(self._item_dirs.items()):
            self._read_dir(item, path)
        for ref in [ref for ref in self._failures if ref in self._stored]:
            self._failures.pop(ref)
            self._failure_path(ref).unlink()

    def _find_item_dirs(self) -> None:
        found = _subdirs(self._layout.forums_dir, Kind.FORUM)
        for group in _subdirs_named_by_number(self._layout.threads_dir):
            found += _subdirs(group, Kind.THREAD)
        for item, path in found:
            previous = self._item_dirs.get(item)
            if previous is None:
                self._item_dirs[item] = path
                continue
            # two folders for one item: an interrupted rename. Keep the one with words.
            keep, drop = (path, previous) if _has_words(path) else (previous, path)
            merge_dirs(drop, keep)
            self._item_dirs[item] = keep

    def _read_dir(self, item: ItemRef, path: Path) -> None:
        if not path.is_dir():
            return
        for entry in os.scandir(path):
            if entry.name.startswith(PARTIAL_PREFIX):
                os.remove(entry.path)
                continue
            if not entry.is_file() or (item.kind is Kind.INDEX) != entry.name.startswith("index."):
                continue
            name = PageFileName.parse(entry.name)
            if name is None:
                continue
            ref = item.page(name.page)
            if name.is_failure:
                self._failures[ref] = parse_failures(Path(entry.path).read_text(encoding="utf-8"))
            else:
                self._stored.add(ref)


def _subdirs(parent: Path, kind: Kind) -> list[tuple[ItemRef, Path]]:
    if not parent.is_dir():
        return []
    found = []
    for entry in os.scandir(parent):
        name = ItemDirName.parse(entry.name)
        if entry.is_dir() and name is not None:
            found.append((ItemRef(kind, name.item_id), Path(entry.path)))
    return found


def _subdirs_named_by_number(parent: Path) -> list[Path]:
    if not parent.is_dir():
        return []
    return [Path(entry.path) for entry in os.scandir(parent) if entry.is_dir() and entry.name.isdigit()]


def _has_words(path: Path) -> bool:
    name = ItemDirName.parse(path.name)
    return name is not None and name.slug is not None


def merge_dirs(source: Path, target: Path) -> None:
    """Move source's files into target, where target's copy wins, then remove source."""
    target.mkdir(parents=True, exist_ok=True)
    for entry in os.scandir(source):
        destination = target / entry.name
        if destination.exists():
            os.remove(entry.path)
        else:
            shutil.move(entry.path, destination)
    source.rmdir()

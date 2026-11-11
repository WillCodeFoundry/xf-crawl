"""Where everything sits in a crawl store. The only module that knows its current layout.

    .crawl-store/
      config.json                                   CrawlConfig
      links.jsonl                                   LinkIndex: what each page links to (a shortcut)
      tunnelbear.json                               TunnelBear login tokens, when used
      pages/index.html.gz
      pages/forums/<slug>.<id>/page-0001.html.gz
      pages/threads/<id // 1000>/<slug>.<id>/page-0001.html.gz
      .../page-0001.failed.jsonl                    the page's failed fetches, instead of its .html.gz

A forum's or thread's folder is named like its URL; the number after the last dot is what identifies it,
and a folder is just ``<id>`` until its words are known. Threads are grouped by ``id // 1000`` so that no
directory holds millions of entries. Every method returns a path without checking that it exists.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from xfcrawl.urls import ItemRef, Kind

STORE_DIR_NAME = ".crawl-store"
_THREADS_PER_GROUP = 1000
_ITEM_DIR = re.compile(r"^(?:(?P<slug>.+)\.)?(?P<id>\d+)$")
_PAGE_FILE = re.compile(r"^(?:page-(?P<page>\d+)|index)\.(?P<ext>html\.gz|failed\.jsonl)$")
_PAGE_EXT = "html.gz"
_FAILED_EXT = "failed.jsonl"


@dataclass(frozen=True)
class ItemDirName:
    item_id: int
    slug: str | None

    def __str__(self) -> str:
        return f"{self.slug}.{self.item_id}" if self.slug else str(self.item_id)

    @classmethod
    def parse(cls, name: str) -> "ItemDirName | None":
        match = _ITEM_DIR.match(name)
        return cls(int(match["id"]), match["slug"]) if match else None


@dataclass(frozen=True)
class PageFileName:
    page: int
    is_failure: bool

    @classmethod
    def parse(cls, name: str) -> "PageFileName | None":
        """A stored page or failure file in an item's folder (or ``index.*`` in the pages folder)."""
        match = _PAGE_FILE.match(name)
        if match is None:
            return None
        return cls(int(match["page"] or 1), match["ext"] == _FAILED_EXT)


@dataclass(frozen=True)
class StoreLayout:
    root: Path

    @classmethod
    def in_directory(cls, directory: Path) -> "StoreLayout":
        return cls(directory / STORE_DIR_NAME)

    @property
    def config_file(self) -> Path:
        return self.root / "config.json"

    @property
    def link_index(self) -> Path:
        return self.root / "links.jsonl"

    @property
    def tunnelbear_tokens(self) -> Path:
        return self.root / "tunnelbear.json"

    @property
    def pages_dir(self) -> Path:
        return self.root / "pages"

    @property
    def forums_dir(self) -> Path:
        return self.pages_dir / "forums"

    @property
    def threads_dir(self) -> Path:
        return self.pages_dir / "threads"

    def item_parent(self, item: ItemRef) -> Path:
        """The folder an item's own folder goes in."""
        if item.kind is Kind.THREAD:
            return self.threads_dir / str(item.item_id // _THREADS_PER_GROUP)
        return self.forums_dir

    def item_dir(self, item: ItemRef, slug: str | None) -> Path:
        return self.item_parent(item) / str(ItemDirName(item.item_id, slug))

    def index_file(self, *, failure: bool) -> Path:
        return self.pages_dir / f"index.{_FAILED_EXT if failure else _PAGE_EXT}"

    @staticmethod
    def page_file(item_dir: Path, page: int, *, failure: bool) -> Path:
        return item_dir / f"page-{page:04d}.{_FAILED_EXT if failure else _PAGE_EXT}"

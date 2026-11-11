"""LinkIndex: what each stored page links to, so a run doesn't re-read the pages to plan.

A shortcut, not state: everything in it can be worked out again from the stored pages, and the plan does
that for any stored page the index doesn't cover (it was deleted, or the page was stored before it existed).
The pages stay the source of truth.

One JSON line per page the plan reads (the index, every forum page, and every thread's first page),
appended when the page is stored. If a page is stored again, its later line wins.

    {"page": "forum:2:1", "title": "General", "links": [["thread", 10, 7, "/threads/a-thread.10/"], ...]}

Each link is a forum or thread on the same site: its kind and number, the highest page linked, and the
path of its first page. Paths, not URLs, keep the file small; the site's address is in the crawl's config.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from xfcrawl.atomic_files import append_line, append_lines
from xfcrawl.urls import ItemRef, Kind, PageRef


@dataclass(frozen=True)
class ItemLink:
    item: ItemRef
    last_page: int
    """The highest page of the item linked from the page."""
    path: str
    """The path of the item's first page, such as ``/threads/a-thread.10/``."""


@dataclass(frozen=True)
class PageLinks:
    page: PageRef
    title: str | None
    links: tuple[ItemLink, ...]

    def to_json_line(self) -> str:
        record: dict[str, object] = {"page": self.page.key}
        if self.title:
            record["title"] = self.title
        record["links"] = [[link.item.kind.value, link.item.item_id, link.last_page, link.path] for link in self.links]
        return json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"


class LinkIndex:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[PageRef, PageLinks]:
        """Every page's latest entry. A line cut short by a crash is skipped; its page is read again."""
        if not self._path.exists():
            return {}
        entries: dict[PageRef, PageLinks] = {}
        with self._path.open(encoding="utf-8") as lines:
            for line in lines:
                entry = _parse(line)
                if entry is not None:
                    entries[entry.page] = entry
        return entries

    def record(self, entry: PageLinks) -> None:
        append_line(self._path, entry.to_json_line())

    def record_all(self, entries: list[PageLinks]) -> None:
        append_lines(self._path, [entry.to_json_line() for entry in entries])


def _parse(line: str) -> PageLinks | None:
    try:
        record = json.loads(line)
        kind, item_id, page = record["page"].split(":")
        links = tuple(
            ItemLink(ItemRef(Kind(link_kind), int(link_id)), int(last_page), str(path))
            for link_kind, link_id, last_page, path in record["links"]
        )
        return PageLinks(PageRef(Kind(kind), int(item_id), int(page)), record.get("title"), links)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None

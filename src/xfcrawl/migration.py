"""Converting a crawl store made by an earlier version, and refusing to use one until it is converted.

The only module that knows the older layouts and formats:

  1. ``.state/`` (pages in ``cache/`` or ``page-store/``)  -> ``.crawl-store/`` (``pages/``)
  2. ``threads/<id>/``                                       -> ``threads/<id // 1000>/<id>/``
  3. ``.crawl-store/errors.jsonl``                           -> each page's ``.failed.jsonl``
  4. ``page-NNNN.failed`` (tab-separated) and ``page-NNNN.{failed,gone,redirect}.json`` -> ``.failed.jsonl``
  5. folders called just ``<id>``                            -> ``<slug>.<id>``, like the site's URLs

Nothing is downloaded. Each step skips what is already done, so running it twice is safe, and so is
running it again after it was interrupted.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from xfcrawl import config
from xfcrawl.errors import XfError
from xfcrawl.failures import Backoff, Failure, RetryPolicy
from xfcrawl.layout import STORE_DIR_NAME, ItemDirName, StoreLayout
from xfcrawl.link_index import LinkIndex
from xfcrawl.plan import CrawlPlan
from xfcrawl.store import PageStore, merge_dirs
from xfcrawl.urls import INDEX_PAGE, ItemRef, Kind, PageRef

_OLD_STORE_DIR = ".state"
_OLD_PAGE_DIRS = ("page-store", "cache")
_MOVING_PREFIX = "_moving."
_OLD_ERRORS_LOG = "errors.jsonl"
_OLD_FAILURE_FILE = re.compile(
    r"^(?:page-(?P<page>\d+)|index)\.(?P<ext>failed|failed\.json|gone\.json|redirect\.json)$"
)
_HTTP_STATUS = re.compile(r"HTTP (\d+)")


def old_layout(directory: Path) -> str | None:
    """What is out of date when ``directory`` holds a store from an earlier version, or None."""
    layout = StoreLayout.in_directory(directory)
    if (directory / _OLD_STORE_DIR).is_dir():
        return f"a {_OLD_STORE_DIR}/ folder from an earlier version"
    if (layout.root / _OLD_ERRORS_LOG).exists():
        return f"{STORE_DIR_NAME}/{_OLD_ERRORS_LOG} from an earlier version"
    if not layout.threads_dir.is_dir():
        return None
    entries = list(layout.threads_dir.iterdir())  # all of them: listing order varies by filesystem
    if any(entry.name.startswith(_MOVING_PREFIX) for entry in entries):
        return "an unfinished migration"
    if any(_is_flat_thread_dir(entry) for entry in entries):
        return "thread folders not yet grouped by 1000"
    return None


def refuse_old_layout(directory: Path) -> None:
    reason = old_layout(directory)
    if reason is not None:
        raise XfError(f"this crawl store has {reason}. Back it up, then run: uv run xf migrate")


@dataclass
class MigrationReport:
    moved_store: int = 0
    grouped_threads: int = 0
    converted_errors_log: int = 0
    converted_failure_files: int = 0
    named_folders: int = 0

    def lines(self) -> list[str]:
        counts = [
            (self.moved_store, f"{_OLD_STORE_DIR}/ -> {STORE_DIR_NAME}/"),
            (self.grouped_threads, "threads moved into groups of 1000"),
            (self.converted_errors_log, f"{_OLD_ERRORS_LOG} entries -> .failed.jsonl"),
            (self.converted_failure_files, "old failure files -> .failed.jsonl"),
            (self.named_folders, "folders named after the site's URLs"),
        ]
        done = [f"{count:>8,}  {what}" for count, what in counts if count]
        return [*done, "Done."] if done else ["Already up to date; nothing to migrate."]


def migrate(directory: Path) -> MigrationReport:
    layout = StoreLayout.in_directory(directory)
    if not layout.root.is_dir() and not (directory / _OLD_STORE_DIR).is_dir():
        raise XfError(f"no {STORE_DIR_NAME} (or {_OLD_STORE_DIR}) in {directory}; nothing to migrate")
    report = MigrationReport()
    report.moved_store = _move_old_store_dir(directory, layout)
    report.grouped_threads = _group_flat_threads(layout)
    store = PageStore.open(layout)
    report.converted_errors_log = _convert_errors_log(layout, store)
    report.converted_failure_files = _convert_old_failure_files(layout, store)
    report.named_folders = _name_folders(layout, store)
    return report


# -- the steps ---------------------------------------------------------------------------------------
def _move_old_store_dir(directory: Path, layout: StoreLayout) -> int:
    old = directory / _OLD_STORE_DIR
    if layout.root.exists() or not old.is_dir():
        return 0
    for name in _OLD_PAGE_DIRS:
        if (old / name).is_dir():
            (old / name).rename(old / layout.pages_dir.name)
            break
    old.rename(layout.root)
    return 1


def _group_flat_threads(layout: StoreLayout) -> int:
    threads = layout.threads_dir
    if not threads.is_dir():
        return 0
    moving = []
    for entry in list(threads.iterdir()):
        match = re.fullmatch(rf"(?:{re.escape(_MOVING_PREFIX)})?(\d+)", entry.name)
        if match is None or not (entry.name.startswith(_MOVING_PREFIX) or _is_flat_thread_dir(entry)):
            continue
        aside = threads / f"{_MOVING_PREFIX}{match.group(1)}"
        if entry != aside:
            entry.rename(aside)  # out of the way first: "12" may also be the name of a group
        moving.append((int(match.group(1)), aside))
    for item_id, aside in moving:
        target = layout.item_parent(ItemRef(Kind.THREAD, item_id)) / str(item_id)
        if target.exists():
            merge_dirs(aside, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            aside.rename(target)
    return len(moving)


def _convert_errors_log(layout: StoreLayout, store: PageStore) -> int:
    log = layout.root / _OLD_ERRORS_LOG
    if not log.exists():
        return 0
    converted = 0
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
            ref = _ref_from_key(record["key"])
            when = datetime.fromisoformat(record["time"])
        except (ValueError, KeyError):
            continue
        if store.is_stored(ref):
            continue
        store.record_failure(ref, _old_failure(record, when), None)
        converted += 1
    log.unlink()
    return converted


def _convert_old_failure_files(layout: StoreLayout, store: PageStore) -> int:
    converted = 0
    for path in sorted(layout.pages_dir.rglob("*")):
        match = _OLD_FAILURE_FILE.match(path.name)
        ref = _ref_of_file(layout, path, match)
        if match is None or ref is None:
            continue
        if not store.is_stored(ref):
            for failure in _read_old_failure_file(path, match["ext"]):
                store.record_failure(ref, failure, None)
        path.unlink()
        converted += 1
    return converted


def _name_folders(layout: StoreLayout, store: PageStore) -> int:
    loaded = config.load(layout)
    if loaded is None:
        return 0
    backoff = Backoff(RetryPolicy(), datetime.now(timezone.utc), ignore=True)
    plan = CrawlPlan(loaded.start_url, store, LinkIndex(layout.link_index), backoff)
    plan.rebuild()
    named = 0
    for item in plan.known_items():
        before = store.item_dir(item)
        slug = plan.slug(item)
        if before is None or slug is None or not before.is_dir():
            continue
        store.name_folder(item, slug)
        named += store.item_dir(item) != before
    return named


# -- reading the old formats ---------------------------------------------------------------------------
def _is_flat_thread_dir(path: Path) -> bool:
    """A thread folder straight under threads/, as earlier versions kept them: a numbered folder of files."""
    if not (path.is_dir() and path.name.isdigit()):
        return False
    first = next(path.iterdir(), None)
    return first is not None and first.is_file()


def _ref_from_key(key: str) -> PageRef:
    kind, item_id, page = key.split(":")
    return PageRef(Kind(kind), int(item_id), int(page))


def _ref_of_file(layout: StoreLayout, path: Path, match: re.Match[str] | None) -> PageRef | None:
    if match is None:
        return None
    if path.parent == layout.pages_dir:
        return INDEX_PAGE
    name = ItemDirName.parse(path.parent.name)
    if name is None:
        return None
    kind = Kind.THREAD if layout.threads_dir in path.parents else Kind.FORUM
    return PageRef(kind, name.item_id, int(match["page"] or 1))


def _old_failure(record: dict[str, object], when: datetime) -> Failure:
    error = str(record.get("error", ""))
    status = _HTTP_STATUS.match(error)
    if record.get("status") == "redirect":
        reason = "redirect"
    elif "robots" in error:
        reason = "robots"
    elif status:
        reason = f"http-{status.group(1)}"
    elif "page" in error:
        reason = "error-page"
    else:
        reason = "network"
    detail = str(record.get("target") or ("" if status else error))
    return Failure(when, reason, str(record.get("url", "")), detail)


def _read_old_failure_file(path: Path, ext: str) -> list[Failure]:
    text = path.read_text(encoding="utf-8")
    if ext == "failed":  # time<TAB>reason<TAB>detail, one line per attempt
        failures = []
        for line in text.splitlines():
            parts = line.split("\t")
            try:
                when = datetime.fromisoformat(parts[0])
            except ValueError:
                continue
            failures.append(Failure(when, parts[1] if len(parts) > 1 else "network", "",
                                    parts[2] if len(parts) > 2 else ""))
        return failures
    try:  # one JSON record per page
        record = json.loads(text)
        when = datetime.fromisoformat(record["time"])
    except (ValueError, KeyError):
        return []
    return [_old_failure(record, when)] * max(int(record.get("attempts", 0)), 1)


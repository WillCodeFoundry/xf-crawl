"""
xf-migrate — bring a .crawl-store made by an earlier version up to date.

    uv run xf-migrate        (in the directory that holds .crawl-store, or .state)

xf-crawl and xf-mine only understand the current layout and refuse to run on
an old one; this command converts it. Back up first:

    tar czf crawl-store-backup.tgz .crawl-store

It never downloads anything and is safe to run more than once: steps with
nothing to do are skipped. What it converts:

  1. .state/ (pages in cache/ or page-store/)  ->  .crawl-store/ (pages/)
  2. threads/<id>/                           ->  threads/<id // 1000>/<id>/
  3. .crawl-store/errors.jsonl               ->  per-page page-NNNN.failed.jsonl
  4. page-NNNN.failed (tab-separated) and page-NNNN.{failed,gone,redirect}.json
                                             ->  page-NNNN.failed.jsonl
  5. folders named just <id>                 ->  <slug>.<id>, like the site's URLs
     (the words come from links in the stored pages)
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

from .common import (
    CONFIG_PATH,
    DIR_RE,
    PAGES_DIR,
    STORE_DIR,
    Plan,
    _dirs,
    _merge_dirs,
    append_failure,
    item_dir,
    scan_store,
    slug_of,
    split_key,
    store_path,
)

LEGACY_MARKER_RE = re.compile(r"^(page-(\d+)|index)\.(failed|failed\.json|gone\.json|redirect\.json)$")


def _reason(status, err):
    m = re.match(r"HTTP (\d+)", err or "")
    reason = ("redirect" if status == "redirect" else
              "robots" if "robots" in (err or "") else
              f"http-{m.group(1)}" if m else
              "error-page" if "page" in (err or "") else "network")
    return reason, bool(m)


def step_state_dir():
    if os.path.exists(STORE_DIR) or not os.path.isdir(".state"):
        return 0
    for old in ("page-store", "cache"):
        if os.path.isdir(os.path.join(".state", old)):
            os.rename(os.path.join(".state", old), os.path.join(".state", "pages"))
            break
    os.rename(".state", STORE_DIR)
    return 1


def step_flat_threads():
    base = os.path.join(PAGES_DIR, "threads")
    if not os.path.isdir(base):
        return 0
    moving = []
    for d in list(os.scandir(base)):
        m = re.fullmatch(r"(?:_moving\.)?(\d+)", d.name)
        if not (m and d.is_dir()):
            continue
        if d.name.startswith("_moving.") or any(e.is_file() for e in os.scandir(d.path)):
            tmp = os.path.join(base, f"_moving.{m.group(1)}")
            if d.path != tmp:
                os.rename(d.path, tmp)   # out of the way first: "12" may also be a group name
            moving.append((int(m.group(1)), tmp))
    for item_id, tmp in moving:
        dest = os.path.join(base, str(item_id // 1000), str(item_id))
        if os.path.exists(dest):
            _merge_dirs(tmp, dest)
        else:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.rename(tmp, dest)
    return len(moving)


def step_errors_log():
    old_log = os.path.join(STORE_DIR, "errors.jsonl")
    if not os.path.exists(old_log):
        return 0
    n = 0
    with open(old_log, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
                kind, item_id, page = split_key(rec["key"])
                when = datetime.fromisoformat(rec["time"])
            except (ValueError, KeyError):
                continue
            if os.path.exists(store_path(kind, item_id, page)):
                continue
            err = rec.get("error", "")
            reason, is_http = _reason(rec.get("status"), err)
            append_failure(kind, item_id, page, reason, rec.get("url", ""),
                           rec.get("target") or ("" if is_http else err), when)
            n += 1
    os.remove(old_log)
    return n


def step_legacy_markers():
    dirs = [("index", 0, PAGES_DIR)] + [(k, i, p) for (k, i), p in _dirs.items()]
    n = 0
    for kind, item_id, path in dirs:
        if not os.path.isdir(path):
            continue
        for f in list(os.scandir(path)):
            m = LEGACY_MARKER_RE.match(f.name)
            if not m or (kind == "index") != (m.group(2) is None):
                continue
            page, ext = int(m.group(2) or 1), m.group(3)
            if os.path.exists(store_path(kind, item_id, page)):
                os.remove(f.path)            # the page was fetched fine later
                continue
            try:
                with open(f.path, encoding="utf-8") as fh:
                    if ext == "failed":      # time<TAB>reason<TAB>detail
                        for line in fh:
                            parts = line.rstrip("\n").split("\t")
                            try:
                                when = datetime.fromisoformat(parts[0])
                            except ValueError:
                                continue
                            append_failure(kind, item_id, page, parts[1] if len(parts) > 1 else "network",
                                           "", parts[2] if len(parts) > 2 else "", when)
                    else:                    # one JSON record
                        rec = json.load(fh)
                        err = rec.get("error", "")
                        reason, is_http = _reason(rec.get("status"), err)
                        detail = rec.get("target") or ("" if is_http else err)
                        for _ in range(max(rec.get("attempts", 0), 1)):
                            append_failure(kind, item_id, page, reason, rec.get("url", ""), detail,
                                           datetime.fromisoformat(rec["time"]))
            except (OSError, ValueError, KeyError):
                pass
            os.remove(f.path)
            n += 1
    return n


def step_name_folders():
    if not os.path.exists(CONFIG_PATH):
        return 0
    with open(CONFIG_PATH, encoding="utf-8") as f:
        start_url = json.load(f)["start_url"]
    plan = Plan(start_url)
    plan.rebuild()
    n = 0
    for (kind, item_id), it in plan.items.items():
        cur = _dirs.get((kind, item_id))
        slug = slug_of(it["base"])
        if slug and cur and os.path.isdir(cur) and DIR_RE.match(os.path.basename(cur)).group(1) is None:
            item_dir(kind, item_id, slug)
            n += 1
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args()
    if not os.path.isdir(STORE_DIR) and not os.path.isdir(".state"):
        sys.exit(f"No {STORE_DIR} (or .state) in {os.getcwd()}; nothing to migrate.")

    done = {}
    done[".state/ -> .crawl-store/"] = step_state_dir()
    done["threads moved into groups of 1000"] = step_flat_threads()
    scan_store()   # find every item's folder for the steps below
    done["errors.jsonl entries -> .failed.jsonl"] = step_errors_log()
    done["old failure files -> .failed.jsonl"] = step_legacy_markers()
    done["folders named after the site's URLs"] = step_name_folders()

    if not any(done.values()):
        print("Already up to date; nothing to migrate.")
        return
    for what, n in done.items():
        if n:
            print(f"{n:>8,}  {what}")
    print("Done.")


if __name__ == "__main__":
    main()

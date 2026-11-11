"""xf mine: statistics about the crawl here, from the stored pages only."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from xfcrawl import migration
from xfcrawl.layout import StoreLayout
from xfcrawl.mining import stats
from xfcrawl.mining.stats import Stats
from xfcrawl.stored_crawl import open_stored_crawl

_INCOMPLETE_SHOWN = 10


def add_parser(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subparsers.add_parser(
        "mine",
        help="statistics about the crawl here (no network)",
        description="Forums, threads and posts in ./.crawl-store, and how complete the download is.",
    )
    parser.add_argument("--json", action="store_true", help="print JSON instead of a report")
    parser.set_defaults(func=main)


def main(args: argparse.Namespace) -> int:
    """Composition root: the store here, now."""
    directory = Path.cwd()
    migration.refuse_old_layout(directory)
    crawl = open_stored_crawl(StoreLayout.in_directory(directory), datetime.now(timezone.utc))
    result = stats.compute(crawl.config.start_url, crawl.store, crawl.plan)
    print(json.dumps(result.to_json(), indent=2, ensure_ascii=False) if args.json else "\n".join(report(result)))
    return 0


def report(s: Stats) -> list[str]:
    lines = [
        f"Forum: {s.forum}",
        "",
        f"  Forums                     {s.forums:>8,}",
        f"  Threads known              {s.threads_known:>8,}",
        f"  Threads downloaded         {s.threads_downloaded:>8,}   "
        f"({s.threads_complete:,} complete, {s.threads_incomplete:,} missing pages)",
        f"  Thread pages downloaded    {s.thread_pages_stored:>8,}",
        f"  Posts in downloaded pages  {s.posts_stored:>8,}",
        f"  Replies per forum listings {s.replies_per_listings:>8,}"
        + ("  (some rounded, e.g. 1.2K)" if s.replies_approx else ""),
        "",
        f"  Crawl: {s.pages_stored:,} pages stored, {s.pages_to_crawl:,} still to crawl",
    ]
    if s.pages_failed:
        lines.append("  Failed pages: " + ", ".join(f"{n:,} {reason}" for reason, n in s.pages_failed.items())
                     + f"  ({s.pages_failed_waiting_retry:,} waiting for their next retry)")
    if s.pages_to_crawl:
        lines.append("  (crawl not finished: the numbers cover what is downloaded so far)")
    if s.per_forum:
        width = max(len(forum.title) for forum in s.per_forum)
        lines += ["", "  Threads per forum:", f"    {'Forum':<{width}}  {'Listed':>7}  {'Downloaded':>10}"]
        lines += [f"    {f.title:<{width}}  {f.threads_listed:>7,}  {f.threads_downloaded:>10,}" for f in s.per_forum]
    if s.incomplete_threads:
        lines += ["", f"  Threads with missing pages (first {_INCOMPLETE_SHOWN} of {len(s.incomplete_threads)}):"]
        lines += [f"    thread {t.thread_id}: {t.pages_stored} of {t.pages_total} pages"
                  for t in s.incomplete_threads[:_INCOMPLETE_SHOWN]]
    return lines

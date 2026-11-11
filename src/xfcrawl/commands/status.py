"""xf status: how far the crawl here has got, by stage and by forum, and how long the rest may take."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from xfcrawl import migration, progress
from xfcrawl.fetching import DEFAULT_DELAY, DEFAULT_JITTER
from xfcrawl.layout import StoreLayout
from xfcrawl.progress import Estimate, Progress
from xfcrawl.stored_crawl import open_stored_crawl
from xfcrawl.urls import Stage

_DEFAULT_PACE = DEFAULT_DELAY + DEFAULT_JITTER / 2
_STAGE_NOTES = {Stage.THREAD_OTHER_PAGES: "so far"}
_SECONDS_PER = (("day", 86400), ("hour", 3600), ("minute", 60))


def add_parser(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subparsers.add_parser(
        "status",
        help="how far the crawl here has got (no network)",
        description=(
            "How far the crawl in ./.crawl-store has got: pages stored and known at each stage, failures, and "
            "an estimate of the time left. Quick however big the crawl: it reads no pages."
        ),
    )
    parser.add_argument("--forums", action="store_true", help="add a line per forum")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a report")
    parser.add_argument("--seconds-per-page", type=float, default=_DEFAULT_PACE, metavar="S",
                        help=f"the pace to estimate with (default {_DEFAULT_PACE}, xf crawl's default)")
    parser.set_defaults(func=main)


def main(args: argparse.Namespace) -> int:
    """Composition root: the store here, now."""
    directory = Path.cwd()
    migration.refuse_old_layout(directory)
    crawl = open_stored_crawl(StoreLayout.in_directory(directory), datetime.now(timezone.utc))
    result = progress.compute(crawl.config.start_url, crawl.store, crawl.plan, args.seconds_per_page)
    waiting = len(crawl.plan.waiting())
    if args.json:
        print(json.dumps({**result.to_json(), "waiting_for_retry": waiting}, indent=2, ensure_ascii=False))
    else:
        print("\n".join(report(result, waiting, forums=args.forums)))
    return 0


def report(result: Progress, waiting: int, *, forums: bool) -> list[str]:
    labels = [f"{n}  {s.stage.describe()}" + (f" ({_STAGE_NOTES[s.stage]})" if s.stage in _STAGE_NOTES else "")
              for n, s in enumerate(result.stages, 1)]
    width = max(len(label) for label in labels)
    lines = [f"Crawl of {result.start_url}", "",
             f"  {'Stage':<{width}}{'Stored':>12}{'Known':>12}{'Done':>7}{'Failed':>9}{'To fetch':>11}"]
    for label, stage in zip(labels, result.stages, strict=True):
        lines.append(f"  {label:<{width}}{stage.stored:>12,}{stage.known:>12,}"
                     f"{_percent(stage.stored, stage.known):>7}{stage.failing:>9,}{stage.to_fetch:>11,}")
    current = result.current
    lines.append("")
    lines.append("  Complete." if current is None
                 else f"  Now on stage {result.stages.index(current) + 1}: {current.stage.describe()}.")
    lines.append(f"  Threads known: {result.threads_known:,}; the longest has {result.longest_thread:,} pages.")
    if result.estimate is not None and result.estimate.pages_left:
        lines.append(_estimate_line(result.estimate))
    if waiting:
        lines.append(f"  {waiting:,} failed pages are waiting to be retried; xf crawl --retry-failed tries them now.")
    if forums and result.forums:
        lines += ["", *forum_lines(result)]
    return lines


def forum_lines(result: Progress) -> list[str]:
    width = max(len(forum.title) for forum in result.forums)
    lines = [f"  {'Forum':<{width}}{'Threads':>11}{'Opened':>9}{'Pages stored':>15}{'of known':>11}{'Done':>7}"]
    for f in result.forums:
        lines.append(f"  {f.title:<{width}}{f.threads:>11,}{_percent(f.first_pages_stored, f.threads):>9}"
                     f"{f.thread_pages_stored:>15,}{f.thread_pages_known:>11,}"
                     f"{_percent(f.thread_pages_stored, f.thread_pages_known):>7}")
    return lines


def _estimate_line(estimate: Estimate) -> str:
    return (f"  About {estimate.pages_left:,} pages left (the threads' later pages estimated from those seen so "
            f"far): {duration(estimate.seconds)} at {estimate.seconds_per_page:g} s per page.")


def duration(seconds: float) -> str:
    """``about 3 days 4 hours``: the two largest units, rounded down."""
    parts: list[str] = []
    remaining = int(seconds)
    for unit, size in _SECONDS_PER:
        count, remaining = divmod(remaining, size)
        if count or parts:
            parts.append(f"{count} {unit}{'s' if count != 1 else ''}")
        if len(parts) == 2:
            break
    return f"about {' '.join(parts)}" if parts else "under a minute"


def _percent(part: int, whole: int) -> str:
    return f"{part * 100 // whole}%" if whole else "-"

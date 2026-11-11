"""xf pending: the pages still to fetch, worked out from the store alone; nothing is downloaded."""

import argparse
from datetime import datetime, timezone
from pathlib import Path

from xfcrawl import migration
from xfcrawl.layout import StoreLayout
from xfcrawl.stored_crawl import open_stored_crawl


def add_parser(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subparsers.add_parser(
        "pending",
        help="list the pages still to fetch (no network)",
        description="List the pages xf crawl would fetch next, one per line: the page, then its URL.",
    )
    parser.set_defaults(func=main)


def main(_args: argparse.Namespace) -> int:
    """Composition root: the store here, now."""
    directory = Path.cwd()
    migration.refuse_old_layout(directory)
    plan = open_stored_crawl(StoreLayout.in_directory(directory), datetime.now(timezone.utc)).plan
    print("\n".join(f"{ref.key}\t{plan.url_of(ref)}" for ref in plan.to_fetch()))
    return 0

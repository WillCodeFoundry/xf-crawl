"""xf migrate: convert a crawl store made by an earlier version; nothing is downloaded."""

import argparse
from pathlib import Path

from xfcrawl import migration


def add_parser(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subparsers.add_parser(
        "migrate",
        help="convert a crawl store made by an earlier version",
        description=(
            "Convert ./.crawl-store (or an older ./.state) to the current layout. Back it up first: "
            "tar czf crawl-store-backup.tgz .crawl-store. Safe to run more than once."
        ),
    )
    parser.set_defaults(func=main)


def main(_args: argparse.Namespace) -> int:
    """Composition root: the store here."""
    print("\n".join(migration.migrate(Path.cwd()).lines()))
    return 0

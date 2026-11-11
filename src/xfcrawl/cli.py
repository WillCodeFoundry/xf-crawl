"""The xf command: one subcommand per module in xfcrawl.commands.

A command prints its result. Progress goes to the ``xfcrawl`` logger from the module where it happens, and
main shows it on stderr, each line stamped with the time.
"""

import argparse
import logging
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager

from xfcrawl.commands import crawl, migrate, mine, pending, tunnelbear
from xfcrawl.errors import XfError

COMMANDS = (crawl, pending, mine, migrate, tunnelbear)
_LOGGER = logging.getLogger("xfcrawl")
_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xf", description="Crawl a XenForo forum, politely and resumably.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in COMMANDS:
        command.add_parser(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Errors and interruptions are reported here only."""
    args = build_parser().parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.func
    with progress_on_stderr():
        try:
            return handler(args)
        except XfError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("Interrupted. Everything downloaded is kept; run it again to carry on.", file=sys.stderr)
            return _INTERRUPTED


@contextmanager
def progress_on_stderr() -> Iterator[None]:
    """Show progress on stderr while one command runs; removed afterwards, so nothing carries over."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S"))
    previous_level = _LOGGER.level
    _LOGGER.addHandler(handler)
    _LOGGER.setLevel(logging.INFO)
    try:
        yield
    finally:
        _LOGGER.removeHandler(handler)
        _LOGGER.setLevel(previous_level)


if __name__ == "__main__":
    sys.exit(main())

"""Writing a file in one step: written beside it under a fresh hidden name, then renamed over it.

A crash leaves either the old file or the new one, never half of one. A partial file left by a crash is
named ``.<name>.<random>``; PageStore removes them when it opens.
"""

import os
import tempfile
from pathlib import Path

PARTIAL_PREFIX = "."


def atomic_write(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, partial_name = tempfile.mkstemp(dir=target.parent, prefix=f"{PARTIAL_PREFIX}{target.name}.")
    partial = Path(partial_name)
    try:
        with os.fdopen(descriptor, "wb") as partial_file:
            partial_file.write(content)
            partial_file.flush()
            os.fsync(partial_file.fileno())
        partial.chmod(0o644)
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)


def append_line(target: Path, line: str) -> None:
    """Append one line and flush it to disk; a crash can cut only that line short."""
    append_lines(target, [line])


def append_lines(target: Path, lines: list[str]) -> None:
    """Append lines and flush them to disk once; a crash can cut only the last line written short."""
    if not lines:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as file:
        file.writelines(lines)
        file.flush()
        os.fsync(file.fileno())

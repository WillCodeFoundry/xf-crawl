"""Failed fetches: what one looks like on disk, and when a failing page may be tried again.

A page's failures are kept as JSON lines, one per attempt, appended in order:

    {"time": "2026-11-09T16:16:05+00:00", "reason": "http-503", "url": "http://x/threads/a.12/"}
    {"time": "...", "reason": "redirect", "url": "http://x/threads/a.12/page-3", "detail": "http://x/threads/a.12/page-2"}

Reasons: ``http-<status>``, ``refused`` (a 403 from in front of the forum), ``timeout``, ``network``,
``error-page``, ``redirect``, ``robots``.

A redirect to another thread or forum (``detail``) means the page's thread was merged into it, or moved: that
is final, and the page is never tried again unless asked (``--retry-failed``). A redirect within the same
thread, as for a page past its end, is retried like any page that looks gone, as the thread may grow.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from xfcrawl.urls import ItemRef, parse_page_url

PERMANENT_REASONS = frozenset({"http-401", "http-403", "http-404", "http-410", "redirect", "robots"})


@dataclass(frozen=True)
class Failure:
    time: datetime
    reason: str
    url: str = ""
    detail: str = ""

    @property
    def merged_into(self) -> ItemRef | None:
        """The thread or forum this page's own was merged into or moved to, if this was such a redirect."""
        if self.reason != "redirect":
            return None
        asked, went = parse_page_url(self.url), parse_page_url(self.detail)
        if asked is None or went is None or asked.ref.item == went.ref.item:
            return None
        return went.ref.item

    @property
    def is_permanent(self) -> bool:
        """Whether the page looks gone for good rather than temporarily unavailable."""
        return self.reason in PERMANENT_REASONS

    def to_json_line(self) -> str:
        record = {"time": self.time.isoformat(), "reason": self.reason}
        if self.url:
            record["url"] = self.url
        if self.detail:
            record["detail"] = self.detail
        return json.dumps(record, ensure_ascii=False) + "\n"


def parse_failures(text: str) -> list[Failure]:
    """The failures in a failure file, oldest first. A line cut short by a crash is skipped."""
    failures = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
            failures.append(
                Failure(
                    time=datetime.fromisoformat(record["time"]),
                    reason=record["reason"],
                    url=record.get("url", ""),
                    detail=record.get("detail", ""),
                )
            )
        except (ValueError, KeyError, TypeError):
            continue
    return failures


@dataclass(frozen=True)
class RetryPolicy:
    """After N failures a page waits ``base * 2**(N-1)`` from its last failure, up to ``longest``.

    ``base`` is ``temporary`` for 5xx, timeouts and error pages, and ``permanent`` for what looks gone for
    good, so a page that keeps failing is tried less and less often, and never given up on.
    """

    temporary: timedelta = timedelta(minutes=15)
    permanent: timedelta = timedelta(days=1)
    longest: timedelta = timedelta(days=30)
    _MAX_DOUBLINGS = 20  # 2**20 * 15 minutes is past any sensible `longest`; more would overflow timedelta

    def wait_after(self, failures: list[Failure]) -> timedelta:
        last = failures[-1]
        base = self.permanent if last.is_permanent else self.temporary
        return min(base * (1 << min(len(failures) - 1, self._MAX_DOUBLINGS)), self.longest)

    def next_try(self, failures: list[Failure]) -> datetime:
        return failures[-1].time + self.wait_after(failures)


@dataclass(frozen=True)
class Backoff:
    """Whether a page that failed is still waiting, as of ``now``; never, with ``ignore``."""

    policy: RetryPolicy
    now: datetime
    ignore: bool = False

    def settled(self, failures: list[Failure]) -> bool:
        """Whether the page is never to be tried again: its thread was merged into another, or moved."""
        return bool(failures) and not self.ignore and failures[-1].merged_into is not None

    def waiting_until(self, failures: list[Failure]) -> datetime | None:
        """When the page may be tried again, or None if it may be now."""
        if not failures or self.ignore:
            return None
        next_try = self.policy.next_try(failures)
        return next_try if self.now < next_try else None

"""What the crawl does when pages keep failing: a ladder of changes, and when to stop.

Each page failing in a row for a reason that may lie with the crawler rather than the page (``trouble``:
429, a 403 from in front of the forum, 5xx, timeouts, error pages...) climbs one step: a longer pause, then
a new proxy server, then a new visitor (browser, and none of the site's cookies). One page that comes
through, or a normal answer such as a deleted thread, goes back to the bottom. A page that fails is not
asked again at once: it waits its turn in the plan (failures.RetryPolicy).

When the top step has been tried ``TOP_STEP_TRIES`` times in a row and pages still fail, the crawl stops:
the trouble isn't one server or one visitor, and carrying on would only spend the proxy's data.
"""

from collections import Counter
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Step:
    pause: float
    """Seconds to wait before the next request."""
    new_server: bool
    new_visitor: bool


LADDER: tuple[Step, ...] = (
    Step(30, new_server=False, new_visitor=False),
    Step(60, new_server=True, new_visitor=False),
    Step(120, new_server=True, new_visitor=True),
    Step(300, new_server=True, new_visitor=True),
    Step(600, new_server=True, new_visitor=True),
    Step(900, new_server=True, new_visitor=True),
)
"""The steps, one per page failing in a row; the last is repeated. Change them here."""
TOP_STEP_TRIES = 3
"""How many times the last step is tried, each followed by another failure, before the crawl stops."""
LONGEST_PAUSE = 900.0
"""No pause is longer, not even one a site asks for with Retry-After."""
NORMAL_ANSWERS = frozenset({"redirect", "http-404", "http-410", "http-403", "robots"})
"""Failures that are about the page, not the crawler: it moved, was deleted, is private, or robots.txt
forbids it. They show the site answering normally."""


def is_trouble(reason: str) -> bool:
    return reason not in NORMAL_ANSWERS


@dataclass
class Trouble:
    """Pages failing in a row, and what has been tried since the last one that came through."""

    longest_pause: float = LONGEST_PAUSE
    ladder: tuple[Step, ...] = LADDER
    top_step_tries: int = TOP_STEP_TRIES
    in_a_row: int = 0
    servers: int = 0
    visitors: int = 0
    since: float = 0.0
    """When the first of the pages failing in a row failed, on the crawl's clock."""
    reasons: Counter[str] = field(default_factory=Counter)

    def recovered(self) -> None:
        self.in_a_row = self.servers = self.visitors = 0
        self.reasons.clear()

    def failed(self, reason: str, now: float, retry_after: float | None = None) -> Step | None:
        """The step to take after another page failed, or None to stop."""
        if self.in_a_row == 0:
            self.since = now
        self.in_a_row += 1
        self.reasons[reason] += 1
        if self.in_a_row >= len(self.ladder) + self.top_step_tries:
            return None
        step = self.ladder[min(self.in_a_row, len(self.ladder)) - 1]
        pause = min(max(step.pause, retry_after or 0.0), self.longest_pause)
        self.servers += step.new_server
        self.visitors += step.new_visitor
        return Step(pause, step.new_server, step.new_visitor)

    def why_stop(self, now: float) -> str:
        reasons = ", ".join(f"{reason} x{count}" for reason, count in self.reasons.most_common())
        return (f"no page has come through for {_duration(now - self.since)}, {self.in_a_row} pages in a row, "
                f"despite pauses, {self.servers} new servers and {self.visitors} new visitors; "
                f"the answers: {reasons}. Is the site down, or turning the crawler away?")


def _duration(seconds: float) -> str:
    return f"{seconds / 60:.0f} min" if seconds >= 90 else f"{seconds:.0f}s"

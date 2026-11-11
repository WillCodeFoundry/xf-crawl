"""Reading XenForo 2 pages for their content: a forum's title and thread list, a thread's posts.

The only module that knows XenForo's HTML beyond links (those are in ``xenforo``).
"""

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag

_COUNT = re.compile(r"^([\d.]+)([KkMm]?)$")
_COUNT_SEPARATORS = re.compile(r"[,\s]")  # \s covers no-break spaces too
_ABBREVIATIONS = {"k": 1_000, "m": 1_000_000}


@dataclass(frozen=True)
class Count:
    value: int
    approximate: bool
    """True when XenForo showed it rounded, such as 1.2K."""


@dataclass(frozen=True)
class ListedThread:
    href: str
    title: str
    replies: Count | None


def parse_count(text: str) -> Count | None:
    """``1,234`` -> 1234; ``1.2K`` -> about 1200; None for anything that isn't a count."""
    match = _COUNT.match(_COUNT_SEPARATORS.sub("", text))
    if match is None:
        return None
    multiplier = _ABBREVIATIONS.get(match.group(2).lower(), 1)
    return Count(int(float(match.group(1)) * multiplier), approximate=multiplier != 1)


def parse(page_html: str) -> BeautifulSoup:
    return BeautifulSoup(page_html, "html.parser")


def title(page: BeautifulSoup) -> str | None:
    heading = page.select_one("h1.p-title-value")
    return heading.get_text(" ", strip=True) if heading else None


def listed_threads(page: BeautifulSoup) -> list[ListedThread]:
    """The threads a forum page lists, stickies included, with the reply count each shows."""
    threads = []
    for item in page.select(".structItem--thread"):
        title_box = item.select_one(".structItem-title")
        links = title_box.find_all("a") if title_box else []
        if title_box is None or not links:
            continue
        primary = title_box.select_one("a[data-tp-primary]")
        link = primary if isinstance(primary, Tag) else links[-1]  # earlier links are prefixes and labels
        counts = item.select(".structItem-cell--meta dl dd")  # replies, then views
        threads.append(ListedThread(
            href=str(link["href"]),
            title=link.get_text(strip=True),
            replies=parse_count(counts[0].get_text()) if counts else None,
        ))
    return threads


def post_count(page: BeautifulSoup) -> int:
    return len(page.select("article.message--post"))

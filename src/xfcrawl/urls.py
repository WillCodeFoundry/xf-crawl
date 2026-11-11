"""XenForo's URL rules: which URLs are forum or thread pages, and how pages and slugs are spelled.

The only module that parses or builds a XenForo page URL. A page is named by a ``PageRef``: its kind, the
forum's or thread's number, and the page number. The number is the identity; the words in the URL (the slug)
are for people and change when a forum or thread is renamed.
"""

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import unquote


class Kind(str, Enum):
    INDEX = "index"
    FORUM = "forum"
    THREAD = "thread"


class Stage(int, Enum):
    """The crawl goes level by level: everything of one stage before anything of the next."""

    FORUM_LIST = 0
    THREAD_LISTS = 1
    THREAD_FIRST_PAGES = 2
    THREAD_OTHER_PAGES = 3

    def describe(self) -> str:
        return {
            Stage.FORUM_LIST: "the forum list",
            Stage.THREAD_LISTS: "every forum's thread list",
            Stage.THREAD_FIRST_PAGES: "every thread's first page",
            Stage.THREAD_OTHER_PAGES: "the threads' other pages",
        }[self]

# .../threads/some-title.123/  .../threads/some-title.123/page-4  .../forums/123/
# and the same in the query string when friendly URLs are off: index.php?threads/some-title.123/page-4
_PAGE_URL = re.compile(r"^(.*?(threads|forums)/(?:[^/?#&]*\.)?(\d+)/)(?:page-(\d+))?$")
_SLUG = re.compile(r"(?:threads|forums)/(?:([^/?#&]*)\.)?\d+/$")
_SLUG_MAX_LENGTH = 120


@dataclass(frozen=True)
class ItemRef:
    """A forum or thread, by its number."""

    kind: Kind
    item_id: int

    def page(self, page: int) -> "PageRef":
        return PageRef(self.kind, self.item_id, page)


@dataclass(frozen=True)
class PageRef:
    kind: Kind
    item_id: int
    page: int

    @property
    def item(self) -> ItemRef:
        return ItemRef(self.kind, self.item_id)

    @property
    def key(self) -> str:
        """How the page is named in logs and listings: ``thread:123:4``."""
        return f"{self.kind.value}:{self.item_id}:{self.page}"

    @property
    def stage(self) -> Stage:
        if self.kind is Kind.INDEX:
            return Stage.FORUM_LIST
        if self.kind is Kind.FORUM:
            return Stage.THREAD_LISTS
        return Stage.THREAD_FIRST_PAGES if self.page == 1 else Stage.THREAD_OTHER_PAGES

    def crawl_order(self) -> tuple[int, int, int]:
        """Stage by stage (see Stage); within one, by forum or thread number, then page.

        Thread pages can't link to forums, so every forum and every thread is known before the first
        thread is fetched. Each thread's first page says how many pages it has, so the whole size of the
        crawl is known before its last stage starts."""
        return (self.stage, self.item_id, self.page)


INDEX_PAGE = PageRef(Kind.INDEX, 0, 1)


@dataclass(frozen=True)
class PageLink:
    """A URL found to be a forum or thread page: which page, and the URL of the item's first page."""

    ref: PageRef
    first_page_url: str

    @property
    def slug(self) -> str | None:
        return slug_of(self.first_page_url)


def normalize(url: str) -> str:
    """Drop the #fragment and ?filters, but keep a XenForo route in the query string."""
    url = url.split("#", 1)[0]
    if "?" not in url:
        return url
    path, query = url.split("?", 1)
    route = query.split("&", 1)[0]
    return f"{path}?{route}" if route.startswith(("threads/", "forums/")) else path


def parse_page_url(url: str) -> PageLink | None:
    """The forum or thread page ``url`` is, or None for anything else: posts, members, /unread, /reply..."""
    match = _PAGE_URL.match(normalize(url))
    if match is None:
        return None
    first_page_url, kind_word, item_id, page = match.groups()
    kind = Kind.THREAD if kind_word == "threads" else Kind.FORUM
    return PageLink(PageRef(kind, int(item_id), int(page or 1)), first_page_url)


def page_url(first_page_url: str, page: int) -> str:
    return first_page_url if page == 1 else f"{first_page_url}page-{page}"


def bare_first_page_url(site_prefix: str, item: ItemRef) -> str:
    """A first-page URL without the words, which XenForo redirects to the full one."""
    return f"{site_prefix}{'threads' if item.kind is Kind.THREAD else 'forums'}/{item.item_id}/"


def site_prefix(first_page_url: str) -> str:
    """The part of a page URL before ``threads/`` or ``forums/``: where the forum software lives."""
    return re.split(r"(?:threads|forums)/", first_page_url, maxsplit=1)[0]


def slug_of(first_page_url: str) -> str | None:
    """``.../forums/french-english.46/`` -> ``french-english``; None if the URL has no words."""
    match = _SLUG.search(first_page_url)
    if match is None or not match.group(1):
        return None
    slug = re.sub(r"[^\w-]+", "-", unquote(match.group(1))).strip("-.")[:_SLUG_MAX_LENGTH]
    return slug or None

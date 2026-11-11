"""The site's robots.txt: which pages the crawler may fetch, and how slowly."""

import logging
from urllib import robotparser
from urllib.parse import urlparse

from xfcrawl.fetching import USER_AGENT, Fetcher, FetchFailed

_LOGGER = logging.getLogger(__name__)


class RobotsRules:
    def __init__(self, parser: robotparser.RobotFileParser | None) -> None:
        """Rules from a parsed robots.txt; None allows everything."""
        self._parser = parser

    @classmethod
    def allow_all(cls) -> "RobotsRules":
        return cls(None)

    @classmethod
    def fetch(cls, fetcher: Fetcher, start_url: str) -> "RobotsRules":
        """The site's robots.txt, fetched like any page (so through the proxy, if there is one). A missing
        robots.txt allows everything; one that can't be fetched is warned about, and allows everything."""
        site = urlparse(start_url)
        try:
            response = fetcher.get(f"{site.scheme}://{site.netloc}/robots.txt")
        except FetchFailed as error:
            _LOGGER.warning("warning: could not read robots.txt (%s); continuing without it", error)
            return cls.allow_all()
        if response.status != 200:
            return cls.allow_all()
        parser = robotparser.RobotFileParser()
        parser.parse(response.text.splitlines())
        return cls(parser)

    def allows(self, url: str) -> bool:
        return self._parser is None or self._parser.can_fetch(USER_AGENT, url)

    def crawl_delay(self) -> float | None:
        if self._parser is None:
            return None
        delay = self._parser.crawl_delay(USER_AGENT)
        return float(delay) if delay is not None else None

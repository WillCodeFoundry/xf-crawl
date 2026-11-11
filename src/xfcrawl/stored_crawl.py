"""A crawl as stored, for the commands that read one without crawling: its config, pages and plan."""

from dataclasses import dataclass
from datetime import datetime

from xfcrawl import config
from xfcrawl.config import CrawlConfig
from xfcrawl.errors import XfError
from xfcrawl.failures import Backoff, RetryPolicy
from xfcrawl.layout import StoreLayout
from xfcrawl.link_index import LinkIndex
from xfcrawl.plan import CrawlPlan
from xfcrawl.store import PageStore


@dataclass(frozen=True)
class StoredCrawl:
    config: CrawlConfig
    store: PageStore
    plan: CrawlPlan
    """Rebuilt from the store, as it would be at ``now``."""


def open_stored_crawl(layout: StoreLayout, now: datetime) -> StoredCrawl:
    crawl_config = config.load(layout)
    if crawl_config is None:
        raise XfError(f"no crawl in {layout.root}; start one with: uv run xf crawl URL")
    store = PageStore.open(layout)
    plan = CrawlPlan(crawl_config.start_url, store, LinkIndex(layout.link_index), Backoff(RetryPolicy(), now))
    plan.rebuild()
    return StoredCrawl(crawl_config, store, plan)

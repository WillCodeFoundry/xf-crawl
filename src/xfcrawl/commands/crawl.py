"""xf crawl: download a XenForo 2 forum into ./.crawl-store, politely and resumably."""

import argparse
import logging
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from urllib.parse import urlparse

import requests

from xfcrawl import config, migration
from xfcrawl.commands.tunnelbear import ask_for_credentials, open_account
from xfcrawl.config import CrawlConfig, ProxySetting, TunnelBearProxy, UrlProxy
from xfcrawl.crawler import Connection, Crawl, CrawlSummary, connect
from xfcrawl.errors import XfError
from xfcrawl.failures import RetryPolicy
from xfcrawl.fetching import USER_AGENT, Pacing, PoliteFetcher
from xfcrawl.layout import StoreLayout
from xfcrawl.plan import CrawlPlan
from xfcrawl.proxy import FixedProxy, ProxyRoute, proxied_session
from xfcrawl.store import PageStore
from xfcrawl.tunnelbear.account import Credentials
from xfcrawl.tunnelbear.rotation import ServerRotation

_LOGGER = logging.getLogger(__name__)
_AUTO_COUNTRY = "auto"


def add_parser(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subparsers.add_parser(
        "crawl",
        help="download the forum into ./.crawl-store, or carry on downloading it",
        description=(
            "Download a XenForo 2 forum into ./.crawl-store: the forum list, every forum's thread list, and "
            "every page of every thread. Only pages not stored yet are fetched, so run it again to carry on "
            "after stopping it, or to fill gaps. A page that fails is retried on later runs, waiting longer "
            "after each failure."
        ),
    )
    parser.add_argument("url", nargs="?", help="the forum's home page; only on the first run, it is remembered")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between requests (default 1.0)")
    parser.add_argument("--jitter", type=float, default=0.5, help="up to this many extra seconds (default 0.5)")
    parser.add_argument("--max-fetches", type=int, metavar="N", help="stop after N requests")
    parser.add_argument("--retry-failed", action="store_true", help="retry failed pages now, without waiting")
    parser.add_argument("--cookie", help="a Cookie header, to crawl as a logged-in user")
    parser.add_argument("--ignore-robots", action="store_true", help="don't read or obey robots.txt")
    route = parser.add_mutually_exclusive_group()
    route.add_argument("--tunnelbear", nargs="?", const=_AUTO_COUNTRY, metavar="COUNTRY",
                       help="go through TunnelBear, in COUNTRY (e.g. FR) or the closest region; remembered")
    route.add_argument("--proxy", metavar="URL", help="go through this HTTP(S) proxy; remembered")
    route.add_argument("--direct", action="store_true", help="stop going through a remembered proxy")
    parser.set_defaults(func=main)


def main(args: argparse.Namespace) -> int:
    """Composition root: the store here, the real clock, network and TunnelBear."""
    directory = Path.cwd()
    migration.refuse_old_layout(directory)
    layout = StoreLayout.in_directory(directory)
    crawl_config = settle_config(layout, args.url, requested_proxy_change(args))
    store = PageStore.open(layout)
    plan = CrawlPlan(crawl_config.start_url, store, RetryPolicy(), _now(), ignore_backoff=args.retry_failed)
    read = plan.rebuild()
    _LOGGER.info("%s pages stored; read %s of them for what is still to fetch", store.stored_count(), read)
    credentials = ask_for_credentials(os.environ, interactive=sys.stdin.isatty())
    open_connection = partial(connect_to_site, crawl_config, layout, args, os.environ, credentials)
    summary = Crawl(plan, store, open_connection, _now, time.monotonic).run(max_fetches=args.max_fetches)
    print("\n".join(report(summary)))
    return 0


@dataclass(frozen=True)
class ProxyChange:
    proxy: ProxySetting | None
    """The proxy to use from now on; None to crawl directly."""


def requested_proxy_change(args: argparse.Namespace) -> ProxyChange | None:
    """The change of proxy the arguments ask for, or None when they don't mention one."""
    if args.tunnelbear is not None:
        return ProxyChange(TunnelBearProxy(None if args.tunnelbear == _AUTO_COUNTRY else args.tunnelbear))
    if args.proxy is not None:
        return ProxyChange(UrlProxy(args.proxy))
    return ProxyChange(None) if args.direct else None


def settle_config(layout: StoreLayout, url: str | None, change: ProxyChange | None) -> CrawlConfig:
    """The crawl's config: the one here, or a new one for ``url``, with ``change`` applied and saved."""
    existing = config.load(layout)
    if existing is None and url is None:
        raise XfError(f"no crawl in {layout.root} yet; give the forum's URL")
    if url is not None:
        check_forum_url(url)
    if existing is not None and url is not None and url != existing.start_url:
        raise XfError(f"{layout.root} is a crawl of {existing.start_url}; crawl another forum from another directory")
    settled = existing or CrawlConfig.new(str(url))
    if change is not None:
        settled = settled.with_proxy(change.proxy)
    if settled != existing:
        config.save(layout, settled)
    _LOGGER.info("%s %s", "Crawl of" if existing else "New crawl of", settled.start_url)
    if settled.proxy is not None:
        _LOGGER.info("Route: %s; --direct to stop", config.describe(settled.proxy))
    return settled


def check_forum_url(url: str) -> None:
    site = urlparse(url)
    if site.scheme not in ("http", "https") or not site.netloc:
        raise XfError(f"{url!r} is not a forum address; give one like https://forum.example/")


def connect_to_site(
    crawl_config: CrawlConfig,
    layout: StoreLayout,
    args: argparse.Namespace,
    environment: Mapping[str, str],
    credentials: Callable[[], Credentials],
) -> Connection:
    """The fetcher for this crawl, through its proxy if it has one, and the site's robots.txt."""
    route = proxy_route(crawl_config.proxy, layout, environment, credentials)
    session = requests.Session() if route is None else proxied_session(environment.get("REQUESTS_CA_BUNDLE"))
    session.headers["User-Agent"] = USER_AGENT
    if args.cookie:
        session.headers["Cookie"] = args.cookie
    if route is not None:
        _LOGGER.info("Going through %s", route.describe())
    fetcher = PoliteFetcher(session, Pacing(args.delay, args.jitter), route)
    return connect(fetcher, crawl_config.start_url, obey_robots=not args.ignore_robots)


def proxy_route(
    proxy: ProxySetting | None,
    layout: StoreLayout,
    environment: Mapping[str, str],
    credentials: Callable[[], Credentials],
) -> ProxyRoute | None:
    if proxy is None:
        return None
    if isinstance(proxy, UrlProxy):
        return FixedProxy(proxy.url)
    account = open_account(layout, environment)
    if not account.is_logged_in():
        account.login(credentials(), mfa_code=None)
    return ServerRotation(account, proxy.country)


def report(summary: CrawlSummary) -> list[str]:
    lines = []
    if summary.stopped:
        lines.append(f"Stopped: {summary.stopped}")
    lines.append(f"Run finished: {summary.saved:,} downloaded, {summary.errors:,} errors, "
                 f"{summary.to_fetch:,} still to fetch.")
    if summary.waiting:
        soonest = min(summary.waiting.values()).astimezone().strftime("%Y-%m-%d %H:%M")
        lines.append(f"{len(summary.waiting):,} failed pages are waiting to be retried (next retry due {soonest}); "
                     "--retry-failed to try them now")
    if summary.to_fetch:
        lines.append("Run xf crawl again to continue.")
    return lines


def _now() -> datetime:
    return datetime.now(timezone.utc)

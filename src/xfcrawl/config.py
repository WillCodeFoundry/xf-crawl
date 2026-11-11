"""CrawlConfig: what a crawl store remembers between runs: the start URL, the proxy to go through, and the
forums to keep to."""

import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from xfcrawl.atomic_files import atomic_write
from xfcrawl.errors import XfError
from xfcrawl.layout import StoreLayout


@dataclass(frozen=True)
class TunnelBearProxy:
    country: str | None
    """An ISO code such as FR; None for the closest region."""


@dataclass(frozen=True)
class UrlProxy:
    url: str


ProxySetting = TunnelBearProxy | UrlProxy


@dataclass(frozen=True)
class CrawlConfig:
    start_url: str
    created_at: str
    proxy: ProxySetting | None = None
    only_forums: frozenset[int] | None = None
    """The forums to crawl, by number; None for all of them."""

    @classmethod
    def new(cls, start_url: str) -> "CrawlConfig":
        return cls(start_url, datetime.now(timezone.utc).replace(microsecond=0).isoformat())

    def with_proxy(self, proxy: ProxySetting | None) -> "CrawlConfig":
        return replace(self, proxy=proxy)

    def with_only_forums(self, forums: frozenset[int] | None) -> "CrawlConfig":
        return replace(self, only_forums=forums)


def load(layout: StoreLayout) -> CrawlConfig | None:
    """The store's config, or None when there is no crawl here yet. A proxy setting it doesn't recognise
    is refused: crawling directly instead would be the one thing a proxy setting rules out."""
    if not layout.config_file.exists():
        return None
    data = json.loads(layout.config_file.read_text(encoding="utf-8"))
    forums = data.get("only_forums")
    return CrawlConfig(data["start_url"], data.get("created_at", ""), _proxy_from_json(data.get("proxy")),
                       frozenset(int(forum) for forum in forums) if forums is not None else None)


def save(layout: StoreLayout, config: CrawlConfig) -> None:
    data: dict[str, object] = {"start_url": config.start_url, "created_at": config.created_at}
    if config.proxy is not None:
        data["proxy"] = _proxy_to_json(config.proxy)
    if config.only_forums is not None:
        data["only_forums"] = sorted(config.only_forums)
    atomic_write(layout.config_file, (json.dumps(data, indent=2) + "\n").encode("utf-8"))


def _proxy_from_json(data: dict[str, str] | None) -> ProxySetting | None:
    if data is None:
        return None
    if data.get("type") == "tunnelbear":
        country = data.get("country")
        return TunnelBearProxy(None if country in (None, "auto") else country)
    if data.get("type") == "url" and data.get("url"):
        return UrlProxy(data["url"])
    raise XfError(f"unrecognised proxy setting in config.json ({data}); not crawling directly. "
                  "Fix it, or run with --direct.")


def _proxy_to_json(proxy: ProxySetting) -> dict[str, str]:
    if isinstance(proxy, UrlProxy):
        return {"type": "url", "url": proxy.url}
    return {"type": "tunnelbear", "country": proxy.country or "auto"}


def describe(proxy: ProxySetting) -> str:
    if isinstance(proxy, UrlProxy):
        return f"proxy {proxy.url}"
    return f"TunnelBear ({proxy.country or 'closest region'})"

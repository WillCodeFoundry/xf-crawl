"""With a proxy set, a crawl must never reach the forum directly.

The fake proxy marks what it forwards (X-Test-Proxy: 1), so forum.direct_hits() lists every request that
went around it. Each test that could leak asserts that list stays empty.
"""

import json
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

from xfcrawl.crawler import Crawl, connect
from xfcrawl.failures import Backoff, RetryPolicy
from xfcrawl.fetching import Pacing, PoliteFetcher
from xfcrawl.layout import StoreLayout
from xfcrawl.link_index import LinkIndex
from xfcrawl.plan import CrawlPlan
from xfcrawl.proxy import proxied_session
from xfcrawl.store import PageStore
from xfcrawl.tunnelbear.account import Credentials, Endpoints, TokenFile, TunnelBearAccount
from xfcrawl.tunnelbear.rotation import ServerRotation

STORE = Path(".crawl-store")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_a_crawl_through_tunnelbear(forum, tunnelbear, xf):
    result = xf("crawl", forum.url, "--tunnelbear", "FR")
    assert result.code == 0, result.err
    assert "0 still to fetch" in result.out
    assert forum.direct_hits() == []
    assert len(forum.hits) > 0
    assert tunnelbear.rejected >= 1                  # the stale token was refused, then renewed
    assert json.loads((STORE / "config.json").read_text())["proxy"] == {"type": "tunnelbear", "country": "FR"}
    assert (STORE / "tunnelbear.json").stat().st_mode & 0o077 == 0   # the login is readable by its owner only


def test_the_proxy_is_remembered_until_direct(forum, tunnelbear, xf):
    xf("crawl", forum.url, "--tunnelbear", "FR", "--max-fetches", "2")
    xf("crawl", "--max-fetches", "2")
    assert forum.direct_hits() == []
    xf("crawl", "--direct", "--max-fetches", "1")
    assert "proxy" not in json.loads((STORE / "config.json").read_text())
    assert len(forum.direct_hits()) >= 1


def test_redirects_stay_on_the_proxy(forum, tunnelbear, xf):
    forum.slugs[21] = "renamed"
    xf("crawl", forum.url, "--tunnelbear", "FR")
    (STORE / "pages/threads/0/renamed.21/page-0001.html.gz").unlink()
    forum.slugs[21] = "renamed-again"                # the stored link now redirects
    xf("crawl")
    assert forum.count("/threads/renamed.21/") == 2
    assert forum.count("/threads/renamed-again.21/") == 1
    assert forum.direct_hits() == []


def test_an_unreachable_proxy_stops_everything(forum, tunnelbear, xf, environment):
    environment.set_env("TB_PROXY_PORT", str(free_port()))   # nothing listens there
    result = xf("crawl", forum.url, "--tunnelbear", "FR")
    assert "Stopped: proxy not working" in result.out
    assert "0 downloaded" in result.out and "1 still to fetch" in result.out
    assert forum.hits == []
    assert not list(STORE.glob("pages/**/*.failed.jsonl"))   # not blamed on any page


def test_an_untrusted_proxy_certificate_is_refused(forum, tunnelbear, xf, environment):
    environment.unset_env("REQUESTS_CA_BUNDLE")      # the test CA is no longer trusted
    result = xf("crawl", forum.url, "--tunnelbear", "FR")
    assert "Stopped: proxy not working" in result.out
    assert forum.hits == []
    assert tunnelbear.forwarded == []


def test_a_failed_token_renewal_stops_everything(forum, tunnelbear, xf):
    tunnelbear.user_calls_allowed = 1                # the first token is stale, and renewing it fails
    result = xf("crawl", forum.url, "--tunnelbear", "FR")
    assert "Stopped: proxy not working" in result.out
    assert "Traceback" not in result.err
    assert forum.hits == []


def test_a_token_renewal_failing_mid_crawl_stops_it(forum, tunnelbear):
    tunnelbear.user_calls_allowed = 6                # renewals work for a while, then the API fails
    layout = StoreLayout.in_directory(Path.cwd())
    account = TunnelBearAccount(Endpoints.from_environment(os.environ), TokenFile(layout.tunnelbear_tokens),
                                proxied_session(None))
    account.login(Credentials(os.environ["TB_USER"], os.environ["TB_PASS"]), mfa_code=None)
    rotation = ServerRotation(account, "FR", renew_every=0)   # renew before every request
    store = PageStore.open(layout)
    plan = CrawlPlan(forum.url, store, LinkIndex(layout.link_index), Backoff(RetryPolicy(), datetime.now(timezone.utc)))
    plan.rebuild()
    fetcher = PoliteFetcher(proxied_session(os.environ["REQUESTS_CA_BUNDLE"]), Pacing(0, 0), rotation)

    def connection():
        return connect(fetcher, forum.url, obey_robots=False)

    summary = Crawl(plan, store, connection, lambda: datetime.now(timezone.utc), time.monotonic).run(max_fetches=None)
    assert summary.stopped is not None
    assert "could not renew the TunnelBear token" in summary.stopped
    assert summary.saved > 0                          # it was working before the API died
    assert forum.direct_hits() == []


def test_proxy_variables_in_the_environment_cant_route_around_it(forum, tunnelbear, xf, environment):
    for name in ("NO_PROXY", "no_proxy"):
        environment.set_env(name, "*")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        environment.set_env(name, f"http://127.0.0.1:{free_port()}")
    assert "0 still to fetch" in xf("crawl", forum.url, "--tunnelbear", "FR").out
    assert forum.direct_hits() == []
    assert len(forum.hits) > 0


def test_a_proxy_dying_mid_crawl_stops_it(forum, tunnelbear, xf):
    xf("crawl", forum.url, "--tunnelbear", "FR", "--max-fetches", "3")
    tunnelbear.proxy.shutdown()
    tunnelbear.proxy.server_close()
    requests_so_far = len(forum.hits)
    assert "Stopped: proxy not working" in xf("crawl").out
    assert len(forum.hits) == requests_so_far
    assert forum.direct_hits() == []


def test_an_unknown_proxy_setting_is_refused(forum, xf):
    xf("crawl", forum.url, "--max-fetches", "1")
    settings = json.loads((STORE / "config.json").read_text())
    settings["proxy"] = {"type": "socks"}
    (STORE / "config.json").write_text(json.dumps(settings))
    requests_so_far = len(forum.hits)
    result = xf("crawl")
    assert result.code == 1
    assert "not crawling directly" in result.err
    assert len(forum.hits) == requests_so_far


def test_a_fixed_proxy_url(forum, tunnelbear, xf):
    url = f"https://tok2:tok2@localhost:{tunnelbear.proxy_port}"
    assert "0 still to fetch" in xf("crawl", forum.url, "--proxy", url).out
    assert forum.direct_hits() == []
    assert len(tunnelbear.forwarded) > 0


"""With a proxy configured the crawler must never contact the forum directly.

The fake proxy marks what it forwards (X-Test-Proxy: 1), so forum.direct_hits()
lists every request that went around it. Each test that could leak asserts
that list stays empty.
"""

import json
import socket
from pathlib import Path

from xfcrawl import tb_proxy

STORE = Path(".crawl-store")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_crawl_through_tunnelbear(forum, tunnelbear, run):
    r = run("xf-crawl", forum.url, "--tunnelbear", "FR")
    assert r.code == 0, r.err
    assert "0 still to fetch" in r.err
    assert forum.direct_hits() == []
    assert len(forum.hits) > 0
    assert tunnelbear.rejected >= 1                      # stale token was refused, then refreshed
    assert json.loads((STORE / "config.json").read_text())["proxy"] == {"type": "tunnelbear", "country": "FR"}
    assert (STORE / "tunnelbear.json").stat().st_mode & 0o077 == 0   # login readable by owner only


def test_proxy_setting_is_remembered_and_direct_clears_it(forum, tunnelbear, run):
    run("xf-crawl", forum.url, "--tunnelbear", "FR", "--max-fetches", "2")
    run("xf-crawl", "--max-fetches", "2")
    assert forum.direct_hits() == []
    run("xf-crawl", "--direct", "--max-fetches", "1")
    assert "proxy" not in json.loads((STORE / "config.json").read_text())
    assert len(forum.direct_hits()) >= 1


def test_redirects_stay_on_the_proxy(forum, tunnelbear, run):
    forum.slugs[21] = "renamed"                          # listing links the new slug...
    run("xf-crawl", forum.url, "--tunnelbear", "FR")
    (STORE / "pages/threads/0/renamed.21/page-0001.html.gz").unlink()
    forum.slugs[21] = "renamed-again"                    # ...then the old URL redirects
    run("xf-crawl")
    assert forum.count("/threads/renamed.21/") == 2
    assert forum.count("/threads/renamed-again.21/") == 1
    assert forum.direct_hits() == []


def test_unreachable_proxy_stops_everything(forum, tunnelbear, run, monkeypatch):
    monkeypatch.setattr(tb_proxy, "PROXY_PORT", free_port())   # nothing listens there
    r = run("xf-crawl", forum.url, "--tunnelbear", "FR")
    assert "Proxy not working, stopping" in r.err
    assert "0 downloaded" in r.err and "1 still to fetch" in r.err
    assert forum.hits == []
    assert not list(STORE.glob("pages/**/*.failed.jsonl"))     # not blamed on any page


def test_untrusted_proxy_certificate_is_refused(forum, tunnelbear, run, monkeypatch):
    monkeypatch.delenv("REQUESTS_CA_BUNDLE")             # the test CA is no longer trusted
    r = run("xf-crawl", forum.url, "--tunnelbear", "FR")
    assert "Proxy not working, stopping" in r.err
    assert forum.hits == []
    assert tunnelbear.forwarded == []


def test_tunnelbear_api_failure_on_refresh_stops(forum, tunnelbear, run):
    tunnelbear.user_calls_allowed = 1                     # stale token, and refresh fails
    r = run("xf-crawl", forum.url, "--tunnelbear", "FR")
    assert "Proxy not working, stopping" in r.err
    assert "Traceback" not in r.err
    assert forum.hits == []


def test_token_refresh_failure_mid_crawl_stops(forum, tunnelbear, run, monkeypatch):
    # renew the token before every request, and let the API die after a few renewals
    monkeypatch.setattr(tb_proxy.ProxyRotator.__init__, "__defaults__", (None, 0))
    tunnelbear.user_calls_allowed = 6
    r = run("xf-crawl", forum.url, "--tunnelbear", "FR")
    assert "Proxy not working, stopping" in r.err
    assert "could not get proxy settings" in r.err
    assert len(forum.hits) > 0                           # it was working before the API died
    assert forum.direct_hits() == []


def test_environment_cannot_route_around_the_proxy(forum, tunnelbear, run, monkeypatch):
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(var, "*")
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(var, f"http://127.0.0.1:{free_port()}")
    r = run("xf-crawl", forum.url, "--tunnelbear", "FR")
    assert "0 still to fetch" in r.err
    assert forum.direct_hits() == []
    assert len(forum.hits) > 0


def test_proxy_dies_mid_crawl(forum, tunnelbear, run):
    run("xf-crawl", forum.url, "--tunnelbear", "FR", "--max-fetches", "3")
    tunnelbear.proxy.shutdown()
    tunnelbear.proxy.server_close()
    before = len(forum.hits)
    r = run("xf-crawl")
    assert "Proxy not working, stopping" in r.err
    assert len(forum.hits) == before
    assert forum.direct_hits() == []


def test_unknown_proxy_setting_refuses_to_run(forum, run):
    run("xf-crawl", forum.url, "--max-fetches", "1")
    cfg = json.loads((STORE / "config.json").read_text())
    cfg["proxy"] = {"type": "socks"}
    (STORE / "config.json").write_text(json.dumps(cfg))
    (STORE / "pages/index.html.gz").unlink()
    before = len(forum.hits)
    r = run("xf-crawl")
    assert r.code == 1
    assert "not crawling directly" in r.err
    assert len(forum.hits) == before


def test_static_proxy_url(forum, tunnelbear, run):
    url = f"https://tok2:tok2@localhost:{tunnelbear.proxy_port}"
    r = run("xf-crawl", forum.url, "--proxy", url)
    assert "0 still to fetch" in r.err
    assert forum.direct_hits() == []
    assert len(tunnelbear.forwarded) > 0

#!/usr/bin/env python3
"""
xf-crawl — download a XenForo 2.x forum into a local gzipped crawl store (.crawl-store/pages),
politely and resumably. Downloads only; use xf-mine to analyse the pages.

    xf-crawl http://localhost/forum/    first run (creates ./.crawl-store)
    xf-crawl                            continue / fill gaps
    xf-crawl --list                     show what is still to crawl (offline)
    xf-crawl --retry-failed             retry failed pages now, ignoring back-off

There is no queue file: each run rebuilds the to-crawl list from the stored pages
(see xfcrawl/common.py), so interrupting, crashing, or deleting stored files is
always safe — the next run simply fetches whatever is missing.

Pages are stored only after a successful fetch of a real forum page. A page
that fails gets a page-0001.failed.jsonl file instead, with one JSON line
appended per failed attempt: time, reason (http-503, http-404, timeout, network,
error-page, redirect, robots), url and an optional detail. It is deleted once
the page is fetched.

Failed pages are retried with exponential back-off based on those timestamps:
1h, 2h, 4h ... for temporary problems, 1d, 2d, 4d ... for 404/403/redirects,
capped at 30 days — so persistently failing pages are tried less and less.
    find .crawl-store/pages -name '*.failed.jsonl'   lists every problem page

Politeness: --delay (1.0s) + random --jitter (0.5s) between requests, robots.txt
respected (incl. Crawl-delay), exponential back-off on 429/5xx honouring
Retry-After, and the run stops after 5 failures in a row.

Going through a proxy (remembered in .crawl-store/config.json for later runs):
    --tunnelbear [COUNTRY]   via your TunnelBear account (see tb-proxy);
                             logs in on first use (or set TB_USER / TB_PASS),
                             rotates to another TunnelBear server if one fails.
    --proxy URL              any HTTP(S) proxy, e.g. http://127.0.0.1:8118 (tb-proxy serve)
    --direct                 stop using a proxy
    Proxy failures never count against a page; if the proxy stays down the run
    stops and the page is simply fetched next time.
    Fail-closed: with a proxy set, no request to the forum ever goes out
    directly. If the proxy (or TunnelBear login/token refresh) fails in any way,
    the run stops. Proxy environment variables (HTTP(S)_PROXY, NO_PROXY) are
    ignored so they can't route around it, and the proxy's TLS certificate is
    always verified. The forum's hostname is resolved by the proxy, not locally.
    (TunnelBear's own login/token API is contacted directly, as the extension does.)

"""
import argparse
import os
import random
import sys
import time
from datetime import datetime
from urllib import robotparser
from urllib.parse import urlparse

import requests

from .common import (
    STORE_DIR,
    Plan,
    classify,
    load_config,
    make_key,
    now,
    save_config,
    template_of,
    write_page,
)

USER_AGENT = "xf-crawl/4.0 (+local maintenance crawl)"
RETRY_STATUSES = {429, 500, 502, 503, 504}
BAD_TEMPLATES = {"error", "login", "error_with_login", "message_page"}
MAX_RETRIES = 5
MAX_CONSECUTIVE_FAILURES = 5


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


class ProxyDown(Exception):
    pass


class StaticProxy:
    """A fixed proxy URL (e.g. tb-proxy serve, or any other proxy)."""
    max_failures = 3

    def __init__(self, url):
        self.url = url

    def proxies(self):
        return {"http": self.url, "https": self.url}

    def failed(self, auth_problem=False):
        pass

    def describe(self):
        return f"proxy {self.url}"


def make_tunnelbear(country):
    from .tb_proxy import ProxyRotator, TBError, TunnelBear
    try:
        tb = TunnelBear()
        tb.ensure_login(interactive=sys.stdin.isatty())
        rot = ProxyRotator(tb, None if country == "auto" else country)
    except TBError as e:
        sys.exit(f"TunnelBear: {e}")
    rot.max_failures = 2 * len(rot.hosts) + 1   # each server may fail once for auth, once for real
    return rot


class FetchError(Exception):
    def __init__(self, reason, detail=""):
        super().__init__(f"{reason} {detail}".strip())
        self.reason, self.detail = reason, detail


class PoliteFetcher:
    def __init__(self, start_url, delay, jitter, cookie=None, use_robots=True, proxy=None):
        self.proxy = proxy
        self.s = requests.Session()
        if proxy:
            from .tb_proxy import proxied_session  # verifies the proxy's TLS cert, also for http:// forums
            self.s = proxied_session()
            # Ignore HTTP(S)_PROXY / NO_PROXY etc.: nothing in the environment may
            # change or bypass the configured proxy.
            self.s.trust_env = False
            self.s.verify = os.environ.get("REQUESTS_CA_BUNDLE") or True
        self.s.headers["User-Agent"] = USER_AGENT
        if cookie:
            self.s.headers["Cookie"] = cookie
        self.delay, self.jitter, self.last = delay, jitter, 0.0
        self.robots = None
        if use_robots:
            p = urlparse(start_url)
            try:  # same retries and proxy handling as pages; ProxyDown propagates
                status, _, text = self.get(f"{p.scheme}://{p.netloc}/robots.txt")
            except FetchError as e:
                log(f"warning: could not read robots.txt ({e}); continuing without it")
                status = None
            if status == 200:
                self.robots = robotparser.RobotFileParser()
                self.robots.parse(text.splitlines())
                cd = self.robots.crawl_delay(USER_AGENT)
                if cd and float(cd) > self.delay:
                    log(f"robots.txt Crawl-delay is {cd}s; using it")
                    self.delay = float(cd)

    def allowed(self, url):
        return self.robots is None or self.robots.can_fetch(USER_AGENT, url)

    def _proxies(self):
        """Proxy settings for the next request, or None when crawling directly.
        With a proxy configured this never returns None/empty: it raises ProxyDown."""
        if self.proxy is None:
            return None
        try:
            p = self.proxy.proxies()
        except Exception as e:  # e.g. TunnelBear token refresh failed
            raise ProxyDown(f"could not get proxy settings: {e}") from e
        if not (p and p.get("http") and p.get("https")):
            raise ProxyDown("proxy settings are empty")
        return p

    def _proxy_failed(self, auth_problem):
        try:
            self.proxy.failed(auth_problem=auth_problem)
        except Exception as e:
            raise ProxyDown(f"could not switch proxy server: {e}") from e

    def get(self, url):
        """(status, final_url, text). Retries transient errors, then raises FetchError.
        Proxy problems switch proxy server and don't count as attempts; if the
        proxy keeps failing, raises ProxyDown."""
        backoff = max(self.delay, 1.0) * 2
        attempt = proxy_failures = 0
        while attempt < MAX_RETRIES:
            attempt += 1
            pause = self.last + self.delay + random.uniform(0, self.jitter) - time.monotonic()
            if pause > 0:
                time.sleep(pause)
            try:
                r = self.s.get(url, timeout=30, proxies=self._proxies())
                self.last = time.monotonic()
                if r.status_code == 407 and self.proxy:
                    raise requests.exceptions.ProxyError("407 Proxy Authentication Required")
                if r.status_code not in RETRY_STATUSES:
                    return r.status_code, r.url, r.text
                reason, err = f"http-{r.status_code}", f"HTTP {r.status_code}"
                ra = r.headers.get("Retry-After", "")
                if ra.isdigit():
                    backoff = max(backoff, int(ra))
            except requests.RequestException as e:
                self.last = time.monotonic()
                if self.proxy and isinstance(e, (requests.exceptions.ProxyError,
                                                 requests.exceptions.SSLError)):
                    proxy_failures += 1
                    if proxy_failures >= self.proxy.max_failures:
                        raise ProxyDown(f"{self.proxy.describe()}: {e}") from e
                    self._proxy_failed(auth_problem="407" in str(e))
                    log(f"    ! proxy problem ({type(e).__name__}); now {self.proxy.describe()}")
                    attempt -= 1          # not the page's fault
                    continue
                reason = "timeout" if isinstance(e, requests.Timeout) else "network"
                err = f"{type(e).__name__}: {e}"
            if attempt == MAX_RETRIES:
                raise FetchError(reason, "" if reason.startswith("http-") else err)
            log(f"    ! {err}; retry {attempt}/{MAX_RETRIES - 1} in {backoff:.0f}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)


def run(args, cfg):
    t0 = time.monotonic()
    plan = Plan(cfg["start_url"], ignore_backoff=args.retry_failed)
    n_read = plan.rebuild(progress=lambda i, n: log(f"  rebuilding to-crawl list: {i:,}/{n:,} pages read"))
    log(f"{len(plan.stored):,} pages stored; read {n_read:,} of them to rebuild the to-crawl list "
        f"in {time.monotonic() - t0:.1f}s -> {len(plan.heap):,} pages to fetch")
    if plan.waiting:
        soonest = min(plan.failures[k]["next_try"] for k in plan.waiting)
        log(f"{len(plan.waiting):,} failed pages are waiting out their back-off "
            f"(next retry due {soonest.astimezone():%Y-%m-%d %H:%M}); --retry-failed to try them now")

    if args.list:
        for _, item_id, page, kind in plan.pending():
            print(f"{make_key(kind, item_id, page)}\t{plan.url_for(kind, item_id, page)}")
        return

    fetcher = None
    fetched = saved = errors = consecutive = 0   # fetched: requests made; saved: pages stored
    last_report = time.monotonic()

    while True:
        if args.max_fetches is not None and fetched >= args.max_fetches:
            log(f"Reached --max-fetches {args.max_fetches}; stopping.")
            break
        nxt = plan.pop()
        if nxt is None:
            break
        kind, item_id, page, url = nxt
        key = make_key(kind, item_id, page)

        def failed(reason, detail="", msg=None, kind=kind, item_id=item_id, page=page, url=url):
            f = plan.record_failure(kind, item_id, page, reason, url, detail)
            log(f"    ! {msg or reason} — not stored; attempt {f['attempts']}, "
                f"next try after {f['next_try'].astimezone():%Y-%m-%d %H:%M}")

        if fetcher is None:
            proxy = None
            pc = cfg.get("proxy")
            if pc and pc.get("type") == "tunnelbear":
                proxy = make_tunnelbear(pc.get("country") or "auto")
            elif pc and pc.get("type") == "url" and pc.get("url"):
                proxy = StaticProxy(pc["url"])
            elif pc:
                sys.exit(f"Unrecognised proxy setting in config.json ({pc}); not crawling directly. "
                         "Fix it, or run with --direct.")
            if proxy:
                log(f"Going through {proxy.describe()}")
            try:
                fetcher = PoliteFetcher(cfg["start_url"], args.delay, args.jitter, args.cookie,
                                        not args.ignore_robots, proxy)
            except ProxyDown as e:
                log(f"Proxy not working, stopping: {e}")
                plan.requeue(kind, item_id, page)
                break
        if not fetcher.allowed(url):
            failed("robots", msg="blocked by robots.txt")
            continue

        log(f"GET {key:<20} {url}")
        try:
            status, final_url, text = fetcher.get(url)
        except ProxyDown as e:
            log(f"Proxy not working, stopping (nothing recorded against {key}): {e}")
            plan.requeue(kind, item_id, page)
            break
        except FetchError as e:
            failed(e.reason, e.detail, msg=f"{e} (after {MAX_RETRIES} tries)")
            errors += 1
            consecutive += 1
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                log(f"{consecutive} failures in a row — is the server down? Stopping.")
                break
            continue
        fetched += 1

        problem = None
        if status != 200:
            problem = (f"http-{status}", "", None)
        elif kind != "index" and (c := classify(final_url)) and c[:3] != (kind, item_id, page):
            # page past the end, or thread moved/merged: fetch the target instead
            problem = ("redirect", final_url, f"redirected to {final_url}")
            plan.note_url(final_url)
        elif template_of(text) in BAD_TEMPLATES or "</html>" not in text[-2000:].lower():
            tmpl = template_of(text)
            problem = ("error-page", f"template={tmpl}", f"got an error/login page (template={tmpl})")

        if problem:
            failed(*problem)
            errors += 1
            continue

        consecutive = 0
        if kind != "index" and (c := classify(final_url)):
            plan.seen_at(kind, item_id, c[3])   # current URL/slug; renames the folder if it changed
        write_page(plan.page_path(kind, item_id, page), text)
        saved += 1
        plan.mark_stored(kind, item_id, page)
        new = plan.discover(kind, item_id, page, text, url)

        if new or time.monotonic() - last_report > 30:
            log(f"    {len(plan.heap):,} to fetch" + (f" (+{new} new)" if new else ""))
            last_report = time.monotonic()

    left = len(plan.heap)
    log(f"Run finished: {saved:,} downloaded, {errors:,} errors, {left:,} still to fetch.")
    if left:
        log("Re-run xf-crawl to continue.")
    if errors or plan.waiting:
        log("Problem pages have a page-NNNN.failed.jsonl file next to where the page would be "
            "(find .crawl-store/pages -name '*.failed.jsonl').")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?", help="Forum home page (only needed on the first run)")
    ap.add_argument("--list", action="store_true", help="Print the to-crawl list and exit (no network)")
    ap.add_argument("--delay", type=float, default=1.0, help="Seconds between requests (default 1.0)")
    ap.add_argument("--jitter", type=float, default=0.5, help="Extra random 0..N seconds (default 0.5)")
    ap.add_argument("--max-fetches", type=int, help="Stop after downloading N pages")
    ap.add_argument("--retry-failed", action="store_true", help="Retry failed pages now, ignoring back-off")
    ap.add_argument("--cookie", help="Cookie header, to crawl as a logged-in user")
    ap.add_argument("--ignore-robots", action="store_true")
    route = ap.add_mutually_exclusive_group()
    route.add_argument("--tunnelbear", nargs="?", const="auto", metavar="COUNTRY",
                       help="Go through TunnelBear (optional ISO country, e.g. FR); remembered")
    route.add_argument("--proxy", metavar="URL", help="Go through this HTTP(S) proxy; remembered")
    route.add_argument("--direct", action="store_true", help="Stop using a remembered proxy")
    args = ap.parse_args()

    cfg = load_config()
    if cfg is None:
        if not args.url:
            ap.error(f"no crawl in {os.path.abspath(STORE_DIR)} yet — give the forum URL")
        cfg = {"start_url": args.url, "created_at": now().isoformat()}
        save_config(cfg)
        log(f"New crawl of {args.url} in {os.path.abspath(STORE_DIR)}")
    elif args.url and args.url != cfg["start_url"]:
        ap.error(f"{STORE_DIR} belongs to a crawl of {cfg['start_url']}; "
                 "run from another directory to crawl a different forum.")
    else:
        log(f"Crawl of {cfg['start_url']}")

    if args.tunnelbear or args.proxy or args.direct:
        if args.tunnelbear:
            cfg["proxy"] = {"type": "tunnelbear", "country": args.tunnelbear}
        elif args.proxy:
            cfg["proxy"] = {"type": "url", "url": args.proxy}
        else:
            cfg.pop("proxy", None)
        save_config(cfg)
    if cfg.get("proxy"):
        pc = cfg["proxy"]
        route = f"TunnelBear ({pc.get('country')})" if pc.get("type") == "tunnelbear" else f"proxy {pc.get('url')}"
        log(f"Route: {route} — use --direct to stop")

    try:
        run(args, cfg)
    except KeyboardInterrupt:
        log("Interrupted. Everything downloaded is saved; re-run to continue.")
        sys.exit(130)


if __name__ == "__main__":
    main()

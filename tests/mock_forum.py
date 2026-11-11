"""A tiny fake XenForo 2 forum for tests.

Layout (thread pages per thread in THREADS):
    /                         forum list: General (2)
    /forums/general.2/        threads 10-14 (3 per listing page) + sub-forum Sub (5)
    /forums/sub.5/            threads 20, 21
    thread 10 is a sticky, shown on every listing page; it has 7 pages and
    its navigation only links 1 2 3 7, like XenForo's.

Optional misbehaviour (all off by default, so tests stay fast):
    gone          thread ids answering 404
    error_once    thread ids answering a 200 XenForo error page the first time
    busy_once     thread ids answering 503 (Retry-After: 0) the first time
    slugs         thread id -> slug; requesting another slug redirects (a rename)
"""

import http.server
import re
import threading
from dataclasses import dataclass, field

THREADS = {10: 7, 11: 1, 12: 2, 13: 1, 14: 1, 20: 1, 21: 1}
FORUMS = {2: ("General", "general", [10, 11, 12, 13, 14], [5]), 5: ("Sub", "sub", [20, 21], [])}
PER_LISTING = 3
POSTS_PER_PAGE = 20


@dataclass
class Hit:
    path: str
    via_proxy: bool


@dataclass
class MockForum:
    gone: set = field(default_factory=set)
    error_once: set = field(default_factory=set)
    busy_once: set = field(default_factory=set)
    slugs: dict = field(default_factory=dict)
    hits: list = field(default_factory=list)

    def start(self):
        forum = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                forum.hits.append(Hit(self.path, self.headers.get("X-Test-Proxy") == "1"))
                code, body, headers = forum.respond(self.path)
                self.send_response(code)
                for k, v in headers.items():
                    self.send_header(k, v)
                data = body.encode()
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    # -- helpers for assertions ----------------------------------------------
    def direct_hits(self):
        return [h for h in self.hits if not h.via_proxy]

    def count(self, path):
        return sum(1 for h in self.hits if h.path == path)

    # -- pages ---------------------------------------------------------------
    def slug(self, tid):
        return self.slugs.get(tid, f"thread-{tid}")

    @staticmethod
    def page(template, title, body):
        return (f'<!DOCTYPE html><html data-template="{template}"><head><title>{title}</title></head>'
                f'<body><h1 class="p-title-value">{title}</h1>{body}</body></html>')

    @staticmethod
    def nav(base, cur, last):
        if last == 1:
            return ""
        nums = sorted({1, 2, 3, last} & set(range(1, last + 1)))
        links = "".join(f'<li class="pageNav-page"><a href="{base}{"" if n == 1 else f"page-{n}"}">{n}</a></li>'
                        for n in nums)
        nxt = f'<a class="pageNav-jump pageNav-jump--next" href="{base}page-{cur + 1}">Next</a>' if cur < last else ""
        return f'<ul class="pageNav-main">{links}</ul>{nxt}'

    def listing_item(self, tid, prefix=False):
        replies = "1.2K" if tid == 12 else str(THREADS[tid] * POSTS_PER_PAGE - 1)
        pre = '<a href="/forums/general.2/?prefix_id=1" class="labelLink">Q</a>' if prefix else ""
        return (f'<div class="structItem structItem--thread"><div class="structItem-title">{pre}'
                f'<a href="/threads/{self.slug(tid)}.{tid}/" data-tp-primary="on">Thread {tid}</a></div>'
                f'<div class="structItem-cell structItem-cell--meta"><dl><dd>{replies}</dd></dl>'
                f'<dl><dd>99</dd></dl></div></div>')

    def respond(self, path):
        if path == "/robots.txt":
            return 404, "", {}
        if path == "/":
            return 200, self.page("forum_list", "Forums",
                                  '<h3 class="node-title"><a href="/forums/general.2/">General</a></h3>'), {}

        m = re.fullmatch(r"/forums/([a-z-]+)\.(\d+)/(?:page-(\d+))?", path)
        if m and int(m[2]) in FORUMS:
            fid, pg = int(m[2]), int(m[3] or 1)
            title, slug, tids, subs = FORUMS[fid]
            base = f"/forums/{slug}.{fid}/"
            last = -(-len(tids) // PER_LISTING)
            body = ""
            if pg == 1:
                body += "".join(f'<h3 class="node-title"><a href="/forums/{FORUMS[s][1]}.{s}/">{FORUMS[s][0]}</a></h3>'
                                for s in subs)
            if fid == 2:
                body += self.listing_item(10)            # sticky: repeated on every page
            body += "".join(self.listing_item(t, prefix=True) for t in tids[(pg - 1) * PER_LISTING: pg * PER_LISTING])
            return 200, self.page("forum_view", title, body + self.nav(base, pg, last)), {}

        m = re.fullmatch(r"/threads/([a-z0-9-]+)\.(\d+)/(?:page-(\d+))?", path)
        if m and int(m[2]) in THREADS:
            slug, tid, pg = m[1], int(m[2]), int(m[3] or 1)
            if slug != self.slug(tid):                   # renamed thread: XenForo redirects
                return 301, "", {"Location": f"/threads/{self.slug(tid)}.{tid}/" + (f"page-{pg}" if pg > 1 else "")}
            if tid in self.gone:
                return 404, self.page("error", "Oops", "not found"), {}
            if tid in self.error_once:
                self.error_once.discard(tid)
                return 200, self.page("error", "Oops", "Something went wrong"), {}
            if tid in self.busy_once:
                self.busy_once.discard(tid)
                return 503, "busy", {"Retry-After": "0"}
            last = THREADS[tid]
            base = f"/threads/{slug}.{tid}/"
            if pg > last:
                return 301, "", {"Location": f"{base}page-{last}"}
            canonical = f'<link rel="canonical" href="{self.url.rstrip("/")}{base}{"" if pg == 1 else f"page-{pg}"}">'
            n_posts = POSTS_PER_PAGE if pg < last else 7
            posts = '<article class="message message--post">post</article>' * n_posts
            return 200, self.page("thread_view", f"Thread {tid}", canonical + posts + self.nav(base, pg, last)), {}

        return 404, self.page("error", "Oops", "no such page"), {}

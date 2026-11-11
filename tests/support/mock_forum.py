"""A tiny fake XenForo 2 forum for tests.

Layout (thread pages per thread in THREADS):
    /                         forum list: General (2)
    /forums/general.2/        threads 10-14 (3 per listing page) + sub-forum Sub (5)
    /forums/sub.5/            threads 20, 21
    thread 10 is a sticky, shown on every page of General; it has 7 pages and its navigation links only
    1 2 3 7, like XenForo's.

Optional misbehaviour (all off by default, so tests stay fast):
    gone          thread ids answering 404
    error_once    thread ids answering a 200 XenForo error page the first time
    busy_once     thread ids answering 503 (Retry-After: 0) the first time
    slugs         thread id -> slug; requesting another slug redirects (a rename)
    robots        the text of /robots.txt (none by default: 404)
"""

import http.server
import re
import threading
from dataclasses import dataclass, field

THREADS = {10: 7, 11: 1, 12: 2, 13: 1, 14: 1, 20: 1, 21: 1}
FORUMS = {2: ("General", "general", [10, 11, 12, 13, 14], [5]), 5: ("Sub", "sub", [20, 21], [])}
PER_LISTING = 3
POSTS_PER_PAGE = 20
_FORUM_PATH = re.compile(r"/forums/([a-z-]+)\.(\d+)/(?:page-(\d+))?")
_THREAD_PATH = re.compile(r"/threads/([a-z0-9-]+)\.(\d+)/(?:page-(\d+))?")
_FORUM_LIST = '<h3 class="node-title"><a href="/forums/general.2/">General</a></h3>'

Reply = tuple[int, str, dict[str, str]]


@dataclass
class Hit:
    path: str
    via_proxy: bool


@dataclass
class MockForum:
    gone: set[int] = field(default_factory=set)
    error_once: set[int] = field(default_factory=set)
    busy_once: set[int] = field(default_factory=set)
    slugs: dict[int, str] = field(default_factory=dict)
    robots: str | None = None
    hits: list[Hit] = field(default_factory=list)

    def start(self) -> "MockForum":
        forum = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                forum.hits.append(Hit(self.path, self.headers.get("X-Test-Proxy") == "1"))
                code, body, headers = forum.respond(self.path)
                self.send_response(code)
                for name, value in headers.items():
                    self.send_header(name, value)
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

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # -- for assertions -----------------------------------------------------------------------------
    def direct_hits(self) -> list[Hit]:
        return [hit for hit in self.hits if not hit.via_proxy]

    def count(self, path: str) -> int:
        return sum(1 for hit in self.hits if hit.path == path)

    def slug(self, thread_id: int) -> str:
        return self.slugs.get(thread_id, f"thread-{thread_id}")

    # -- pages --------------------------------------------------------------------------------------
    def respond(self, path: str) -> Reply:
        if path == "/robots.txt":
            return (404, "", {}) if self.robots is None else (200, self.robots, {})
        if path == "/":
            return 200, _page("forum_list", "Forums", _FORUM_LIST), {}
        forum = _FORUM_PATH.fullmatch(path)
        if forum and int(forum[2]) in FORUMS:
            return self._forum(int(forum[2]), int(forum[3] or 1))
        thread = _THREAD_PATH.fullmatch(path)
        if thread and int(thread[2]) in THREADS:
            return self._thread(thread[1], int(thread[2]), int(thread[3] or 1))
        return 404, _page("error", "Oops", "no such page"), {}

    def _forum(self, forum_id: int, page: int) -> Reply:
        title, slug, thread_ids, subforums = FORUMS[forum_id]
        last = -(-len(thread_ids) // PER_LISTING)
        body = ""
        if page == 1:
            body += "".join(f'<h3 class="node-title"><a href="/forums/{FORUMS[s][1]}.{s}/">{FORUMS[s][0]}</a></h3>'
                            for s in subforums)
        if forum_id == 2:
            body += self._listed(10)                 # the sticky, on every page
        body += "".join(self._listed(t, prefix=True) for t in thread_ids[(page - 1) * PER_LISTING: page * PER_LISTING])
        return 200, _page("forum_view", title, body + _navigation(f"/forums/{slug}.{forum_id}/", page, last)), {}

    def _thread(self, slug: str, thread_id: int, page: int) -> Reply:
        if slug != self.slug(thread_id):            # renamed: XenForo redirects to the new address
            target = f"/threads/{self.slug(thread_id)}.{thread_id}/"
            return 301, "", {"Location": target + (f"page-{page}" if page > 1 else "")}
        if thread_id in self.gone:
            return 404, _page("error", "Oops", "not found"), {}
        if thread_id in self.error_once or thread_id in self.busy_once:
            return self._misbehave_once(thread_id)
        last = THREADS[thread_id]
        base = f"/threads/{slug}.{thread_id}/"
        if page > last:
            return 301, "", {"Location": f"{base}page-{last}"}
        canonical = f'<link rel="canonical" href="{self.url.rstrip("/")}{base}{"" if page == 1 else f"page-{page}"}">'
        posts = '<article class="message message--post">post</article>' * (POSTS_PER_PAGE if page < last else 7)
        return 200, _page("thread_view", f"Thread {thread_id}", canonical + posts + _navigation(base, page, last)), {}

    def _misbehave_once(self, thread_id: int) -> Reply:
        if thread_id in self.error_once:
            self.error_once.discard(thread_id)
            return 200, _page("error", "Oops", "Something went wrong"), {}
        self.busy_once.discard(thread_id)
        return 503, "busy", {"Retry-After": "0"}

    def _listed(self, thread_id: int, *, prefix: bool = False) -> str:
        replies = "1.2K" if thread_id == 12 else str(THREADS[thread_id] * POSTS_PER_PAGE - 1)
        label = '<a href="/forums/general.2/?prefix_id=1" class="labelLink">Q</a>' if prefix else ""
        return (f'<div class="structItem structItem--thread"><div class="structItem-title">{label}'
                f'<a href="/threads/{self.slug(thread_id)}.{thread_id}/" data-tp-primary="on">Thread {thread_id}</a>'
                f'</div><div class="structItem-cell structItem-cell--meta"><dl><dd>{replies}</dd></dl>'
                f'<dl><dd>99</dd></dl></div></div>')


def _page(template: str, title: str, body: str) -> str:
    return (f'<!DOCTYPE html><html data-template="{template}"><head><title>{title}</title></head>'
            f'<body><h1 class="p-title-value">{title}</h1>{body}</body></html>')


def _navigation(base: str, current: int, last: int) -> str:
    if last == 1:
        return ""
    numbers = sorted({1, 2, 3, last} & set(range(1, last + 1)))
    links = "".join(f'<li class="pageNav-page"><a href="{base}{"" if n == 1 else f"page-{n}"}">{n}</a></li>'
                    for n in numbers)
    following = ""
    if current < last:
        following = f'<a class="pageNav-jump pageNav-jump--next" href="{base}page-{current + 1}">Next</a>'
    return f'<ul class="pageNav-main">{links}</ul>{following}'

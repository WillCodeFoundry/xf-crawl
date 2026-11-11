"""
xfcrawl.common — crawl-store layout, URL rules and the to-crawl plan shared by
xf-crawl and xf-mine. No database: the files in .crawl-store/pages are
the whole state.

.crawl-store/ (in the current working directory, created if missing):
    config.json                                   start URL and proxy setting
    tunnelbear.json                               TunnelBear login (if used)
    pages/index.html.gz                           forum home page
    pages/forums/<slug>.<id>/page-0001.html.gz    forum thread-list pages
    pages/threads/<id // 1000>/<slug>.<id>/page-0001.html.gz
                                                  thread pages
    .../page-0001.failed.jsonl                    failed fetches of that page (instead of .html.gz)

Folders are named like the site's URLs: forums/french-english.46,
threads/1234/parler-le-francais.1234567. The number is the key; the words
(XenForo's URL slug) are for people and may change when a forum or thread is
renamed, in which case the folder is renamed too. A folder is called just
<id> until its words are known. Threads are grouped by id // 1000 so no
directory gets huge. Stores made by earlier versions: see xf-migrate.

A .failed.jsonl file gets one JSON object appended per failed attempt:
    {"time": "2026-11-09T16:16:05+00:00", "reason": "http-503", "url": "http://localhost/threads/x.12/"}
    {"time": "2026-11-09T17:20:41+00:00", "reason": "redirect", "url": "http://localhost/threads/x.12/page-3",
     "detail": "http://localhost/threads/x.12/page-2"}
Reasons: http-<code>, timeout, network, error-page, redirect, robots.
"detail" is optional (redirect target, error message, template name).
The file is deleted once the page is fetched successfully.

Retry back-off: after N failed attempts a page is not tried again until
    last failure + base * 2^(N-1)       (capped at 30 days)
where base is 1 hour for temporary problems (5xx, timeouts, error pages) and
1 day for ones that look permanent (401/403/404/410, redirect, robots.txt).
So a page that keeps failing is tried less and less often, never in a tight loop.

The to-crawl list is rebuilt from the stored pages on every run:
    * the index and every stored forum page: every link to a forum or thread
      (any page) on the same site is wanted;
    * page 1 of every stored thread: its page navigation says how many pages
      the thread has (e.g. 1 2 3 ... 48 -> pages 1..48 are wanted);
    * for any forum or thread, if page N is known, pages 1..N are wanted.
    Wanted pages that are not stored, and not waiting out a back-off, are
    the to-crawl list.
"""
import gzip
import heapq
import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urljoin, urlparse

STORE_DIR = ".crawl-store"
PAGES_DIR = os.path.join(STORE_DIR, "pages")
CONFIG_PATH = os.path.join(STORE_DIR, "config.json")

PRIORITY = {"index": 0, "forum": 1, "thread": 2}
PERMANENT_REASONS = {"http-401", "http-403", "http-404", "http-410", "redirect", "robots"}
BACKOFF_TEMPORARY = timedelta(hours=1)
BACKOFF_PERMANENT = timedelta(days=1)
BACKOFF_MAX = timedelta(days=30)

ROUTE_RE = re.compile(r"^(.*?(threads|forums)/(?:[^/?#&]*\.)?(\d+)/)(?:page-(\d+))?$")
HREF_RE = re.compile(r'href="([^"]+)"')
TEMPLATE_RE = re.compile(r'<html\b[^>]*\bdata-template="([^"]*)"', re.I)
FILE_RE = re.compile(r"^(?:page-(\d+)|index)\.(html\.gz|failed\.jsonl)$")


def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def is_permanent(reason):
    return reason in PERMANENT_REASONS


def backoff(attempts, reason):
    base = BACKOFF_PERMANENT if is_permanent(reason) else BACKOFF_TEMPORARY
    return min(base * 2 ** min(max(attempts - 1, 0), 20), BACKOFF_MAX)   # exponent capped: no overflow


# --------------------------------------------------------------------------
# URLs
# --------------------------------------------------------------------------
def normalize(url):
    """Drop #fragment and ?filters, but keep XenForo routes in the query string."""
    url = url.split("#", 1)[0]
    if "?" in url:
        path, query = url.split("?", 1)
        route = query.split("&", 1)[0]
        url = f"{path}?{route}" if route.startswith(("threads/", "forums/")) else path
    return url


def classify(url):
    """(kind, id, page, page1_url) for a forum/thread page URL, else None.
    Rejects post permalinks, /unread, /reply, /latest etc."""
    m = ROUTE_RE.match(normalize(url))
    if not m:
        return None
    base, kind, item_id, page = m.groups()
    return ("thread" if kind == "threads" else "forum"), int(item_id), int(page or 1), base


def page_url(page1_url, page):
    return page1_url if page == 1 else f"{page1_url}page-{page}"


def make_key(kind, item_id, page):
    return f"{kind}:{item_id}:{page}"


def split_key(key):
    kind, item_id, page = key.split(":")
    return kind, int(item_id), int(page)


# --------------------------------------------------------------------------
# Files in .crawl-store/pages
# --------------------------------------------------------------------------
DIR_RE = re.compile(r"^(?:(.+)\.)?(\d+)$")
SLUG_RE = re.compile(r"(?:threads|forums)/(?:([^/?#&]*)\.)?\d+/$")
_dirs = {}   # (kind, id) -> its folder; filled by scan_store() and as folders are made


def slug_of(page1_url):
    """'.../forums/french-english.46/' -> 'french-english'; None if the URL has no words."""
    m = SLUG_RE.search(page1_url or "")
    if not m or not m.group(1):
        return None
    slug = re.sub(r"[^\w-]+", "-", unquote(m.group(1))).strip("-.")[:120]
    return slug or None


def _dir_name(item_id, slug):
    return f"{slug}.{item_id}" if slug else str(item_id)


def _parent(kind, item_id):
    if kind == "thread":
        return os.path.join(PAGES_DIR, "threads", str(item_id // 1000))
    return os.path.join(PAGES_DIR, "forums")


def _merge_dirs(src, dst):
    """Move src's files into dst (dst's copy wins), then remove src."""
    os.makedirs(dst, exist_ok=True)
    for f in os.scandir(src):
        target = os.path.join(dst, f.name)
        if os.path.exists(target):
            os.remove(f.path)
        else:
            os.replace(f.path, target)
    os.rmdir(src)


def rename_item(kind, item_id, slug):
    """Name an item's folder after its (new) slug."""
    cur = _dirs.get((kind, item_id))
    new = os.path.join(_parent(kind, item_id), _dir_name(item_id, slug))
    if cur and cur != new and os.path.isdir(cur):
        if os.path.exists(new):
            _merge_dirs(cur, new)
        else:
            os.rename(cur, new)
    _dirs[(kind, item_id)] = new
    return new


def item_dir(kind, item_id, slug=None):
    """Folder of a forum/thread: the existing one (named after slug if it was still
    just <id>), or where a new one goes."""
    if kind == "index":
        return PAGES_DIR
    cur = _dirs.get((kind, item_id))
    if cur is None:
        cur = _dirs[(kind, item_id)] = os.path.join(_parent(kind, item_id), _dir_name(item_id, slug))
    elif slug and DIR_RE.match(os.path.basename(cur)).group(1) is None:
        cur = rename_item(kind, item_id, slug)
    return cur


def _base_path(kind, item_id, page, slug=None):
    if kind == "index":
        return os.path.join(PAGES_DIR, "index")
    return os.path.join(item_dir(kind, item_id, slug), f"page-{page:04d}")


def store_path(kind, item_id, page, slug=None):
    return _base_path(kind, item_id, page, slug) + ".html.gz"


def failed_path(kind, item_id, page, slug=None):
    return _base_path(kind, item_id, page, slug) + ".failed.jsonl"


def read_page(path):
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        return f.read()


def write_page(path, text):
    """Atomic: a crash leaves either no file or a complete one, never half a page."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(gzip.compress(text.encode("utf-8"), compresslevel=6, mtime=0))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def append_failure(kind, item_id, page, reason, url="", detail="", when=None, slug=None):
    path = failed_path(kind, item_id, page, slug)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rec = {"time": (when or now()).isoformat(), "reason": reason}
    if url:
        rec["url"] = url
    if detail:
        rec["detail"] = str(detail)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_failures(path):
    """[(datetime, reason, detail), ...] oldest first; skips a torn or bad line."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
                out.append((datetime.fromisoformat(rec["time"]), rec["reason"], rec.get("detail", "")))
            except (ValueError, KeyError, TypeError):
                continue
    return out


def clear_failures(kind, item_id, page):
    try:
        os.remove(failed_path(kind, item_id, page))
    except FileNotFoundError:
        pass


def failure_summary(history):
    when, reason, detail = history[-1]
    return {"attempts": len(history), "last_time": when, "reason": reason, "detail": detail,
            "next_try": when + backoff(len(history), reason)}


def scan_store():
    """Walk .crawl-store/pages. Returns (stored, failures):
         stored:   set of (kind, id, page) with a stored page
         failures: key -> failure_summary() for pages that only have a .failed.jsonl file
       Removes leftover .part files, and .failed.jsonl files of pages that are stored."""
    stored, failed = set(), []
    _dirs.clear()
    found = []
    forums = os.path.join(PAGES_DIR, "forums")
    if os.path.isdir(forums):
        found += [("forum", d) for d in os.scandir(forums) if d.is_dir()]
    threads = os.path.join(PAGES_DIR, "threads")
    if os.path.isdir(threads):
        for group in list(os.scandir(threads)):
            if group.is_dir() and group.name.isdigit():
                found += [("thread", d) for d in os.scandir(group.path) if d.is_dir()]
    for kind, d in found:
        m = DIR_RE.match(d.name)
        if not m:
            continue
        key = (kind, int(m.group(2)))
        prev = _dirs.get(key)
        if prev is None:
            _dirs[key] = d.path
        else:  # two folders for one item (e.g. an interrupted rename): keep the named one
            keep, drop = (d.path, prev) if m.group(1) else (prev, d.path)
            _merge_dirs(drop, keep)
            _dirs[key] = keep
    dirs = [("index", 0, PAGES_DIR)] + [(k, i, p) for (k, i), p in _dirs.items()]
    for kind, item_id, path in dirs:
        if not os.path.isdir(path):
            continue
        for f in list(os.scandir(path)):
            if f.name.endswith(".part"):
                os.remove(f.path)
                continue
            m = FILE_RE.match(f.name)
            if not m or (kind == "index") != (m.group(1) is None):
                continue
            page, ext = int(m.group(1) or 1), m.group(2)
            if ext == "html.gz":
                stored.add((kind, item_id, page))
            else:
                failed.append((kind, item_id, page))
    failures = {}
    for kind, item_id, page in set(failed):
        if (kind, item_id, page) in stored:      # fetched fine later
            clear_failures(kind, item_id, page)
            continue
        path = failed_path(kind, item_id, page)
        history = read_failures(path) if os.path.exists(path) else []
        if history:
            failures[make_key(kind, item_id, page)] = failure_summary(history)
    return stored, failures


def hrefs(text):
    return [html.unescape(h) for h in HREF_RE.findall(text)]


def template_of(text):
    m = TEMPLATE_RE.search(text[:5000])
    return m.group(1) if m else None


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def old_layout():
    """What is out of date if this directory holds a store from an earlier version, else None."""
    if os.path.isdir(".state"):
        return "a .state/ folder from an earlier version"
    if os.path.exists(os.path.join(STORE_DIR, "errors.jsonl")):
        return ".crawl-store/errors.jsonl from an earlier version"
    threads = os.path.join(PAGES_DIR, "threads")
    if os.path.isdir(threads):
        entries = list(os.scandir(threads))     # check all of them: listing order varies by filesystem
        if any(d.name.startswith("_moving.") for d in entries):
            return "an unfinished migration"
        for d in entries:
            if d.is_dir() and d.name.isdigit():
                first = next(os.scandir(d.path), None)
                if first is not None and first.is_file():
                    return "thread folders not yet grouped by 1000"
    return None


def load_config():
    reason = old_layout()
    if reason:
        raise SystemExit(f"This crawl store has {reason}.\n"
                         "Back it up, then run: uv run xf-migrate")
    if not os.path.exists(CONFIG_PATH):
        return None
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    os.makedirs(STORE_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


# --------------------------------------------------------------------------
# The to-crawl plan, rebuilt from the stored pages
# --------------------------------------------------------------------------
class Plan:
    def __init__(self, start_url, ignore_backoff=False):
        self.start_url = start_url
        self.host = urlparse(start_url).netloc
        self.ignore_backoff = ignore_backoff
        self.items = {}            # (kind, id) -> {"base": page-1 URL or None, "max": highest page known}
        self.stored, self.failures = scan_store()
        self.heap, self.queued = [], set()
        self.waiting = set()       # keys skipped because their back-off has not expired
        self.prefix = None         # e.g. "http://localhost/forum/" — for building URLs from IDs

    # -- failures ------------------------------------------------------------
    def backing_off(self, key):
        f = self.failures.get(key)
        return bool(f) and not self.ignore_backoff and now() < f["next_try"]

    def record_failure(self, kind, item_id, page, reason, url="", detail=""):
        """Append to the page's .failed.jsonl file; returns the updated summary."""
        append_failure(kind, item_id, page, reason, url, detail, slug=self.slug(kind, item_id))
        key = make_key(kind, item_id, page)
        attempts = self.failures.get(key, {}).get("attempts", 0) + 1
        t = now()
        self.failures[key] = {"attempts": attempts, "last_time": t, "reason": reason,
                              "detail": detail, "next_try": t + backoff(attempts, reason)}
        return self.failures[key]

    # -- adding pages --------------------------------------------------------
    def _want(self, kind, item_id, page):
        key = make_key(kind, item_id, page)
        if (kind, item_id, page) in self.stored or key in self.queued:
            return 0
        if self.backing_off(key):
            self.waiting.add(key)
            return 0
        heapq.heappush(self.heap, (PRIORITY[kind], item_id, page, kind))
        self.queued.add(key)
        return 1

    def _note(self, kind, item_id, page, base):
        it = self.items.setdefault((kind, item_id), {"base": None, "max": 0})
        if base and not it["base"]:
            it["base"] = base
            if self.prefix is None:
                self.prefix = re.split(r"(?:threads|forums)/", base, maxsplit=1)[0]
        new = 0
        if page > it["max"]:
            for p in range(it["max"] + 1, page + 1):
                new += self._want(kind, item_id, p)
            it["max"] = page
        return new

    def note_url(self, url):
        c = classify(url)
        if c and urlparse(c[3]).netloc == self.host:
            return self._note(*c)
        return 0

    def discover(self, kind, item_id, page, text, url):
        """Links that count: everything on index/forum pages; on a thread, only
        page 1's links to its own pages (its page navigation)."""
        if kind == "thread" and page != 1:
            return 0
        new = 0
        for h in hrefs(text):
            c = classify(urljoin(url, h))
            if not c or urlparse(c[3]).netloc != self.host:
                continue
            if kind == "thread" and (c[0], c[1]) != ("thread", item_id):
                continue
            new += self._note(*c)
        return new

    def slug(self, kind, item_id):
        return slug_of(self.items.get((kind, item_id), {}).get("base"))

    def page_path(self, kind, item_id, page):
        """Where to store a page, in a folder named after the item's slug when known."""
        return store_path(kind, item_id, page, self.slug(kind, item_id))

    def seen_at(self, kind, item_id, page1_url):
        """The site just served this item at page1_url (after any redirect): its URL
        and slug are current, so use them from now on and rename the folder if needed."""
        it = self.items.setdefault((kind, item_id), {"base": None, "max": 0})
        it["base"] = page1_url
        slug = slug_of(page1_url)
        cur = _dirs.get((kind, item_id))
        if slug and cur and os.path.basename(cur) != _dir_name(item_id, slug):
            rename_item(kind, item_id, slug)

    def mark_stored(self, kind, item_id, page):
        self.stored.add((kind, item_id, page))
        self.failures.pop(make_key(kind, item_id, page), None)
        clear_failures(kind, item_id, page)

    def url_for(self, kind, item_id, page):
        if kind == "index":
            return self.start_url
        base = self.items.get((kind, item_id), {}).get("base")
        if not base:  # XenForo redirects /threads/123/ to its full slug URL
            base = f"{self.prefix or self.start_url}{'threads' if kind == 'thread' else 'forums'}/{item_id}/"
        return page_url(base, page)

    # -- rebuild + iterate ---------------------------------------------------
    def rebuild(self, progress=None):
        self._want("index", 0, 1)
        for kind, item_id, page in self.stored:     # stored pages are known pages
            if kind != "index":
                self._note(kind, item_id, page, None)
        to_read = sorted((k for k in self.stored if k[0] != "thread" or k[2] == 1),
                         key=lambda k: (PRIORITY[k[0]], k[1], k[2]))
        for i, (kind, item_id, page) in enumerate(to_read, 1):
            text = read_page(store_path(kind, item_id, page))
            self.discover(kind, item_id, page, text, self.url_for(kind, item_id, page))
            if progress and i % 2000 == 0:
                progress(i, len(to_read))
        for f in list(self.failures.values()):      # where redirected pages pointed to
            if f["reason"] == "redirect" and f["detail"]:
                self.note_url(f["detail"])
        return len(to_read)

    def pop(self):
        if not self.heap:
            return None
        _, item_id, page, kind = heapq.heappop(self.heap)
        return kind, item_id, page, self.url_for(kind, item_id, page)

    def requeue(self, kind, item_id, page):
        """Put back a page that was popped but not attempted (e.g. proxy down)."""
        self.queued.discard(make_key(kind, item_id, page))
        self._want(kind, item_id, page)

    def pending(self):
        return sorted(self.heap)

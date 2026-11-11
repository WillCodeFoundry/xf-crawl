#!/usr/bin/env python3
"""
xf-mine — analyse a forum downloaded by xf-crawl, entirely from the
pages in ./.crawl-store (never touches the network).

    xf-mine            text summary
    xf-mine --json     same numbers as JSON
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .common import Plan, classify, is_permanent, load_config, read_page, store_path


def soup_at(kind, item_id, page):
    return BeautifulSoup(read_page(store_path(kind, item_id, page)), "html.parser")


def parse_count(text):
    """'1,234' -> (1234, False); '1.2K' -> (1200, True)."""
    t = re.sub(r"[,\s  ]", "", text)
    m = re.match(r"^([\d.]+)([KkMm]?)$", t)
    if not m:
        return None, False
    mult = {"k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1)
    return int(float(m.group(1)) * mult), mult != 1


def thread_listing(soup):
    rows = []
    for item in soup.select(".structItem--thread"):
        box = item.select_one(".structItem-title")
        if not box or not box.find("a"):
            continue
        link = box.select_one("a[data-tp-primary]") or box.find_all("a")[-1]
        dds = item.select(".structItem-cell--meta dl dd")  # 1st = replies, 2nd = views
        replies, approx = parse_count(dds[0].get_text()) if dds else (None, False)
        rows.append({"href": link["href"], "title": link.get_text(strip=True),
                     "replies": replies, "replies_approx": approx})
    return rows


def mine():
    cfg = load_config()
    if cfg is None:
        sys.exit("No .crawl-store directory here — run xf-crawl first.")
    start = cfg["start_url"]

    plan = Plan(start)          # same offline rebuild the crawler does
    plan.rebuild()
    stored = plan.stored

    # Forums: title + threads listed on their stored listing pages
    forums, listed = {}, {}
    for (kind, fid), it in sorted(plan.items.items()):
        if kind != "forum":
            continue
        pages = sorted(p for k, i, p in stored if k == "forum" and i == fid)
        title = None
        seen = set()
        for p in pages:
            soup = soup_at("forum", fid, p)
            if p == 1:
                el = soup.select_one("h1.p-title-value")
                title = el.get_text(" ", strip=True) if el else None
            for t in thread_listing(soup):
                c = classify(urljoin(start, t["href"]))
                if c and c[0] == "thread" and c[1] not in seen:
                    seen.add(c[1])
                    listed.setdefault(c[1], {**t, "forum_id": fid})
        forums[fid] = {"forum_id": fid, "title": title or f"forum {fid}",
                       "pages_stored": len(pages), "pages_total": it["max"],
                       "threads_listed": len(seen), "threads_downloaded": 0}

    # Threads: completeness + posts
    threads_known = [i for (k, i) in plan.items if k == "thread"]
    downloaded = complete = pages_stored = posts = 0
    incomplete = []
    for tid in sorted(threads_known):
        # pages that 404'd or redirected elsewhere (e.g. stale links past the end) don't count
        total = sum(1 for p in range(1, plan.items[("thread", tid)]["max"] + 1)
                    if ("thread", tid, p) in stored
                    or not is_permanent(plan.failures.get(f"thread:{tid}:{p}", {}).get("reason")))
        have = sorted(p for k, i, p in stored if k == "thread" and i == tid)
        if not have:
            continue
        downloaded += 1
        pages_stored += len(have)
        posts += sum(len(soup_at("thread", tid, p).select("article.message--post")) for p in have)
        if len(have) == total:
            complete += 1
        else:
            incomplete.append({"thread_id": tid, "pages_stored": len(have), "pages_total": total})
        if tid in listed and listed[tid]["forum_id"] in forums:
            forums[listed[tid]["forum_id"]]["threads_downloaded"] += 1

    errs = defaultdict(int)          # pages with a .failed.jsonl file, by latest reason
    for f in plan.failures.values():
        errs[f["reason"]] += 1

    return {
        "forum": start,
        "forums": len(forums),
        "threads_known": len(threads_known),
        "threads_listed": len(listed),
        "threads_downloaded": downloaded,
        "threads_complete": complete,
        "threads_incomplete": len(incomplete),
        "thread_pages_stored": pages_stored,
        "posts_stored": posts,
        "replies_per_listings": sum(t["replies"] or 0 for t in listed.values()),
        "replies_approx": any(t["replies_approx"] for t in listed.values()),
        "pages_stored": len(stored),
        "pages_to_crawl": len(plan.heap),
        "pages_failed": dict(sorted(errs.items())),
        "pages_failed_waiting_retry": len(plan.waiting),
        "per_forum": sorted(forums.values(), key=lambda f: -f["threads_listed"]),
        "incomplete_threads": incomplete,
    }


def print_report(s):
    print(f"Forum: {s['forum']}\n")
    print(f"  Forums                     {s['forums']:>8,}")
    print(f"  Threads known              {s['threads_known']:>8,}")
    print(f"  Threads downloaded         {s['threads_downloaded']:>8,}   "
          f"({s['threads_complete']:,} complete, {s['threads_incomplete']:,} missing pages)")
    print(f"  Thread pages downloaded    {s['thread_pages_stored']:>8,}")
    print(f"  Posts in downloaded pages  {s['posts_stored']:>8,}")
    approx = "  (some rounded, e.g. 1.2K)" if s["replies_approx"] else ""
    print(f"  Replies per forum listings {s['replies_per_listings']:>8,}{approx}")

    print(f"\n  Crawl: {s['pages_stored']:,} pages stored, {s['pages_to_crawl']:,} still to crawl")
    if s["pages_failed"]:
        print("  Failed pages: " + ", ".join(f"{n:,} {k}" for k, n in s["pages_failed"].items())
              + f"  ({s['pages_failed_waiting_retry']:,} waiting for their next retry)")
    if s["pages_to_crawl"]:
        print("  (crawl not finished — numbers cover what is downloaded so far)")

    if s["per_forum"]:
        print("\n  Threads per forum:")
        w = max(len(f["title"]) for f in s["per_forum"])
        print(f"    {'Forum':<{w}}  {'Listed':>7}  {'Downloaded':>10}")
        for f in s["per_forum"]:
            print(f"    {f['title']:<{w}}  {f['threads_listed']:>7,}  {f['threads_downloaded']:>10,}")

    if s["incomplete_threads"]:
        print(f"\n  Threads with missing pages (first 10 of {len(s['incomplete_threads'])}):")
        for t in s["incomplete_threads"][:10]:
            print(f"    thread {t['thread_id']}: {t['pages_stored']} of {t['pages_total']} pages")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="Output JSON instead of a text report")
    args = ap.parse_args()
    stats = mine()
    if args.json:
        print(json.dumps(stats, indent=2, ensure_ascii=False))
    else:
        print_report(stats)


if __name__ == "__main__":
    main()

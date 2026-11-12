from pathlib import Path

from xfcrawl.link_index import ItemLink, LinkIndex, PageLinks
from xfcrawl.urls import INDEX_PAGE, ItemRef, Kind

FORUM_PAGE = ItemRef(Kind.FORUM, 2).page(1)
THREAD = ItemRef(Kind.THREAD, 10)


def entry(page, *links, title=None):
    return PageLinks(page, title, tuple(links))


def test_entries_round_trip():
    index = LinkIndex(Path("links.jsonl"))
    written = [entry(INDEX_PAGE, ItemLink(ItemRef(Kind.FORUM, 2), 1, "/forums/general.2/")),
               entry(FORUM_PAGE, ItemLink(THREAD, 7, "/threads/t.10/"), title="Général & co")]
    index.record_all(written)
    assert list(index.entries()) == written


def test_entries_come_back_in_the_order_written():
    index = LinkIndex(Path("links.jsonl"))
    index.record(entry(FORUM_PAGE, ItemLink(THREAD, 2, "/threads/old.10/")))
    index.record(entry(FORUM_PAGE, ItemLink(THREAD, 7, "/threads/new.10/")))
    assert [e.links[0].last_page for e in index.entries()] == [2, 7]


def test_a_line_cut_short_by_a_crash_is_skipped():
    path = Path("links.jsonl")
    LinkIndex(path).record(entry(INDEX_PAGE))
    with path.open("a") as file:
        file.write('{"page": "forum:2:1", "links": [["thre')
    assert [e.page for e in LinkIndex(path).entries()] == [INDEX_PAGE]


def test_no_index_yet_is_empty():
    assert list(LinkIndex(Path("missing.jsonl")).entries()) == []

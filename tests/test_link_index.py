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
    assert index.load() == {e.page: e for e in written}


def test_the_latest_entry_for_a_page_wins():
    index = LinkIndex(Path("links.jsonl"))
    index.record(entry(FORUM_PAGE, ItemLink(THREAD, 2, "/threads/old.10/")))
    index.record(entry(FORUM_PAGE, ItemLink(THREAD, 7, "/threads/new.10/")))
    assert index.load()[FORUM_PAGE].links == (ItemLink(THREAD, 7, "/threads/new.10/"),)


def test_a_line_cut_short_by_a_crash_is_skipped():
    path = Path("links.jsonl")
    LinkIndex(path).record(entry(INDEX_PAGE))
    with path.open("a") as file:
        file.write('{"page": "forum:2:1", "links": [["thre')
    assert list(LinkIndex(path).load()) == [INDEX_PAGE]


def test_no_index_yet_is_empty():
    assert LinkIndex(Path("missing.jsonl")).load() == {}

import pytest

from xfcrawl.urls import INDEX_PAGE, ItemRef, Kind, PageRef, normalize, page_url, parse_page_url, slug_of


@pytest.mark.parametrize(("url", "ref", "first_page_url"), [
    ("http://x/threads/hello.12/", PageRef(Kind.THREAD, 12, 1), "http://x/threads/hello.12/"),
    ("http://x/threads/hello.12/page-7", PageRef(Kind.THREAD, 12, 7), "http://x/threads/hello.12/"),
    ("http://x/forums/general.2/page-3", PageRef(Kind.FORUM, 2, 3), "http://x/forums/general.2/"),
    ("http://x/threads/12/", PageRef(Kind.THREAD, 12, 1), "http://x/threads/12/"),
    ("http://x/forum/index.php?threads/hello.12/page-2", PageRef(Kind.THREAD, 12, 2),
     "http://x/forum/index.php?threads/hello.12/"),
    ("http://x/forums/general.2/?prefix_id=1", PageRef(Kind.FORUM, 2, 1), "http://x/forums/general.2/"),
    ("http://x/threads/hello.12/#post-5", PageRef(Kind.THREAD, 12, 1), "http://x/threads/hello.12/"),
])
def test_page_urls_are_recognised(url, ref, first_page_url):
    link = parse_page_url(url)
    assert link is not None
    assert (link.ref, link.first_page_url) == (ref, first_page_url)


@pytest.mark.parametrize("url", [
    "http://x/threads/hello.12/post-345",
    "http://x/threads/hello.12/unread",
    "http://x/threads/hello.12/reply",
    "http://x/members/bob.3/",
    "http://x/",
])
def test_other_urls_are_not_pages(url):
    assert parse_page_url(url) is None


def test_normalize_keeps_only_a_route_from_the_query():
    assert normalize("http://x/index.php?threads/a.1/&foo=1#x") == "http://x/index.php?threads/a.1/"
    assert normalize("http://x/forums/a.1/?order=title") == "http://x/forums/a.1/"


def test_page_url():
    assert page_url("http://x/threads/a.1/", 1) == "http://x/threads/a.1/"
    assert page_url("http://x/threads/a.1/", 4) == "http://x/threads/a.1/page-4"


@pytest.mark.parametrize(("url", "slug"), [
    ("http://x/forums/french-english.46/", "french-english"),
    ("http://x/threads/parler-le-fran%C3%A7ais.1234567/", "parler-le-français"),
    ("http://x/threads/12/", None),
    ("http://x/threads/we-ird%2Fslug.5/", "we-ird-slug"),
])
def test_slug_of(url, slug):
    assert slug_of(url) == slug


def test_every_threads_first_page_comes_before_any_later_page():
    refs = [PageRef(Kind.THREAD, 1, 2), PageRef(Kind.THREAD, 99, 1), PageRef(Kind.THREAD, 1, 1),
            PageRef(Kind.FORUM, 5, 3)]
    assert sorted(refs, key=PageRef.crawl_order) == [
        PageRef(Kind.FORUM, 5, 3), PageRef(Kind.THREAD, 1, 1), PageRef(Kind.THREAD, 99, 1), PageRef(Kind.THREAD, 1, 2)]


def test_crawl_order_is_index_then_forums_then_threads():
    refs = [PageRef(Kind.THREAD, 1, 2), PageRef(Kind.FORUM, 9, 1), INDEX_PAGE, PageRef(Kind.THREAD, 1, 1)]
    assert sorted(refs, key=PageRef.crawl_order) == [
        INDEX_PAGE, PageRef(Kind.FORUM, 9, 1), PageRef(Kind.THREAD, 1, 1), PageRef(Kind.THREAD, 1, 2)]


def test_page_keys():
    assert ItemRef(Kind.THREAD, 12).page(3).key == "thread:12:3"


def test_a_page_title_is_read_without_its_markup():
    from xfcrawl import xenforo

    assert xenforo.title('<h1 class="p-title-value"><span class="label">Q</span> Fran&ccedil;ais\n seulement</h1>') \
        == "Q Français seulement"
    assert xenforo.title("<h1>no class</h1>") is None

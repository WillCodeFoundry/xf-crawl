from datetime import timedelta

import pytest

from xfcrawl.common import backoff, classify, normalize, page_url, slug_of
from xfcrawl.mine import parse_count


@pytest.mark.parametrize("url, expected", [
    ("http://x/threads/hello.12/", ("thread", 12, 1, "http://x/threads/hello.12/")),
    ("http://x/threads/hello.12/page-7", ("thread", 12, 7, "http://x/threads/hello.12/")),
    ("http://x/forums/general.2/page-3", ("forum", 2, 3, "http://x/forums/general.2/")),
    ("http://x/threads/12/", ("thread", 12, 1, "http://x/threads/12/")),
    ("http://x/forum/index.php?threads/hello.12/page-2", ("thread", 12, 2, "http://x/forum/index.php?threads/hello.12/")),
    ("http://x/forums/general.2/?prefix_id=1", ("forum", 2, 1, "http://x/forums/general.2/")),
    ("http://x/threads/hello.12/#post-5", ("thread", 12, 1, "http://x/threads/hello.12/")),
])
def test_classify_pages(url, expected):
    assert classify(url) == expected


@pytest.mark.parametrize("url", [
    "http://x/threads/hello.12/post-345",
    "http://x/threads/hello.12/unread",
    "http://x/threads/hello.12/reply",
    "http://x/members/bob.3/",
    "http://x/",
])
def test_classify_rejects_non_pages(url):
    assert classify(url) is None


def test_normalize_keeps_route_in_query_only():
    assert normalize("http://x/index.php?threads/a.1/&foo=1#x") == "http://x/index.php?threads/a.1/"
    assert normalize("http://x/forums/a.1/?order=title") == "http://x/forums/a.1/"


def test_page_url():
    assert page_url("http://x/threads/a.1/", 1) == "http://x/threads/a.1/"
    assert page_url("http://x/threads/a.1/", 4) == "http://x/threads/a.1/page-4"


@pytest.mark.parametrize("url, slug", [
    ("http://x/forums/french-english.46/", "french-english"),
    ("http://x/threads/parler-le-fran%C3%A7ais.1234567/", "parler-le-français"),
    ("http://x/threads/12/", None),
    ("http://x/threads/we-ird%2Fslug.5/", "we-ird-slug"),
    (None, None),
])
def test_slug_of(url, slug):
    assert slug_of(url) == slug


def test_backoff_doubles_and_caps():
    assert backoff(1, "http-503") == timedelta(hours=1)
    assert backoff(3, "http-503") == timedelta(hours=4)
    assert backoff(1, "http-404") == timedelta(days=1)
    assert backoff(2, "redirect") == timedelta(days=2)
    assert backoff(50, "http-503") == timedelta(days=30)


@pytest.mark.parametrize("text, expected", [
    ("1,234", (1234, False)),
    ("1.2K", (1200, True)),
    ("3M", (3_000_000, True)),
    ("1 045", (1045, False)),
    ("n/a", (None, False)),
])
def test_parse_count(text, expected):
    assert parse_count(text) == expected

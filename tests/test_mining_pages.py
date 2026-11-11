import pytest

from xfcrawl.mining import pages
from xfcrawl.mining.pages import Count


@pytest.mark.parametrize(("text", "count"), [
    ("1,234", Count(1234, approximate=False)),
    ("1.2K", Count(1200, approximate=True)),
    ("3M", Count(3_000_000, approximate=True)),
    ("1\u202f045", Count(1045, approximate=False)),
    ("n/a", None),
])
def test_parse_count(text, count):
    assert pages.parse_count(text) == count


def test_listed_threads_take_the_primary_link_not_the_prefix():
    page = pages.parse(
        '<div class="structItem structItem--thread"><div class="structItem-title">'
        '<a href="/forums/f.1/?prefix_id=2">Q</a><a href="/threads/t.7/" data-tp-primary="on">Seven</a></div>'
        '<div class="structItem-cell--meta"><dl><dd>1.2K</dd></dl><dl><dd>9</dd></dl></div></div>'
    )
    assert pages.listed_threads(page) == [pages.ListedThread("/threads/t.7/", "Seven", Count(1200, approximate=True))]

"""Reading the cookie a site's script check asks for."""

from xfcrawl.cookie_check import AskedCookie, asked_for

WORDREFERENCE = ('<script>document.cookie = "nginx_wr_human=1; Path=/; Domain=wordreference.com;"; '
                 'window.location.reload();</script>')
THREAD = "https://forum.wordreference.com/threads/x.1/"


def test_wordreferences_check_is_read():
    assert asked_for(WORDREFERENCE, THREAD) == AskedCookie("nginx_wr_human", "1", ".wordreference.com", "/")


def test_a_cookie_without_a_domain_is_for_the_host_alone():
    page = "<script>document.cookie='seen=yes'; location.reload();</script>"
    assert asked_for(page, THREAD) == AskedCookie("seen", "yes", "forum.wordreference.com", "/")


def test_a_cookie_for_another_site_is_ignored():
    assert asked_for(WORDREFERENCE, "https://notwordreference.com/") is None
    assert asked_for(WORDREFERENCE, "https://example.com/") is None


def test_pages_that_are_not_checks_are_left_alone():
    assert asked_for("<p>Forbidden</p>", THREAD) is None
    assert asked_for('<script>document.cookie = "a=1";</script>', THREAD) is None        # no reload
    assert asked_for("<script>location.reload();</script>", THREAD) is None             # no cookie
    assert asked_for('<script>document.cookie = "no-value";location.reload()</script>', THREAD) is None
    assert asked_for(WORDREFERENCE + "<p>" + "x" * 3000 + "</p>", THREAD) is None        # a real page

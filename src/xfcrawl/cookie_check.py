"""A site's check that a visitor runs scripts: a short page that only sets a cookie and reloads.

WordReference answers 418 with ``<script>document.cookie = "nginx_wr_human=1; Path=/; Domain=wordreference.com;";
window.location.reload();</script>``. A browser runs it without anyone noticing; ``asked_for`` reads the cookie
from such a page, so the fetcher can set it and ask again, as the browser would.
"""

import re
from dataclasses import dataclass
from urllib.parse import urlparse

_LONGEST_CHECK_PAGE = 2000
"""A check page is a line of script; a longer page is a real one, whatever scripts it has."""
_SETS_COOKIE = re.compile(r"""document\.cookie\s*=\s*(["'])(?P<cookie>[^"']+)\1""")
_RELOADS = re.compile(r"location\.reload\(|location\.href\s*=|location\.replace\(")


@dataclass(frozen=True, slots=True)
class AskedCookie:
    name: str
    value: str
    domain: str
    """Where the cookie goes, as a cookie jar takes it: ``.example.com`` for a site and its subdomains."""
    path: str


def asked_for(body: str, url: str) -> AskedCookie | None:
    """The cookie a check page at ``url`` sets before reloading, or None if ``body`` isn't one. A cookie
    for another site than ``url``'s is ignored, as a browser would ignore it."""
    if len(body) > _LONGEST_CHECK_PAGE or not _RELOADS.search(body):
        return None
    found = _SETS_COOKIE.search(body)
    if found is None:
        return None
    pair, *attributes = (part.strip() for part in found["cookie"].split(";"))
    name, equals, value = pair.partition("=")
    if not equals or not name:
        return None
    settings = {key.strip().lower(): setting.strip() for key, _, setting in (a.partition("=") for a in attributes)}
    host = urlparse(url).hostname or ""
    domain = settings.get("domain", "").lstrip(".").lower()
    if domain and host != domain and not host.endswith("." + domain):
        return None
    return AskedCookie(name.strip(), value.strip(), f".{domain}" if domain else host, settings.get("path") or "/")

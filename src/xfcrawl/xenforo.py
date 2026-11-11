"""What the crawler reads from a XenForo page's HTML: its links, and whether it is a real page.

Kept to regular expressions over the raw text: the crawler reads every stored listing page and thread's
first page on each start, so this has to be fast. The fuller parsing, for mining, is in ``mining.pages``.
"""

import html
import re

_HREF = re.compile(r'href="([^"]+)"')
_TEMPLATE = re.compile(r'<html\b[^>]*\bdata-template="([^"]*)"', re.IGNORECASE)
_TEMPLATE_WITHIN = 5000  # the <html> tag is at the top
_END_WITHIN = 2000       # and </html> at the bottom

# XenForo serves these with status 200; their content is not the page asked for.
ERROR_TEMPLATES = frozenset({"error", "login", "error_with_login", "message_page"})


def links(page_html: str) -> list[str]:
    return [html.unescape(href) for href in _HREF.findall(page_html)]


def template(page_html: str) -> str | None:
    """XenForo's name for the page's template, from ``<html data-template="thread_view">``."""
    match = _TEMPLATE.search(page_html[:_TEMPLATE_WITHIN])
    return match.group(1) if match else None


def is_complete(page_html: str) -> bool:
    """Whether the page arrived whole, ending with ``</html>``."""
    return "</html>" in page_html[-_END_WITHIN:].lower()

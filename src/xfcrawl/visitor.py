"""The visitor a crawl passes for, from run to run: its browser and the cookies the site gave it.

A person comes back to a site with the same browser and the site's cookies; a crawl does the same. The
visitor is saved in ``.crawl-store/visitor.json``, readable only by its owner since cookies can log someone
in, and is the only module that reads or writes that file.
"""

import json
import random
from dataclasses import dataclass
from datetime import date
from http.cookiejar import CookieJar
from pathlib import Path

from requests.cookies import RequestsCookieJar

from xfcrawl.atomic_files import atomic_write
from xfcrawl.browser import Browser
from xfcrawl.errors import XfError


@dataclass(slots=True)
class Visitor:
    browser: Browser
    cookies: RequestsCookieJar
    returning: bool
    """Whether it was loaded from an earlier run, rather than made new."""


class VisitorFile:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self, rng: random.Random, today: date) -> Visitor:
        """The saved visitor, its browser updated if it has fallen behind and its expired cookies dropped;
        or a new visitor if none is saved."""
        if not self._path.exists():
            return Visitor(Browser.pick(rng, today), RequestsCookieJar(), returning=False)
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            browser = Browser.from_json(data["browser"])
            cookies = _cookies_from_json(data["cookies"])
        except (ValueError, KeyError, TypeError) as error:
            raise XfError(f"{self._path} is damaged ({error}); run with --new-browser to start afresh") from error
        cookies.clear_expired_cookies()
        return Visitor(browser.updated(rng, today), cookies, returning=True)

    def save(self, visitor: Visitor) -> None:
        data = {"browser": visitor.browser.to_json(), "cookies": _cookies_to_json(visitor.cookies)}
        atomic_write(self._path, (json.dumps(data, indent=1) + "\n").encode(), mode=0o600)

    def forget(self) -> None:
        self._path.unlink(missing_ok=True)


def _cookies_to_json(jar: CookieJar) -> list[dict[str, object]]:
    return [
        {"name": cookie.name, "value": cookie.value, "domain": cookie.domain, "path": cookie.path,
         "expires": cookie.expires, "secure": cookie.secure}
        for cookie in jar
    ]


def _cookies_from_json(data: object) -> RequestsCookieJar:
    if not isinstance(data, list):
        raise ValueError("cookies are not a list")
    jar = RequestsCookieJar()
    for item in data:
        jar.set(item["name"], item["value"], domain=item["domain"], path=item["path"], expires=item["expires"],
                secure=item["secure"])
    return jar

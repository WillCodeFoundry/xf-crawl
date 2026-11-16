"""The browser the crawler passes for: a user agent and the headers that go with it, picked at random.

``Browser.pick`` chooses a common browser and system, with a version near the one current on the day, so the
user agent doesn't age as the code does. The other headers match what that browser sends for a page; opened
from a link on the site, a page also gets ``followed_from``'s headers. A crawl keeps its browser from run to
run (see ``visitor.py``), updating it as browsers update themselves.
"""

import random
from dataclasses import asdict, dataclass, replace
from datetime import date

# A release of each browser, and how often a new major version comes out. Versions are worked out from these,
# so they stay current; a guess that's a version or two off still looks like a browser that wasn't updated.
_CHROME_RELEASE = (136, date(2025, 4, 29))
_FIREFOX_RELEASE = (138, date(2025, 4, 29))
_DAYS_PER_VERSION = 28
_SAFARI_SHIPS_IN_SEPTEMBER = 9

_WINDOWS = "Windows NT 10.0; Win64; x64"
_MAC = "Macintosh; Intel Mac OS X 10_15_7"
_FIREFOX_MAC = "Macintosh; Intel Mac OS X 10.15"
_LINUX = "X11; Linux x86_64"

# Roughly how common each browser and system is on desktops.
_CHOICES: tuple[tuple[str, str, int], ...] = (
    ("Chrome", "Windows", 40),
    ("Chrome", "macOS", 14),
    ("Chrome", "Linux", 3),
    ("Edge", "Windows", 13),
    ("Firefox", "Windows", 7),
    ("Firefox", "macOS", 2),
    ("Firefox", "Linux", 3),
    ("Safari", "macOS", 10),
)
_LANGUAGES = (
    "en-US,en;q=0.9",
    "en-GB,en;q=0.9",
    "en-GB,en-US;q=0.9,en;q=0.8",
    "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "en-US,en;q=0.9,fr;q=0.8",
)
_HTML_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
_CHROME_ACCEPT = ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,"
                  "*/*;q=0.8,application/signed-exchange;v=b3;q=0.7")


@dataclass(frozen=True, slots=True)
class Browser:
    name: str
    """Chrome, Edge, Firefox or Safari."""
    system: str
    """Windows, macOS or Linux."""
    version: int
    language: str
    """The Accept-Language header."""
    minor: int = 0
    """Safari's minor version; the others show only a major one."""

    def describe(self) -> str:
        return f"{self.name} {self.version} on {self.system}"

    @classmethod
    def pick(cls, rng: random.Random, today: date) -> "Browser":
        name, system, _ = rng.choices(_CHOICES, weights=[weight for *_, weight in _CHOICES])[0]
        browser = cls(name, system, 0, rng.choice(_LANGUAGES))
        return browser.updated(rng, today, force=True)

    def updated(self, rng: random.Random, today: date, *, force: bool = False) -> "Browser":
        """The same browser, updated once it falls behind, as browsers update themselves: Safari when a new
        major version comes out each year, the others when more than two of their monthly versions behind.
        ``force`` updates it anyway. Chrome, Edge and Firefox may end up the latest or one or two behind."""
        if self.name == "Safari":
            newest, behind, allowed = safari_version(today), 0, 0
        else:
            newest = current_version(_FIREFOX_RELEASE if self.name == "Firefox" else _CHROME_RELEASE, today)
            behind, allowed = rng.choices((0, 1, 2), weights=(6, 3, 1))[0], 2
        if not force and self.version >= newest - allowed:
            return self
        minor = rng.choice((0, 1, 2, 3, 4)) if self.name == "Safari" else 0
        return replace(self, version=newest - behind, minor=minor)

    @property
    def user_agent(self) -> str:
        if self.name == "Firefox":
            platform = {"Windows": _WINDOWS, "macOS": _FIREFOX_MAC, "Linux": _LINUX}[self.system]
            return f"Mozilla/5.0 ({platform}; rv:{self.version}.0) Gecko/20100101 Firefox/{self.version}.0"
        if self.name == "Safari":
            return (f"Mozilla/5.0 ({_MAC}) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                    f"Version/{self.version}.{self.minor} Safari/605.1.15")
        platform = {"Windows": _WINDOWS, "macOS": _MAC, "Linux": _LINUX}[self.system]
        agent = (f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) "
                 f"Chrome/{self.version}.0.0.0 Safari/537.36")
        return agent + f" Edg/{self.version}.0.0.0" if self.name == "Edge" else agent

    @property
    def headers(self) -> dict[str, str]:
        """Every header to send with a page typed into the address bar, the user agent included."""
        return {
            "User-Agent": self.user_agent,
            "Accept": _HTML_ACCEPT if self.name in ("Firefox", "Safari") else _CHROME_ACCEPT,
            "Accept-Language": self.language,
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        }

    def to_json(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: object) -> "Browser":
        """A browser saved by ``to_json``; raises ValueError if it isn't one."""
        if not isinstance(data, dict):
            raise ValueError("not an object")
        name, system, version = data.get("name"), data.get("system"), data.get("version")
        language, minor = data.get("language"), data.get("minor", 0)
        if (name, system) not in {(n, s) for n, s, _ in _CHOICES}:
            raise ValueError(f"unknown browser {name!r} on {system!r}")
        if not isinstance(version, int) or not isinstance(minor, int) or not isinstance(language, str):
            raise ValueError("version, minor or language missing")
        return cls(str(name), str(system), version, language, minor)


def followed_from(referer: str | None) -> dict[str, str]:
    """The headers that change when a page is opened from a link on another page of the site."""
    return {} if referer is None else {"Referer": referer, "Sec-Fetch-Site": "same-origin"}


def current_version(release: tuple[int, date], today: date) -> int:
    version, released = release
    return version + max(0, (today - released).days) // _DAYS_PER_VERSION


def safari_version(today: date) -> int:
    """Safari 26 came with the systems of September 2025, and a new major version comes every September."""
    return today.year - 1999 - (today.month < _SAFARI_SHIPS_IN_SEPTEMBER)

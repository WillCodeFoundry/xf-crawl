"""The browser the crawler passes for: a user agent and the headers that go with it, picked at random.

``Browser.pick`` chooses a common browser and system, with a version near the one current on the day, so the
user agent doesn't age as the code does. The other headers match what that browser sends for a page opened
from the address bar. One browser is picked per run and kept for every request, as a person's browser would be.
"""

import random
from dataclasses import dataclass
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
    system: str
    version: int
    user_agent: str
    headers: dict[str, str]
    """Every header to send with a page request, the user agent included."""

    def describe(self) -> str:
        return f"{self.name} {self.version} on {self.system}"

    @classmethod
    def pick(cls, rng: random.Random, today: date) -> "Browser":
        name, system, _ = rng.choices(_CHOICES, weights=[weight for *_, weight in _CHOICES])[0]
        language = rng.choice(_LANGUAGES)
        behind = rng.choices((0, 1, 2), weights=(6, 3, 1))[0]
        if name == "Firefox":
            return _firefox(system, current_version(_FIREFOX_RELEASE, today) - behind, language)
        if name == "Safari":
            return _safari(safari_version(today), rng.choice((0, 1, 2, 3, 4)), language)
        return _chromium(name, system, current_version(_CHROME_RELEASE, today) - behind, language)


def current_version(release: tuple[int, date], today: date) -> int:
    version, released = release
    return version + max(0, (today - released).days) // _DAYS_PER_VERSION


def safari_version(today: date) -> int:
    """Safari 26 came with the systems of September 2025, and a new major version comes every September."""
    return today.year - 1999 - (today.month < _SAFARI_SHIPS_IN_SEPTEMBER)


def _chromium(name: str, system: str, version: int, language: str) -> Browser:
    platform = {"Windows": _WINDOWS, "macOS": _MAC, "Linux": _LINUX}[system]
    user_agent = (f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) "
                  f"Chrome/{version}.0.0.0 Safari/537.36")
    if name == "Edge":
        user_agent += f" Edg/{version}.0.0.0"
    return Browser(name, system, version, user_agent, _headers(user_agent, _CHROME_ACCEPT, language))


def _firefox(system: str, version: int, language: str) -> Browser:
    platform = {"Windows": _WINDOWS, "macOS": _FIREFOX_MAC, "Linux": _LINUX}[system]
    user_agent = f"Mozilla/5.0 ({platform}; rv:{version}.0) Gecko/20100101 Firefox/{version}.0"
    return Browser("Firefox", system, version, user_agent, _headers(user_agent, _HTML_ACCEPT, language))


def _safari(version: int, minor: int, language: str) -> Browser:
    user_agent = (f"Mozilla/5.0 ({_MAC}) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                  f"Version/{version}.{minor} Safari/605.1.15")
    return Browser("Safari", "macOS", version, user_agent, _headers(user_agent, _HTML_ACCEPT, language))


def _headers(user_agent: str, accept: str, language: str) -> dict[str, str]:
    return {
        "User-Agent": user_agent,
        "Accept": accept,
        "Accept-Language": language,
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }

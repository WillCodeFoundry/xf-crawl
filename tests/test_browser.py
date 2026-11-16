"""The browser the crawler passes for."""

import random
import re
from datetime import date

import pytest

from xfcrawl.browser import Browser, current_version, safari_version

TODAY = date(2026, 11, 16)


def picks(count: int, today: date = TODAY) -> list[Browser]:
    rng = random.Random(1)
    return [Browser.pick(rng, today) for _ in range(count)]


def test_the_pick_varies_across_browsers_systems_and_languages():
    browsers = picks(500)
    assert {b.name for b in browsers} == {"Chrome", "Edge", "Firefox", "Safari"}
    assert {b.system for b in browsers} == {"Windows", "macOS", "Linux"}
    assert len({b.headers["Accept-Language"] for b in browsers}) > 3
    assert len({b.user_agent for b in browsers}) > 20


def test_chrome_is_the_most_common_pick():
    names = [b.name for b in picks(1000)]
    assert max(set(names), key=names.count) == "Chrome"


def test_each_user_agent_matches_its_browser():
    for browser in picks(300):
        agent = browser.user_agent
        assert agent.startswith("Mozilla/5.0 (")
        assert browser.headers["User-Agent"] == agent
        assert "xf-crawl" not in agent
        if browser.name in ("Chrome", "Edge"):
            assert f"Chrome/{browser.version}.0.0.0 Safari/537.36" in agent
            assert ("Edg/" in agent) == (browser.name == "Edge")
            assert "image/webp" in browser.headers["Accept"]
        elif browser.name == "Firefox":
            assert agent.endswith(f"rv:{browser.version}.0) Gecko/20100101 Firefox/{browser.version}.0")
            assert "Chrome" not in agent
        else:
            assert re.search(rf"Version/{browser.version}\.\d Safari/605\.1\.15$", agent)
            assert browser.system == "macOS"
        if browser.system == "Windows":
            assert "Windows NT 10.0; Win64; x64" in agent
        if browser.system == "Linux":
            assert "X11; Linux x86_64" in agent


def test_versions_keep_up_with_the_date():
    assert current_version((136, date(2025, 4, 29)), date(2025, 4, 29)) == 136
    assert current_version((136, date(2025, 4, 29)), date(2025, 5, 27)) == 137
    assert current_version((136, date(2025, 4, 29)), date(2026, 11, 16)) == 136 + 566 // 28
    now = max(b.version for b in picks(200) if b.name == "Chrome")
    assert min(b.version for b in picks(200, date(2028, 1, 1)) if b.name == "Chrome") > now


def test_versions_are_at_most_two_behind():
    newest = current_version((136, date(2025, 4, 29)), TODAY)
    assert {b.version for b in picks(500) if b.name == "Chrome"} == {newest, newest - 1, newest - 2}


def test_safari_moves_on_each_september():
    assert safari_version(date(2025, 8, 31)) == 25
    assert safari_version(date(2025, 9, 1)) == 26
    assert safari_version(date(2026, 11, 16)) == 27


def test_a_saved_browser_updates_itself_once_it_falls_behind():
    rng = random.Random(2)
    chrome = Browser("Chrome", "Windows", 140, "en-US,en;q=0.9")
    newest = current_version((136, date(2025, 4, 29)), TODAY)
    assert chrome.updated(rng, TODAY).version >= newest - 2
    current = Browser("Chrome", "Windows", newest - 1, "en-US,en;q=0.9")
    assert current.updated(rng, TODAY) is current
    safari = Browser("Safari", "macOS", 25, "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7", minor=3)
    updated = safari.updated(rng, TODAY)
    assert (updated.version, updated.language) == (27, safari.language)


def test_a_browser_survives_saving_and_bad_ones_are_refused():
    for browser in picks(30):
        assert Browser.from_json(browser.to_json()) == browser
    bad_ones: list[object] = [[], {"name": "Lynx", "system": "Linux", "version": 2, "language": "en"},
                              {"name": "Chrome", "system": "Windows", "version": "140", "language": "en"}]
    for bad in bad_ones:
        with pytest.raises(ValueError, match=r"not an object|unknown browser|missing"):
            Browser.from_json(bad)

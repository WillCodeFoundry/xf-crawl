"""xf tunnelbear, and the account behind it, against the fake TunnelBear."""

import asyncio
import json
import os
import ssl
import threading
from pathlib import Path

import pytest
import requests

from xfcrawl.errors import XfError
from xfcrawl.tunnelbear.account import Credentials, Endpoints, TokenFile, TunnelBearAccount, TunnelBearError
from xfcrawl.tunnelbear.local_proxy import LocalProxy
from xfcrawl.tunnelbear.rotation import ServerRotation

TOKENS = Path(".crawl-store/tunnelbear.json")


def account() -> TunnelBearAccount:
    return TunnelBearAccount(Endpoints.from_environment(os.environ), TokenFile(TOKENS), requests.Session())


def test_login_saves_tokens_never_the_password(tunnelbear, xf):
    result = xf("tunnelbear", "login")
    assert result.code == 0, result.err
    saved = json.loads(TOKENS.read_text())
    assert saved["refresh_token"] == "r1" and saved["email"] == "me@example.com"
    assert "not-a-real-password" not in TOKENS.read_text()
    assert TOKENS.stat().st_mode & 0o077 == 0


def test_tb_state_keeps_the_login_elsewhere(tunnelbear, xf, environment, tmp_path):
    environment.set_env("TB_STATE", str(tmp_path / "shared-login.json"))
    xf("tunnelbear", "login")
    assert (tmp_path / "shared-login.json").exists()
    assert not TOKENS.exists()


def test_logout_forgets_the_login(tunnelbear, xf):
    xf("tunnelbear", "login")
    assert xf("tunnelbear", "logout").code == 0
    assert not TOKENS.exists()


def test_countries(tunnelbear, xf):
    assert xf("tunnelbear", "countries").out.split() == ["FR", "France"]


def test_an_unknown_country_is_refused(tunnelbear):
    tb = account()
    tb.login(Credentials("me@example.com", "x"), mfa_code=None)
    with pytest.raises(TunnelBearError, match="unknown country 'XX'"):
        tb.servers("XX")


def test_fetch_goes_through_tunnelbear(forum, tunnelbear, xf, tmp_path):
    result = xf("tunnelbear", "fetch", forum.url, "-c", "FR", "-o", str(tmp_path / "page.html"))
    assert result.code == 0, result.err
    assert "HTTP 200" in result.err
    assert "Forums" in (tmp_path / "page.html").read_text()
    assert forum.direct_hits() == []


def test_without_a_login_or_credentials_the_message_says_what_to_do(tunnelbear, xf, environment):
    environment.unset_env("TB_USER")
    environment.unset_env("TB_PASS")
    result = xf("tunnelbear", "countries")
    assert result.code == 1
    assert "xf tunnelbear login" in result.err


def test_an_account_with_mfa_cant_log_in_without_a_prompt(tunnelbear):
    tunnelbear.mfa = True
    with pytest.raises(XfError, match="needs an MFA code"):
        account().login(Credentials("me@example.com", "x"), mfa_code=None)


def test_an_mfa_code_is_asked_for_and_sent(tunnelbear):
    tunnelbear.mfa = True
    asked: list[str] = []

    def code(kind: str) -> str:
        asked.append(kind)
        return "123456"

    account().login(Credentials("me@example.com", "x"), mfa_code=code)
    assert asked == ["totp"]
    assert tunnelbear.mfa_codes == ["123456"]


def test_serve_relays_requests_through_tunnelbear(forum, tunnelbear):
    tb = account()
    tb.login(Credentials("me@example.com", "x"), mfa_code=None)
    rotation = ServerRotation(tb, "FR")
    rotation.renew()                                  # the fake's first token is stale on purpose
    relay = LocalProxy(rotation, 0, ssl.create_default_context(cafile=os.environ["REQUESTS_CA_BUNDLE"]))
    ready = threading.Event()
    threading.Thread(target=asyncio.run, args=(relay.serve(ready),), daemon=True).start()
    assert ready.wait(10)
    response = requests.get(forum.url, proxies={"http": f"http://127.0.0.1:{relay.port}"}, timeout=10)
    assert response.status_code == 200
    assert "Forums" in response.text
    assert forum.direct_hits() == []
    assert tunnelbear.forwarded == [forum.url]

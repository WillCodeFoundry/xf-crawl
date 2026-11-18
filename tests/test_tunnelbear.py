"""xf tunnelbear, and the account behind it, against the fake TunnelBear."""

import asyncio
import json
import os
import ssl
import threading
from pathlib import Path

import pytest
import requests

from tests.support.mock_tunnelbear import CREDENTIALS_FILE, EMAIL, PASSWORD, write_credentials
from xfcrawl.errors import XfError
from xfcrawl.tunnelbear.account import (
    Credentials,
    CredentialsFile,
    Endpoints,
    Server,
    TokenFile,
    TunnelBearAccount,
    TunnelBearError,
)
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


def test_every_server_in_every_country_is_listed_once(tunnelbear):
    tb = account()
    tb.login(Credentials("me@example.com", "x"), mfa_code=None)
    assert sorted(tb.all_servers(), key=lambda s: s.host) == [Server("127.0.0.1", "FR"), Server("localhost", "FR")]


def test_fetch_goes_through_tunnelbear(forum, tunnelbear, xf, tmp_path):
    result = xf("tunnelbear", "fetch", forum.url, "-o", str(tmp_path / "page.html"))
    assert result.code == 0, result.err
    assert "HTTP 200" in result.err
    assert "Forums" in (tmp_path / "page.html").read_text()
    assert forum.direct_hits() == []


def test_without_a_login_or_credentials_the_message_says_what_to_do(tunnelbear, xf):
    CREDENTIALS_FILE.unlink()
    result = xf("tunnelbear", "fetch", "http://forum.example/")
    assert result.code == 1
    assert "uv run xf tunnelbear login" in result.err
    assert "tunnelbear-credentials.json" in result.err


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
    rotation = ServerRotation(tb)
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


def expire_the_saved_login(tunnelbear, xf):
    """Log in, then have TunnelBear refuse both the access token and the refresh token it gave."""
    assert xf("tunnelbear", "login").code == 0
    tunnelbear.expired_access, tunnelbear.refused_refresh = {"a1"}, {"r1"}


def test_a_refused_login_is_renewed_from_the_credentials_file(forum, tunnelbear, xf):
    expire_the_saved_login(tunnelbear, xf)
    result = xf("crawl", forum.url, "--tunnelbear")
    assert result.code == 0, result.out
    assert "no longer accepts the saved login" in result.err
    assert "Now using TunnelBear account me@example.com" in result.err
    assert tunnelbear.logins == [EMAIL, EMAIL]
    assert json.loads(TOKENS.read_text())["refresh_token"] == "r2"
    assert PASSWORD not in TOKENS.read_text()
    assert forum.direct_hits() == []


def test_without_credentials_a_refused_login_stops_the_crawl_saying_what_to_do(forum, tunnelbear, xf):
    expire_the_saved_login(tunnelbear, xf)
    CREDENTIALS_FILE.unlink()
    result = xf("crawl", forum.url, "--tunnelbear")
    assert result.code == 1
    assert "no longer accepts the saved login" in result.err
    assert "uv run xf tunnelbear login" in result.err
    assert forum.hits == []


def test_tunnelbear_being_down_is_not_mistaken_for_a_refused_login(tunnelbear):
    refused, down = TunnelBearError("x", 401), TunnelBearError("x", 503)
    assert refused.refused and not down.refused and not TunnelBearError("x").refused


def test_logins_without_a_terminal_use_the_first_account_listed(tunnelbear, xf):
    write_credentials([(f"bear{n}@example.com", "pw") for n in range(5)])
    for _ in range(3):
        assert xf("tunnelbear", "login").code == 0
    assert tunnelbear.logins == ["bear0@example.com"] * 3


def test_the_credentials_file_is_refused_if_others_can_read_it(tunnelbear, xf):
    CREDENTIALS_FILE.chmod(0o644)
    result = xf("tunnelbear", "login")
    assert result.code == 1
    assert "others can read it; run: chmod 600" in result.err
    assert tunnelbear.logins == []


@pytest.mark.parametrize("content", ["{", '{"email": "x"}', '[{"email": "x"}]', '["x"]'])
def test_a_malformed_credentials_file_says_what_it_should_be(tunnelbear, xf, content):
    CREDENTIALS_FILE.write_text(content)
    result = xf("tunnelbear", "login")
    assert result.code == 1
    assert 'should be a list of {"email": ..., "password": ...}' in result.err


def test_accounts_missing_an_email_or_password_are_skipped(tmp_path):
    path = tmp_path / "creds.json"
    path.write_text(json.dumps([{"email": "", "password": "p"}, {"email": "a@b", "password": "p"}]))
    path.chmod(0o600)
    assert CredentialsFile(path).accounts() == [Credentials("a@b", "p")]
    assert CredentialsFile(tmp_path / "missing.json").first() is None


def test_data_left_sums_every_device(tunnelbear, xf):
    tunnelbear.devices = [{"down_bytes_used": 400_000_000, "up_bytes_used": 100_000_000},
                          {"down_bytes_used": 90_000_000, "up_bytes_used": 10_000_000}]
    assert xf("tunnelbear", "login").code == 0
    result = xf("tunnelbear", "data")
    assert result.code == 0, result.err
    assert result.out.strip() == "me@example.com: 1.40 GB of 2.00 GB left (70%)"


def test_an_unlimited_account_never_asks_for_its_use(tunnelbear, xf):
    tunnelbear.plans = [{"platform": "Browser", "maxBandwidth": -1}]
    xf("tunnelbear", "login")
    result = xf("tunnelbear", "data")
    assert result.out.strip() == "me@example.com: unlimited data"
    assert tunnelbear.data_calls == 0


def test_an_account_without_a_browser_plan_says_so(tunnelbear, xf):
    tunnelbear.plans = [{"platform": "Desktop", "maxBandwidth": 2_000_000_000}]
    xf("tunnelbear", "login")
    result = xf("tunnelbear", "data")
    assert result.code == 1
    assert "no Browser plan" in result.err


def test_data_needs_a_saved_login_and_never_picks_an_account(tunnelbear, xf):
    result = xf("tunnelbear", "data")
    assert result.code == 1
    assert "xf tunnelbear login" in result.err
    assert tunnelbear.logins == []


SPARE = "spare@example.com"


def test_a_crawl_tries_the_accounts_in_order_past_those_out_of_data_or_refused(forum, tunnelbear, xf):
    assert xf("tunnelbear", "login").code == 0          # as me@example.com, the only account listed yet
    write_credentials([(EMAIL, PASSWORD), ("wrong@example.com", "pw"), (SPARE, "pw"), ("last@example.com", "pw")])
    tunnelbear.used_up, tunnelbear.wrong_password = {EMAIL}, {"wrong@example.com"}
    result = xf("crawl", forum.url, "--tunnelbear", "--max-fetches", "1")
    assert result.code == 0, result.err
    assert "Now using TunnelBear account spare@example.com" in result.err
    assert tunnelbear.logins == [EMAIL, SPARE]  # wrong@ was refused; last@ never needed
    assert json.loads(TOKENS.read_text())["email"] == SPARE
    assert forum.direct_hits() == []


def test_a_crawl_stops_before_anything_when_no_account_has_data(forum, tunnelbear, xf):
    assert xf("tunnelbear", "login").code == 0
    write_credentials([(EMAIL, PASSWORD), (SPARE, "pw")])
    tunnelbear.used_up = {EMAIL, SPARE}
    result = xf("crawl", forum.url, "--tunnelbear")
    assert result.code == 1
    assert "no TunnelBear account can be used" in result.err
    assert "me@example.com: no data left" in result.err and "spare@example.com: no data left" in result.err
    assert forum.hits == []


def test_running_out_of_data_mid_crawl_moves_to_the_next_account(tunnelbear):
    tb = TunnelBearAccount(Endpoints.from_environment(os.environ), TokenFile(TOKENS), requests.Session(),
                           relogin=lambda: [Credentials(EMAIL, PASSWORD), Credentials(SPARE, "pw")])
    tb.login(Credentials(EMAIL, PASSWORD), mfa_code=None)
    tunnelbear.used_up = {EMAIL}
    assert tb.vpn_token()
    assert tb.email == SPARE


def test_a_login_refused_mid_crawl_moves_to_the_next_account_that_works(tunnelbear):
    tb = TunnelBearAccount(Endpoints.from_environment(os.environ), TokenFile(TOKENS), requests.Session(),
                           relogin=lambda: [Credentials(EMAIL, PASSWORD), Credentials(SPARE, "pw")])
    tb.login(Credentials(EMAIL, PASSWORD), mfa_code=None)
    tunnelbear.expired_access, tunnelbear.refused_refresh = {"a1"}, {"r1"}
    tunnelbear.wrong_password = {EMAIL}  # its password changed, which revoked the login
    assert tb.vpn_token()
    assert tb.email == SPARE

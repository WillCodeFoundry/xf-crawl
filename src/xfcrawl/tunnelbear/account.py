"""TunnelBearAccount: logging in to TunnelBear, and what the account gives: its VPN token and
proxy servers. The only module that talks to TunnelBear's API.

The login is kept in a TokenFile: the email, a device id and the tokens, never the password.
"""

import json
import logging
import os
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from xfcrawl.errors import XfError

_TIMEOUT_SECONDS = 20
# Headers the extension sends on TunnelBear API calls.
_APP_HEADERS = {
    "tunnelbear-app-id": "com.tunnelbear.browser",
    "tunnelbear-app-version": "",
    "tunnelbear-platform": "",
    "tunnelbear-platform-version": "",
}
_EXPIRED_TOKEN_STATUSES = frozenset({400, 401, 424})
_LOGGER = logging.getLogger(__name__)


class TunnelBearError(XfError):
    """TunnelBear refused or failed a request; the message says which, and what it answered."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        """The HTTP status TunnelBear answered with, if it answered."""

    @property
    def refused(self) -> bool:
        """Whether TunnelBear refused the request (a 4xx), rather than failing to answer it."""
        return self.status is not None and 400 <= self.status < 500


class LoginRefused(TunnelBearError):
    """TunnelBear refused the saved login itself: its refresh token expired, or was revoked."""


@dataclass(frozen=True)
class Endpoints:
    dashboard: str = "https://prod-api-dashboard.tunnelbear.com/dashboard/web"
    tunnelbear_api: str = "https://api.tunnelbear.com"
    polarbear_api: str = "https://api.polargrizzly.com"
    proxy_port: int = 8080

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "Endpoints":
        """The real endpoints, unless TB_DASHBOARD_URL, TB_API_URL, TB_PB_API_URL or TB_PROXY_PORT say
        otherwise: those exist to point the code at a stand-in TunnelBear in tests."""
        default = cls()
        return cls(
            dashboard=environment.get("TB_DASHBOARD_URL", default.dashboard),
            tunnelbear_api=environment.get("TB_API_URL", default.tunnelbear_api),
            polarbear_api=environment.get("TB_PB_API_URL", default.polarbear_api),
            proxy_port=int(environment.get("TB_PROXY_PORT", default.proxy_port)),
        )


@dataclass(frozen=True, slots=True)
class Server:
    host: str
    country: str
    """Its country's ISO code, as TunnelBear lists it."""


@dataclass(frozen=True)
class Credentials:
    email: str
    password: str


class CredentialsFile:
    """TunnelBear accounts to log in with when no one is there to type, kept by hand in a JSON file that
    only its owner can read::

        [{"email": "me@example.com", "password": "..."}, {"email": "other@example.com", "password": "..."}]

    Read when needed, never written. Accounts are tried in the order listed.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def accounts(self) -> list[Credentials]:
        """The accounts listed, or none if there is no file; raises TunnelBearError if it can't be used."""
        if not self.path.exists():
            return []
        if os.name == "posix" and self.path.stat().st_mode & 0o077:
            raise TunnelBearError(f"{self.path} holds passwords but others can read it; run: chmod 600 {self.path}")
        try:
            listed = json.loads(self.path.read_text(encoding="utf-8"))
            accounts = [Credentials(str(entry["email"]), str(entry["password"])) for entry in listed]
        except (ValueError, TypeError, KeyError) as error:
            raise TunnelBearError(f"{self.path} should be a list of {{\"email\": ..., \"password\": ...}} "
                                  f"({type(error).__name__}: {error})") from error
        return [account for account in accounts if account.email and account.password]

    def first(self) -> Credentials | None:
        """The first account listed, or None if none are."""
        accounts = self.accounts()
        return accounts[0] if accounts else None


class TokenFile:
    """The saved login, readable by its owner only."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise TunnelBearError(f"{self.path} is not a saved TunnelBear login ({error}); delete it and run "
                                  "`uv run xf tunnelbear login`") from error
        if not isinstance(loaded, dict):
            raise TunnelBearError(f"{self.path} is not a saved TunnelBear login (not a JSON object); delete it "
                                  "and run `uv run xf tunnelbear login`")
        return {str(key): str(value) for key, value in loaded.items()}

    def save(self, state: Mapping[str, str]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        partial = self.path.with_name(f".{self.path.name}.partial")
        descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(dict(state), file)
        os.replace(partial, self.path)

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)


class TunnelBearAccount:
    def __init__(
        self,
        endpoints: Endpoints,
        tokens: TokenFile,
        http: requests.Session,
        relogin: Callable[[], list[Credentials]] = list,
    ) -> None:
        """``http`` talks to TunnelBear's API directly, as the extension does; it is not the proxy.
        ``relogin`` gives the accounts to try, in order, when the one logged in can't be used (TunnelBear
        refuses its login, or it has no data left); it must not ask anyone, as it is called mid-crawl."""
        self.endpoints = endpoints
        self._relogin = relogin
        self._tokens = tokens
        self._http = http
        self._state = tokens.load()
        self._state.setdefault("device", str(uuid.uuid4()))  # the extension's random uuid4, kept for good
        # tb-proxy serve renews the token from a worker thread. Reentrant: a PolarBear call can find the login
        # refused, and logging in to another account checks its data with PolarBear calls of its own.
        self._lock = threading.RLock()
        self._switching = False
        self._fresh = False  # whether this run got the access token itself, so it can't be stale yet

    def is_logged_in(self) -> bool:
        return bool(self._state.get("refresh_token"))

    @property
    def email(self) -> str:
        """The email of the saved login, or "" if none."""
        return self._state.get("email", "")

    def login(self, credentials: Credentials, mfa_code: Callable[[str], str] | None) -> None:
        """Log in and save the tokens. ``mfa_code`` is asked for a code when the account uses MFA; with
        None, such an account can't log in."""
        self._state.pop("access_token", None)
        body = self._post(self.endpoints.dashboard, "/v2/token", {
            "username": credentials.email, "password": credentials.password,
            "grant_type": "password", "device": self._state["device"],
        })
        if body.get("mfa_type"):
            if mfa_code is None:
                raise TunnelBearError(f"this account needs an MFA code ({body['mfa_type']}); log in with "
                                      "`xf tunnelbear login` in a terminal")
            body = self._post(self.endpoints.tunnelbear_api, "/v2/mfa", {
                "mfa_token": mfa_code(body["mfa_type"]), "grant_type": "mfa_login",
                "device": self._state["device"], "username": credentials.email,
                "login_token": body["login_token"],
            })
        self._state["email"] = credentials.email
        self._store_tokens(body)

    def logout(self) -> None:
        """Forget the saved login here. TunnelBear itself isn't told; changing the password revokes it."""
        self._tokens.delete()
        self._state = {"device": str(uuid.uuid4())}

    def vpn_token(self) -> str:
        """The proxy's credential. When TunnelBear gives none (the account is out of data, or not confirmed),
        moves to another account, as ``relogin`` lists them, that has data left."""
        with self._lock:
            token = self._polarbear_get("/user").get("vpn_token")
            if not token:
                self._switch_account([f"{self.email}: TunnelBear gave no VPN token (out of data, or not confirmed?)"],
                                     skip=self.email)
                token = self._polarbear_get("/user").get("vpn_token")
        if not token:
            raise TunnelBearError(f"TunnelBear gave {self.email} no VPN token, though it has data left")
        return str(token)

    def use_account_with_data(self) -> None:
        """Stay logged in as this login if it has data left, else log in as the first account ``relogin``
        lists that has some. Raises TunnelBearError, saying why each failed, if none can be used."""
        with self._lock:
            if not self.is_logged_in():
                self._switch_account([], skip=None)
                return
            try:
                problem = self.data_problem()
            except LoginRefused as refusal:
                self._log_in_again(refusal)
                return
            if problem is not None:
                self._switch_account([f"{self.email}: {problem}"], skip=self.email)

    def data_problem(self) -> str | None:
        """Why this login can't be used, or None if it has data left: under 0.5% counts as none, as in
        TunnelBear's extension, which turns the proxy off then. Raises LoginRefused if TunnelBear refuses
        the login itself."""
        try:
            allowance, used = self.data_allowance()
        except LoginRefused:
            raise
        except TunnelBearError as error:
            return str(error)
        _LOGGER.info("TunnelBear %s", describe_allowance(self.email, allowance, used))
        if allowance != -1 and (allowance - used) * 200 < allowance:
            return f"no data left ({(allowance - used) / 1e9:.2f} GB of {allowance / 1e9:.2f} GB)"
        return None

    def data_allowance(self) -> tuple[int, int]:
        """The account's data allowance and the bytes used of it, both in bytes, as the extension works
        them out: the Browser plan's maxBandwidth (-1 for unlimited), less what every device has sent and
        received."""
        if not self._fresh:
            self._refresh(may_log_in_again=False)  # so /v2/account can't refuse a stale access token
        response = self._http.get(
            self.endpoints.tunnelbear_api + "/v2/account",
            headers={**_APP_HEADERS, "authorization": "Bearer " + self._state["access_token"]},
            timeout=_TIMEOUT_SECONDS,
        )
        _raise_for(response)
        plans = response.json().get("plans") or []
        plan = next((plan for plan in plans if plan.get("platform") == "Browser"), None)
        if plan is None:
            raise TunnelBearError("TunnelBear's account lists no Browser plan, so no data allowance")
        allowance = int(plan["maxBandwidth"])
        if allowance == -1:
            return -1, 0
        devices = self._polarbear_get("/data").get("devices") or []
        return allowance, sum(int(device["down_bytes_used"]) + int(device["up_bytes_used"]) for device in devices)

    def countries(self) -> list[dict[str, Any]]:
        countries: list[dict[str, Any]] = self._polarbear_get("/countries")
        return countries

    def all_servers(self) -> list[Server]:
        """Every proxy server TunnelBear lists, in every country; a server listed twice counts once."""
        servers: dict[str, Server] = {}
        for country in self.countries():
            iso = country.get("iso")
            if not iso:
                continue
            data = self._polarbear_get(f"/vpns/countries/{iso}")
            for vpn in data.get("vpns", []):
                if vpn.get("url"):
                    servers.setdefault(vpn["url"], Server(vpn["url"], str(iso).upper()))
        if not servers:
            raise TunnelBearError("TunnelBear listed no proxy servers")
        return list(servers.values())

    # -- internals ----------------------------------------------------------------------------------
    def _post(self, base: str, path: str, payload: Mapping[str, str]) -> dict[str, Any]:
        headers = {**_APP_HEADERS, "Content-Type": "application/json"}
        if self._state.get("access_token"):
            headers["authorization"] = "Bearer " + self._state["access_token"]
        response = self._http.post(base + path, data=json.dumps(payload), headers=headers, timeout=_TIMEOUT_SECONDS)
        _raise_for(response)
        body: dict[str, Any] = response.json()
        return body

    def _store_tokens(self, body: Mapping[str, Any]) -> None:
        if not body.get("access_token") or not body.get("refresh_token"):
            raise TunnelBearError("TunnelBear's login answer had no access or refresh token")
        self._state.update(access_token=body["access_token"], refresh_token=body["refresh_token"])
        self._state.pop("polarbear_token", None)
        self._tokens.save(self._state)
        self._fresh = True

    def _refresh(self, *, may_log_in_again: bool = True) -> None:
        if not self.is_logged_in():
            raise TunnelBearError("not logged in to TunnelBear; run `xf tunnelbear login`")
        try:
            body = self._post(self.endpoints.dashboard, "/v2/token", {
                "username": self._state.get("email", ""), "refresh_token": self._state["refresh_token"],
                "grant_type": "refresh_token", "device": self._state["device"],
            })
        except TunnelBearError as error:
            if not error.refused:
                raise
            if not may_log_in_again:
                raise LoginRefused(str(error), error.status) from error
            self._log_in_again(error)
            return
        self._store_tokens(body)

    def _log_in_again(self, refusal: TunnelBearError) -> None:
        """TunnelBear refused the saved login (it expired, or was revoked): move to an account ``relogin``
        lists that logs in and has data left: it may be this one again, with its password."""
        if self._switching:  # already moving between accounts: this one is simply refused
            raise LoginRefused(str(refusal), refusal.status) from refusal
        _LOGGER.info("TunnelBear no longer accepts the saved login for %s; trying the other accounts", self.email)
        self._switch_account([f"{self.email}: TunnelBear no longer accepts the saved login ({refusal})"], skip=None)

    def _switch_account(self, problems: list[str], skip: str | None) -> None:
        """Log in as the first account ``relogin`` lists, other than ``skip``, that logs in and has data left.
        Raises TunnelBearError listing ``problems`` and why each account failed, if none does."""
        tried = {skip}
        self._switching = True
        try:
            for credentials in self._relogin():
                if credentials.email not in tried:
                    tried.add(credentials.email)
                    if self._usable_as(credentials, problems):
                        return
        finally:
            self._switching = False
        raise TunnelBearError("no TunnelBear account can be used:\n  " + "\n  ".join(problems) +
                              "\nlog in with `uv run xf tunnelbear login`, or list accounts in "
                              "tunnelbear-credentials.json to have it done for you")

    def _usable_as(self, credentials: Credentials, problems: list[str]) -> bool:
        """Whether logging in as ``credentials`` gave an account with data left; if not, adds why to
        ``problems``."""
        try:
            problem = self._login_problem(credentials) or self.data_problem()
        except LoginRefused as refusal:
            problem = str(refusal)
        if problem is None:
            _LOGGER.info("Now using TunnelBear account %s", credentials.email)
            return True
        problems.append(f"{credentials.email}: {problem}")
        _LOGGER.info("TunnelBear account %s can't be used (%s)", credentials.email, problem)
        return False

    def _login_problem(self, credentials: Credentials) -> str | None:
        try:
            self.login(credentials, mfa_code=None)
        except TunnelBearError as error:
            return f"login refused: {error}"
        return None

    def _polarbear_auth(self, *, may_refresh: bool) -> None:
        if not self._state.get("access_token"):
            raise TunnelBearError("not logged in to TunnelBear; run `xf tunnelbear login`")
        response = self._http.post(
            self.endpoints.polarbear_api + "/auth",
            data=json.dumps({"partner": "tunnelbear", "token": self._state["access_token"]}),
            headers={"Content-Type": "application/json"},
            timeout=_TIMEOUT_SECONDS,
        )
        if response.status_code in _EXPIRED_TOKEN_STATUSES and may_refresh:
            self._refresh()
            self._polarbear_auth(may_refresh=False)
            return
        _raise_for(response)
        token = response.headers.get("authorization", "")
        if not token:
            raise TunnelBearError("PolarBear's login answer had no Authorization header")
        self._state["polarbear_token"] = token.removeprefix("Bearer ")
        self._tokens.save(self._state)

    def _polarbear_get(self, path: str) -> Any:
        with self._lock:
            for attempt in range(2):
                if not self._state.get("polarbear_token"):
                    self._polarbear_auth(may_refresh=True)
                response = self._http.get(
                    self.endpoints.polarbear_api + path,
                    headers={"authorization": "Bearer " + self._state["polarbear_token"]},
                    timeout=_TIMEOUT_SECONDS,
                )
                if response.status_code == 401 and attempt == 0:
                    self._state.pop("polarbear_token", None)
                    continue
                _raise_for(response)
                return response.json()
        raise TunnelBearError(f"PolarBear kept refusing {path} after a fresh login")


def describe_allowance(email: str, allowance: int, used: int) -> str:
    """One line on the data left, in GB (10^9 bytes); negative when the account has gone over."""
    if allowance == -1:
        return f"{email}: unlimited data"
    left = allowance - used
    percent = left / allowance * 100 if allowance else 0.0
    return f"{email}: {left / 1e9:.2f} GB of {allowance / 1e9:.2f} GB left ({percent:.0f}%)"


def _raise_for(response: requests.Response) -> None:
    if response.ok:
        return
    try:
        body = response.json()
        message = body.get("error_message") or body.get("message") or json.dumps(body)[:200]
    except ValueError:
        message = response.text[:200]
    raise TunnelBearError(f"{response.request.method} {response.url} -> HTTP {response.status_code}: {message}",
                          response.status_code)

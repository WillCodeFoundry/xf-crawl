"""TunnelBearAccount: logging in to TunnelBear, and what the account gives: its VPN token, countries and
proxy servers. The only module that talks to TunnelBear's API.

The login is kept in a TokenFile: the email, a device id and the tokens, never the password.
"""

import json
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


class TunnelBearError(XfError):
    """TunnelBear refused or failed a request; the message says which, and what it answered."""


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


@dataclass(frozen=True)
class Credentials:
    email: str
    password: str


class TokenFile:
    """The saved login, readable by its owner only."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        loaded: dict[str, str] = json.loads(self.path.read_text(encoding="utf-8"))
        return loaded

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
    def __init__(self, endpoints: Endpoints, tokens: TokenFile, http: requests.Session) -> None:
        """``http`` talks to TunnelBear's API directly, as the extension does; it is not the proxy."""
        self.endpoints = endpoints
        self._tokens = tokens
        self._http = http
        self._state = tokens.load()
        self._state.setdefault("device", str(uuid.uuid4()))  # the extension's random uuid4, kept for good
        self._lock = threading.Lock()  # tb-proxy serve renews the token from a worker thread

    def is_logged_in(self) -> bool:
        return bool(self._state.get("refresh_token"))

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
        token = self._polarbear_get("/user").get("vpn_token")
        if not token:
            raise TunnelBearError("TunnelBear gave no VPN token (account out of data, or not confirmed?)")
        return str(token)

    def countries(self) -> list[dict[str, Any]]:
        countries: list[dict[str, Any]] = self._polarbear_get("/countries")
        return countries

    def servers(self, country: str | None) -> list[str]:
        """Proxy server hostnames for an ISO country code, or for the closest region when None."""
        if country is None:
            data = self._polarbear_get("/vpns")
            if not data.get("vpns") and data.get("country_iso"):
                data = self._polarbear_get(f"/vpns/countries/{data['country_iso']}")
        else:
            known = {c.get("iso", "").upper(): c.get("iso") for c in self.countries()}
            if country.upper() not in known:
                raise TunnelBearError(f"unknown country {country!r}; see `xf tunnelbear countries`")
            data = self._polarbear_get(f"/vpns/countries/{known[country.upper()]}")
        hosts = [vpn["url"] for vpn in data.get("vpns", []) if vpn.get("url")]
        if not hosts:
            raise TunnelBearError("TunnelBear listed no proxy servers")
        return hosts

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

    def _refresh(self) -> None:
        if not self.is_logged_in():
            raise TunnelBearError("not logged in to TunnelBear; run `xf tunnelbear login`")
        self._store_tokens(self._post(self.endpoints.dashboard, "/v2/token", {
            "username": self._state.get("email", ""), "refresh_token": self._state["refresh_token"],
            "grant_type": "refresh_token", "device": self._state["device"],
        }))

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


def _raise_for(response: requests.Response) -> None:
    if response.ok:
        return
    try:
        body = response.json()
        message = body.get("error_message") or body.get("message") or json.dumps(body)[:200]
    except ValueError:
        message = response.text[:200]
    raise TunnelBearError(f"{response.request.method} {response.url} -> HTTP {response.status_code}: {message}")

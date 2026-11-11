#!/usr/bin/env python3
"""
tb-proxy - use your TunnelBear account from Python the way the TunnelBear
browser extension does: an authenticated HTTPS proxy. No OpenVPN, no root,
no system-wide changes. Only what you send through it is tunnelled.

What the extension (v4.5.0) does, and what this script reproduces
------------------------------------------------------------------
1. Log in to TunnelBear:
     POST https://prod-api-dashboard.tunnelbear.com/dashboard/web/v2/token
     {"username", "password", "grant_type": "password", "device": <uuid4>}
     -> {"access_token", "refresh_token"}  (or {"mfa_type", "login_token"})
2. Exchange that for a "PolarBear" token (the proxy back end):
     POST https://api.polargrizzly.com/auth {"partner": "tunnelbear", "token": <access_token>}
     -> token in the response's Authorization header
3. GET https://api.polargrizzly.com/user           -> {"vpn_token", ...}
   GET https://api.polargrizzly.com/countries      -> [{"iso", "name", ...}]
   GET https://api.polargrizzly.com/vpns/countries/<ISO> -> {"vpns": [{"url": host}, ...]}
4. Use https://<host>:8080 as an HTTPS proxy (TLS to the proxy itself), with
   Proxy-Authorization Basic <vpn_token>:<vpn_token>.

Usage
-----
  tb-proxy login                 # prompts; tokens saved, not your password
  tb-proxy countries
  tb-proxy fetch https://ifconfig.me -c FR
  tb-proxy fetch https://example.com -c JP -o page.html
  tb-proxy serve -c FR --port 8118
      # then any program, per process:
      curl -x http://127.0.0.1:8118 https://ifconfig.me
      HTTPS_PROXY=http://127.0.0.1:8118 some-tool
  tb-proxy logout

In your own code:
  from xfcrawl.tb_proxy import TunnelBear
  tb = TunnelBear(); tb.ensure_login()
  r = tb.get("https://example.com", country="FR")

Notes
-----
* This is TunnelBear's private API, not a published one. It can change with
  any extension update, and using it may conflict with TunnelBear's terms.
* Requests count against your account's data allowance like the extension's.
* State (email, device id + tokens, never the password) is stored in
  .crawl-store/tunnelbear.json in the current directory, with 0600
  permissions. TB_STATE=/path.json overrides this.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import json
import os
import ssl
import sys
import threading
import time
import urllib.parse
import uuid

import requests

# Endpoints taken from the extension. Overridable through the environment
# only so the script can be tested against a local mock.
DASHBOARD_URL = os.environ.get("TB_DASHBOARD_URL", "https://prod-api-dashboard.tunnelbear.com/dashboard/web")
TB_API_URL = os.environ.get("TB_API_URL", "https://api.tunnelbear.com")
PB_API_URL = os.environ.get("TB_PB_API_URL", "https://api.polargrizzly.com")
PROXY_PORT = int(os.environ.get("TB_PROXY_PORT", "8080"))

# Headers the extension sends on TunnelBear API calls.
TB_HEADERS = {
    "tunnelbear-app-id": "com.tunnelbear.browser",
    "tunnelbear-app-version": "",
    "tunnelbear-platform": "",
    "tunnelbear-platform-version": "",
}

STATE_PATH = os.path.expanduser(os.environ.get("TB_STATE", os.path.join(".crawl-store", "tunnelbear.json")))
TIMEOUT = 20


class TBError(RuntimeError):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


def _err(resp) -> TBError:
    try:
        body = resp.json()
        msg = body.get("error_message") or body.get("message") or json.dumps(body)[:200]
    except ValueError:
        msg = resp.text[:200]
    return TBError(f"{resp.request.method} {resp.url} -> HTTP {resp.status_code}: {msg}", resp.status_code)


class VerifiedProxyAdapter(requests.adapters.HTTPAdapter):
    """requests only verifies an HTTPS proxy's certificate when the *target* URL
    is https; for http:// targets the proxy's TLS (which carries the VPN token)
    would go unverified. This adapter always verifies it."""

    def proxy_manager_for(self, proxy, **kw):
        if proxy.lower().startswith("https://"):
            ca = os.environ.get("REQUESTS_CA_BUNDLE") or requests.utils.DEFAULT_CA_BUNDLE_PATH
            kw.setdefault("cert_reqs", "CERT_REQUIRED")
            kw.setdefault("ca_certs", ca)
            kw.setdefault("proxy_ssl_context", ssl.create_default_context(cafile=ca))
        return super().proxy_manager_for(proxy, **kw)


def proxied_session() -> requests.Session:
    """A requests.Session that verifies HTTPS proxies (use it for TunnelBear proxies)."""
    s = requests.Session()
    s.mount("http://", VerifiedProxyAdapter())
    s.mount("https://", VerifiedProxyAdapter())
    return s


class TunnelBear:
    def __init__(self, state_path: str = STATE_PATH):
        self.state_path = state_path
        self.state = self._load()
        self.state.setdefault("device", str(uuid.uuid4()))   # extension: random uuid4, kept forever
        self.http = requests.Session()                       # direct (not proxied) API session
        self._lock = threading.Lock()

    # ---- persistent state ---------------------------------------------------
    def _load(self) -> dict:
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self):
        os.makedirs(os.path.dirname(self.state_path), mode=0o700, exist_ok=True)
        tmp = self.state_path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.state_path)

    def logout(self):
        try:
            os.remove(self.state_path)
        except FileNotFoundError:
            pass
        self.state = {"device": str(uuid.uuid4())}

    # ---- TunnelBear account API -------------------------------------------------
    def _tb_post(self, base: str, path: str, payload: dict) -> dict:
        headers = dict(TB_HEADERS, **{"Content-Type": "application/json"})
        if self.state.get("access_token"):
            headers["authorization"] = "Bearer " + self.state["access_token"]
        r = self.http.post(base + path, data=json.dumps(payload), headers=headers, timeout=TIMEOUT)
        if not r.ok:
            raise _err(r)
        return r.json()

    def _store_tokens(self, body: dict):
        if not body.get("access_token") or not body.get("refresh_token"):
            raise TBError("login response had no access/refresh token")
        self.state.update(access_token=body["access_token"], refresh_token=body["refresh_token"])
        self.state.pop("pb_token", None)
        self._save()

    def login(self, email: str, password: str, mfa_prompt=None):
        self.state.pop("access_token", None)
        body = self._tb_post(DASHBOARD_URL, "/v2/token", {
            "username": email, "password": password,
            "grant_type": "password", "device": self.state["device"],
        })
        if body.get("mfa_type"):
            if mfa_prompt is None:
                raise TBError(f"account requires MFA ({body['mfa_type']}) but no prompt available")
            code = mfa_prompt(body["mfa_type"])
            body = self._tb_post(TB_API_URL, "/v2/mfa", {
                "mfa_token": code, "grant_type": "mfa_login", "device": self.state["device"],
                "username": email, "login_token": body["login_token"],
            })
        self.state["email"] = email
        self._store_tokens(body)

    def refresh(self):
        if not self.state.get("refresh_token"):
            raise TBError("not logged in - run: tb-proxy login")
        body = self._tb_post(DASHBOARD_URL, "/v2/token", {
            "username": self.state.get("email"), "refresh_token": self.state["refresh_token"],
            "grant_type": "refresh_token", "device": self.state["device"],
        })
        self._store_tokens(body)

    def ensure_login(self, email=None, password=None, interactive=True):
        if self.state.get("refresh_token"):
            return
        email = email or os.environ.get("TB_USER") or (interactive and input("TunnelBear email: "))
        password = password or os.environ.get("TB_PASS") or (interactive and getpass.getpass("TunnelBear password: "))
        if not email or not password:
            raise TBError("not logged in and no credentials given")
        mfa_prompt = (lambda kind: input(f"MFA code ({kind}): ").strip()) if interactive else None
        self.login(email, password, mfa_prompt=mfa_prompt)

    # ---- PolarBear (proxy back end) API -------------------------------------
    def _pb_auth(self, allow_refresh=True):
        if not self.state.get("access_token"):
            raise TBError("not logged in - run: tb-proxy login")
        r = self.http.post(PB_API_URL + "/auth",
                           data=json.dumps({"partner": "tunnelbear", "token": self.state["access_token"]}),
                           headers={"Content-Type": "application/json"}, timeout=TIMEOUT)
        if r.status_code in (400, 401, 424) and allow_refresh:   # TB access token expired
            self.refresh()
            return self._pb_auth(allow_refresh=False)
        if not r.ok:
            raise _err(r)
        tok = r.headers.get("authorization", "")
        if not tok:
            raise TBError("PolarBear auth returned no Authorization header")
        self.state["pb_token"] = tok.replace("Bearer ", "")
        self._save()

    def _pb_get(self, path: str):
        with self._lock:
            for attempt in range(2):
                if not self.state.get("pb_token"):
                    self._pb_auth()
                r = self.http.get(PB_API_URL + path, timeout=TIMEOUT,
                                  headers={"authorization": "Bearer " + self.state["pb_token"]})
                if r.status_code == 401 and attempt == 0:
                    self.state.pop("pb_token", None)
                    continue
                if not r.ok:
                    raise _err(r)
                return r.json()

    def user(self) -> dict:
        return self._pb_get("/user")

    def vpn_token(self) -> str:
        tok = self.user().get("vpn_token")
        if not tok:
            raise TBError("no vpn_token in /user response (account out of data or unconfirmed?)")
        return tok

    def countries(self) -> list:
        return self._pb_get("/countries")

    def servers(self, country: str | None = None) -> list[str]:
        """Proxy hostnames for a country ISO code, or the closest region if None."""
        if country:
            iso = country.upper()
            known = {c.get("iso", "").upper(): c.get("iso") for c in self.countries()}
            if iso not in known:
                raise TBError(f"unknown country {country!r}; see: tb-proxy countries")
            data = self._pb_get(f"/vpns/countries/{known[iso]}")
        else:
            data = self._pb_get("/vpns")
            if not data.get("vpns") and data.get("country_iso"):
                data = self._pb_get(f"/vpns/countries/{data['country_iso']}")
        hosts = [v["url"] for v in data.get("vpns", []) if v.get("url")]
        if not hosts:
            raise TBError("no proxy servers returned")
        return hosts

    # ---- using the proxy ----------------------------------------------------
    @staticmethod
    def proxy_url(host: str, token: str) -> str:
        q = urllib.parse.quote(token, safe="")
        return f"https://{q}:{q}@{host}:{PROXY_PORT}"

    def get(self, url: str, country: str | None = None, **kw) -> requests.Response:
        """requests.get() through TunnelBear, trying each server until one works."""
        token, hosts = self.vpn_token(), self.servers(country)
        last = None
        kw.setdefault("timeout", 60)
        session = proxied_session()
        for host in hosts:
            p = self.proxy_url(host, token)
            try:
                return session.get(url, proxies={"http": p, "https": p}, **kw)
            except (requests.exceptions.ProxyError, requests.exceptions.SSLError,
                    requests.exceptions.ConnectTimeout) as e:
                last = e
        raise TBError(f"all {len(hosts)} TunnelBear servers failed; last error: {last}")


class ProxyRotator:
    """Proxy settings for long-running `requests` use (e.g. a crawler).

    Keeps one TunnelBear server in use, refreshes the VPN token every
    `refresh_every` seconds, and moves to the next server when told the
    current one failed. Not thread-safe; one per crawler.
    """

    def __init__(self, tb: TunnelBear, country: str | None = None, refresh_every: int = 300):
        self.tb, self.country, self.refresh_every = tb, country, refresh_every
        self.hosts: list[str] = []
        self.token = ""
        self.idx = 0
        self.fetched_at = 0.0
        self.refresh()

    def refresh(self):
        """Fetch a fresh VPN token and server list (keeps the current server if still listed)."""
        current = self.host if self.hosts else None
        self.token = self.tb.vpn_token()
        self.hosts = self.tb.servers(self.country)
        self.idx = self.hosts.index(current) if current in self.hosts else 0
        self.fetched_at = time.monotonic()

    @property
    def host(self) -> str:
        return self.hosts[self.idx]

    def proxies(self) -> dict:
        if time.monotonic() - self.fetched_at > self.refresh_every:
            self.refresh()
        p = TunnelBear.proxy_url(self.host, self.token)
        return {"http": p, "https": p}

    def failed(self, auth_problem: bool = False):
        """The current server failed: refresh the token if it was an auth error, then rotate."""
        if auth_problem:
            self.refresh()
        self.idx = (self.idx + 1) % len(self.hosts)

    def describe(self) -> str:
        where = self.country.upper() if self.country else "closest region"
        return f"TunnelBear {where}, {len(self.hosts)} servers, using {self.host}"


# ----------------------------------------------------------------------------
# Local forwarding proxy: plain HTTP proxy on 127.0.0.1 that relays to the
# TunnelBear HTTPS proxy and adds the credentials, so any program can use it.
# ----------------------------------------------------------------------------

class LocalProxy:
    def __init__(self, tb: TunnelBear, country: str | None, port: int,
                 upstream_ssl: ssl.SSLContext | None = None, refresh_every: int = 300):
        self.tb, self.country, self.port = tb, country, port
        self.ssl = upstream_ssl or ssl.create_default_context()
        self.refresh_every = refresh_every
        self.hosts: list[str] = []
        self.auth = b""
        self.idx = 0

    def _refresh_creds(self):
        token = self.tb.vpn_token()
        self.hosts = self.tb.servers(self.country)
        self.auth = b"Basic " + base64.b64encode(f"{token}:{token}".encode())

    async def _refresher(self):
        while True:
            await asyncio.sleep(self.refresh_every)
            try:
                await asyncio.get_running_loop().run_in_executor(None, self._refresh_creds)
            except Exception as e:   # keep serving with the old token
                print(f"warning: credential refresh failed: {e}", file=sys.stderr)

    async def _open_upstream(self):
        last = None
        for i in range(len(self.hosts)):
            host = self.hosts[(self.idx + i) % len(self.hosts)]
            try:
                r, w = await asyncio.wait_for(
                    asyncio.open_connection(host, PROXY_PORT, ssl=self.ssl, server_hostname=host), 15)
                self.idx = (self.idx + i) % len(self.hosts)
                return r, w
            except (OSError, asyncio.TimeoutError, ssl.SSLError) as e:
                last = e
        raise ConnectionError(f"no TunnelBear server reachable ({last})")

    @staticmethod
    async def _pipe(src, dst):
        try:
            while data := await src.read(65536):
                dst.write(data)
                await dst.drain()
        except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
            pass
        finally:
            try:
                dst.close()
            except Exception:
                pass

    async def _handle(self, creader, cwriter):
        try:
            head = await asyncio.wait_for(creader.readuntil(b"\r\n\r\n"), 30)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError):
            cwriter.close()
            return
        lines = head[:-4].split(b"\r\n")
        is_connect = lines[0].upper().startswith(b"CONNECT ")
        drop = (b"proxy-authorization", b"proxy-connection") + (() if is_connect else (b"connection",))
        out = [lines[0]] + [h for h in lines[1:] if h.split(b":", 1)[0].strip().lower() not in drop]
        out.append(b"Proxy-Authorization: " + self.auth)
        if not is_connect:
            out.append(b"Connection: close")   # one request per upstream connection: auth on each
        try:
            ureader, uwriter = await self._open_upstream()
        except ConnectionError as e:
            cwriter.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n"
                          + str(e).encode() + b"\n")
            await cwriter.drain()
            cwriter.close()
            return
        uwriter.write(b"\r\n".join(out) + b"\r\n\r\n")
        await uwriter.drain()
        await asyncio.gather(self._pipe(creader, uwriter), self._pipe(ureader, cwriter))

    async def serve(self, ready: threading.Event | None = None):
        await asyncio.get_running_loop().run_in_executor(None, self._refresh_creds)
        server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        self.port = server.sockets[0].getsockname()[1]
        if ready:
            ready.set()
        print(f"TunnelBear proxy on http://127.0.0.1:{self.port}  ({len(self.hosts)} servers"
              f"{', ' + self.country.upper() if self.country else ', closest region'})", file=sys.stderr)
        asyncio.create_task(self._refresher())
        async with server:
            await server.serve_forever()


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="TunnelBear as a per-process HTTPS proxy (no OpenVPN).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="log in and save tokens")
    sub.add_parser("logout", help="forget saved tokens")
    sub.add_parser("countries", help="list available countries")
    f = sub.add_parser("fetch", help="fetch a URL through TunnelBear")
    f.add_argument("url")
    f.add_argument("-c", "--country", help="ISO code, e.g. FR (default: closest)")
    f.add_argument("-o", "--output", help="write body to file")
    s = sub.add_parser("serve", help="run a local HTTP proxy that relays through TunnelBear")
    s.add_argument("-c", "--country")
    s.add_argument("-p", "--port", type=int, default=8118)
    a = ap.parse_args(argv)

    tb = TunnelBear()
    if a.cmd == "logout":
        tb.logout()
        print("Logged out (local tokens deleted).")
        return
    if a.cmd == "login":
        tb.logout()
        tb.ensure_login()
        print("Logged in. Tokens saved to", tb.state_path)
        return

    tb.ensure_login()
    if a.cmd == "countries":
        for c in sorted(tb.countries(), key=lambda c: c.get("name", "")):
            print(f"{c.get('iso', '?'):4} {c.get('name', '')}")
    elif a.cmd == "fetch":
        r = tb.get(a.url, country=a.country)
        print(f"HTTP {r.status_code}, {len(r.content)} bytes", file=sys.stderr)
        if a.output:
            with open(a.output, "wb") as fh:
                fh.write(r.content)
            print("Saved to", a.output, file=sys.stderr)
        else:
            sys.stdout.buffer.write(r.content)
    elif a.cmd == "serve":
        try:
            asyncio.run(LocalProxy(tb, a.country, a.port).serve())
        except KeyboardInterrupt:
            pass


def cli():
    try:
        main()
    except (TBError, requests.RequestException) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    cli()

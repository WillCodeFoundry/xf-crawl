"""A fake TunnelBear for tests: the account/token API and an HTTPS proxy.

* API (plain HTTP): login, PolarBear auth, /user, /countries, /vpns/...
  The first /user call hands out a stale token ("tok1") that the proxy
  rejects with 407; later calls hand out "tok2". So every test also
  exercises the token refresh.
* The server list is ["127.0.0.1", "localhost"]. Both reach the fake proxy,
  but its certificate is only valid for "localhost", so the first one always
  fails certificate checking: every test also exercises switching servers,
  and the proxy's hostname being verified. (No DNS involved, so it behaves
  the same on every machine.)
* Proxy (TLS, certificate from a throwaway CA made with trustme): accepts
  Proxy-Authorization "tok2:tok2", forwards plain-HTTP requests and marks
  them with an X-Test-Proxy: 1 header so the forum can tell proxied
  requests from direct ones.
"""

import base64
import http.client
import http.server
import json
import ssl
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import trustme

from tests.support import ProcessEnvironment

GOOD_AUTH = "Basic " + base64.b64encode(b"tok2:tok2").decode()
ApiReply = tuple[int, Any, dict[str, str]]


class MockTunnelBear:
    def __init__(self, tmp_path: Path) -> None:
        self.user_calls = 0
        self.user_calls_allowed: int | None = None  # after this many /user calls the API answers 500
        self.mfa = False                            # the account asks for an MFA code at login
        self.mfa_codes: list[str] = []              # the codes it was sent
        self.forwarded: list[str] = []              # URLs the proxy forwarded
        self.rejected = 0             # 407s
        self.ca = trustme.CA()
        self.ca_file = tmp_path / "test-ca.pem"
        self.ca.cert_pem.write_to_path(str(self.ca_file))

    # ---------------------------------------------------------------- API
    def api_post(self, path: str, body: dict[str, Any]) -> ApiReply:
        if path.endswith("/v2/token"):
            if self.mfa and body.get("grant_type") == "password":
                return 200, {"mfa_type": "totp", "login_token": "lt1"}, {}
            return 200, {"access_token": "a1", "refresh_token": "r1"}, {}
        if path == "/v2/mfa":
            self.mfa_codes.append(body["mfa_token"])
            return 200, {"access_token": "a1", "refresh_token": "r1"}, {}
        if path == "/auth":
            return 200, {}, {"Authorization": "Bearer pb1"}
        return 404, {"message": "not found"}, {}

    def api_get(self, path: str) -> ApiReply:
        if path == "/user":
            self.user_calls += 1
            if self.user_calls_allowed is not None and self.user_calls > self.user_calls_allowed:
                return 500, {"message": "down"}, {}
            return 200, {"vpn_token": "tok1" if self.user_calls == 1 else "tok2"}, {}
        if path == "/countries":
            return 200, [{"iso": "FR", "name": "France"}], {}
        if path in ("/vpns", "/vpns/countries/FR"):
            return 200, {"vpns": [{"url": "127.0.0.1"}, {"url": "localhost"}]}, {}
        return 404, {"message": "not found"}, {}

    def _start_api(self) -> None:
        tb = self

        class Api(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def send(self, reply: ApiReply) -> None:
                code, payload, headers = reply
                data = json.dumps(payload).encode()
                self.send_response(code)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                self.send(tb.api_post(self.path, body))

            def do_GET(self) -> None:
                self.send(tb.api_get(self.path))

        self.api = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Api)
        self.api_url = f"http://127.0.0.1:{self.api.server_address[1]}"
        threading.Thread(target=self.api.serve_forever, daemon=True).start()

    # -------------------------------------------------------------- proxy
    def _start_proxy(self) -> None:
        tb = self

        class Proxy(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                if self.headers.get("Proxy-Authorization") != GOOD_AUTH:
                    tb.rejected += 1
                    self.send_response(407)
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    return
                tb.forwarded.append(self.path)
                u = urlsplit(self.path)           # absolute URI: http://127.0.0.1:port/...
                conn = http.client.HTTPConnection(str(u.hostname), u.port, timeout=10)
                conn.request("GET", u.path or "/", headers={"X-Test-Proxy": "1",
                                                            "User-Agent": self.headers.get("User-Agent", "")})
                r = conn.getresponse()
                body = r.read()
                self.send_response(r.status)
                for k, v in r.getheaders():
                    if k.lower() not in ("connection", "content-length", "transfer-encoding"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self.ca.issue_cert("localhost").configure_cert(ctx)
        self.proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        self.proxy.socket = ctx.wrap_socket(self.proxy.socket, server_side=True)
        self.proxy_port = self.proxy.server_address[1]
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()

    def start(self, environment: ProcessEnvironment) -> "MockTunnelBear":
        """Start both servers, and point xf at them through the variables it reads for that."""
        self._start_api()
        self._start_proxy()
        environment.set_env("TB_DASHBOARD_URL", self.api_url + "/dashboard/web")
        environment.set_env("TB_API_URL", self.api_url)
        environment.set_env("TB_PB_API_URL", self.api_url)
        environment.set_env("TB_PROXY_PORT", str(self.proxy_port))
        environment.set_env("REQUESTS_CA_BUNDLE", str(self.ca_file))
        environment.set_env("TB_USER", "me@example.com")
        environment.set_env("TB_PASS", "not-a-real-password")
        return self

    def stop(self) -> None:
        for s in (self.api, self.proxy):
            s.shutdown()
            s.server_close()

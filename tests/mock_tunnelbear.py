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
from urllib.parse import urlsplit

import trustme

GOOD_AUTH = "Basic " + base64.b64encode(b"tok2:tok2").decode()


class MockTunnelBear:
    def __init__(self, tmp_path):
        self.user_calls = 0
        self.user_calls_allowed = None  # after this many /user calls the API answers 500
        self.forwarded = []           # URLs the proxy forwarded
        self.rejected = 0             # 407s
        self.ca = trustme.CA()
        self.ca_file = tmp_path / "test-ca.pem"
        self.ca.cert_pem.write_to_path(str(self.ca_file))

    # ---------------------------------------------------------------- API
    def _start_api(self):
        tb = self

        class Api(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, obj, code=200, headers=None):
                data = json.dumps(obj).encode()
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path.endswith("/v2/token"):
                    return self.reply({"access_token": "a1", "refresh_token": "r1"})
                if self.path == "/auth":
                    return self.reply({}, headers={"Authorization": "Bearer pb1"})
                self.reply({"message": "not found"}, 404)

            def do_GET(self):
                if self.path == "/user":
                    tb.user_calls += 1
                    if tb.user_calls_allowed is not None and tb.user_calls > tb.user_calls_allowed:
                        return self.reply({"message": "down"}, 500)
                    return self.reply({"vpn_token": "tok1" if tb.user_calls == 1 else "tok2"})
                if self.path == "/countries":
                    return self.reply([{"iso": "FR", "name": "France"}])
                if self.path in ("/vpns", "/vpns/countries/FR"):
                    return self.reply({"vpns": [{"url": "127.0.0.1"}, {"url": "localhost"}]})
                self.reply({"message": "not found"}, 404)

        self.api = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Api)
        self.api_url = f"http://127.0.0.1:{self.api.server_address[1]}"
        threading.Thread(target=self.api.serve_forever, daemon=True).start()

    # -------------------------------------------------------------- proxy
    def _start_proxy(self):
        tb = self

        class Proxy(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.headers.get("Proxy-Authorization") != GOOD_AUTH:
                    tb.rejected += 1
                    self.send_response(407)
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    return
                tb.forwarded.append(self.path)
                u = urlsplit(self.path)           # absolute URI: http://127.0.0.1:port/...
                conn = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
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

    def start(self, monkeypatch):
        """Start both servers and point xfcrawl.tb_proxy at them."""
        from xfcrawl import tb_proxy

        self._start_api()
        self._start_proxy()
        monkeypatch.setattr(tb_proxy, "DASHBOARD_URL", self.api_url + "/dashboard/web")
        monkeypatch.setattr(tb_proxy, "TB_API_URL", self.api_url)
        monkeypatch.setattr(tb_proxy, "PB_API_URL", self.api_url)
        monkeypatch.setattr(tb_proxy, "PROXY_PORT", self.proxy_port)
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(self.ca_file))
        monkeypatch.setenv("TB_USER", "me@example.com")
        monkeypatch.setenv("TB_PASS", "not-a-real-password")
        return self

    def stop(self):
        for s in (self.api, self.proxy):
            s.shutdown()
            s.server_close()

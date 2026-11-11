"""Proxies: the interface the fetcher needs from one, a fixed proxy, and the HTTP session to use with one.

With a proxy set, a crawl must never reach the forum directly. ``proxied_session`` makes sure the proxy is
the only way out: it ignores the proxy environment variables, and always verifies the proxy's certificate.
"""

import re
import ssl
from typing import Any, Protocol

import requests
from requests.adapters import HTTPAdapter

from xfcrawl.errors import XfError

Proxies = dict[str, str]


class ProxyDown(XfError):
    """The proxy can't be used; nothing was sent to the site."""


_AUTH_REFUSED = re.compile(r"\b407 Proxy Authentication Required\b")


def refused_credentials(error: object) -> bool:
    """Whether a proxy error is the proxy refusing its credentials (HTTP 407), which a fresh token may fix.

    Matched on the status line, not on "407" alone: an error's text also holds addresses, and a port such
    as :40701 would look like a refusal."""
    return _AUTH_REFUSED.search(str(error)) is not None


class ProxyRoute(Protocol):
    """Where requests go out: one proxy at a time, switched when it fails."""

    @property
    def max_failures(self) -> int:
        """How many proxy failures in a row mean it is down, rather than one bad server."""
        ...

    def proxies(self) -> Proxies:
        """The ``requests`` proxies for the next request; raises ProxyDown when it can't say."""
        ...

    def failed(self, *, auth_problem: bool) -> None:
        """The proxy just failed; move on, renewing credentials first if they were refused. Raises ProxyDown
        when it can't."""
        ...

    def describe(self) -> str: ...


class FixedProxy:
    """One proxy URL, such as ``https://user:pass@proxy.example:443``."""

    max_failures = 3

    def __init__(self, url: str) -> None:
        self._url = url

    def proxies(self) -> Proxies:
        return {"http": self._url, "https": self._url}

    def failed(self, *, auth_problem: bool) -> None:
        """Nothing to switch to; the fetcher gives up after max_failures."""

    def describe(self) -> str:
        return f"proxy {self._url}"


class _VerifiedProxyAdapter(HTTPAdapter):
    """requests verifies an HTTPS proxy's certificate only when the target URL is https too; for http://
    targets the proxy's TLS, which carries its credentials, would go unchecked. This always checks it."""

    def __init__(self, ca_bundle: str) -> None:
        self._ca_bundle = ca_bundle
        super().__init__()

    def proxy_manager_for(self, proxy: str, **proxy_kwargs: Any) -> Any:
        if proxy.lower().startswith("https://"):
            proxy_kwargs.setdefault("cert_reqs", "CERT_REQUIRED")
            proxy_kwargs.setdefault("ca_certs", self._ca_bundle)
            proxy_kwargs.setdefault("proxy_ssl_context", ssl.create_default_context(cafile=self._ca_bundle))
        return super().proxy_manager_for(proxy, **proxy_kwargs)


def proxied_session(ca_bundle: str | None) -> requests.Session:
    """A session whose proxies come only from each request, with every HTTPS proxy's certificate checked
    against ``ca_bundle`` (the usual CA bundle when None)."""
    bundle = ca_bundle or requests.utils.DEFAULT_CA_BUNDLE_PATH
    session = requests.Session()
    session.trust_env = False  # HTTP(S)_PROXY, NO_PROXY and the like can't change or bypass the proxy
    session.verify = bundle
    adapter = _VerifiedProxyAdapter(bundle)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

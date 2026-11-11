"""xf tunnelbear: log in to TunnelBear, list its countries, fetch a URL or run a local proxy through it."""

import argparse
import asyncio
import getpass
import os
import ssl
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, BinaryIO

import requests

from xfcrawl.errors import XfError
from xfcrawl.layout import StoreLayout
from xfcrawl.proxy import proxied_session
from xfcrawl.tunnelbear.account import Credentials, Endpoints, TokenFile, TunnelBearAccount
from xfcrawl.tunnelbear.local_proxy import LocalProxy
from xfcrawl.tunnelbear.rotation import ServerRotation, fetch_through


def add_parser(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subparsers.add_parser(
        "tunnelbear",
        help="use a TunnelBear account as a proxy",
        description=(
            "Use a TunnelBear account as an HTTPS proxy, as its browser extension does. The login is saved in "
            "./.crawl-store/tunnelbear.json (TB_STATE=FILE to keep it elsewhere): its tokens, never the password."
        ),
    )
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("login", help="log in, and save the login here").set_defaults(func=login_main)
    actions.add_parser("logout", help="forget the login saved here").set_defaults(func=logout_main)
    actions.add_parser("countries", help="the countries TunnelBear offers").set_defaults(func=countries_main)
    fetch = actions.add_parser("fetch", help="fetch one URL through TunnelBear, e.g. to check your IP")
    fetch.add_argument("url")
    fetch.add_argument("-c", "--country", help="ISO code, e.g. FR (default: the closest region)")
    fetch.add_argument("-o", "--output", type=Path, help="write the body to this file")
    fetch.set_defaults(func=fetch_main)
    serve = actions.add_parser("serve", help="a local HTTP proxy that relays through TunnelBear")
    serve.add_argument("-c", "--country", help="ISO code, e.g. FR (default: the closest region)")
    serve.add_argument("-p", "--port", type=int, default=8118)
    serve.set_defaults(func=serve_main)


# -- composition roots ---------------------------------------------------------------------------------
def login_main(_args: argparse.Namespace) -> int:
    account = open_account(StoreLayout.in_directory(Path.cwd()), os.environ)
    account.logout()
    account.login(ask_for_credentials(os.environ, interactive=True)(), mfa_code=_ask_mfa_code)
    print(f"Logged in. Saved to {token_path(StoreLayout.in_directory(Path.cwd()), os.environ)}")
    return 0


def logout_main(_args: argparse.Namespace) -> int:
    open_account(StoreLayout.in_directory(Path.cwd()), os.environ).logout()
    print("Logged out (the login saved here is deleted).")
    return 0


def countries_main(_args: argparse.Namespace) -> int:
    account = logged_in_account(StoreLayout.in_directory(Path.cwd()), os.environ)
    print("\n".join(country_lines(account.countries())))
    return 0


def fetch_main(args: argparse.Namespace) -> int:
    rotation = ServerRotation(logged_in_account(StoreLayout.in_directory(Path.cwd()), os.environ), args.country)
    response = fetch_through(rotation, proxied_session(os.environ.get("REQUESTS_CA_BUNDLE")), args.url)
    print(f"HTTP {response.status_code}, {len(response.content)} bytes", file=sys.stderr)
    write_body(response.content, args.output, sys.stdout.buffer)
    return 0


def serve_main(args: argparse.Namespace) -> int:
    rotation = ServerRotation(logged_in_account(StoreLayout.in_directory(Path.cwd()), os.environ), args.country)
    upstream_ssl = ssl.create_default_context(cafile=os.environ.get("REQUESTS_CA_BUNDLE"))
    asyncio.run(LocalProxy(rotation, args.port, upstream_ssl).serve())
    return 0


# -- shared with xf crawl ------------------------------------------------------------------------------
def token_path(layout: StoreLayout, environment: Mapping[str, str]) -> Path:
    return Path(environment["TB_STATE"]) if environment.get("TB_STATE") else layout.tunnelbear_tokens


def open_account(layout: StoreLayout, environment: Mapping[str, str]) -> TunnelBearAccount:
    """The account whose login is saved here; its API is reached directly, not through any proxy."""
    return TunnelBearAccount(
        Endpoints.from_environment(environment), TokenFile(token_path(layout, environment)), requests.Session()
    )


def logged_in_account(layout: StoreLayout, environment: Mapping[str, str]) -> TunnelBearAccount:
    account = open_account(layout, environment)
    if not account.is_logged_in():
        account.login(ask_for_credentials(environment, interactive=sys.stdin.isatty())(), mfa_code=_ask_mfa_code)
    return account


def ask_for_credentials(environment: Mapping[str, str], *, interactive: bool) -> Callable[[], Credentials]:
    """How to get the TunnelBear email and password when no login is saved: TB_USER and TB_PASS, else, in a
    terminal, ask. Asked only when needed, so a saved login never prompts."""

    def credentials() -> Credentials:
        email = environment.get("TB_USER") or (input("TunnelBear email: ") if interactive else "")
        password = environment.get("TB_PASS") or (getpass.getpass("TunnelBear password: ") if interactive else "")
        if not email or not password:
            raise XfError("not logged in to TunnelBear: run `xf tunnelbear login`, or set TB_USER and TB_PASS")
        return Credentials(email, password)

    return credentials


def country_lines(countries: list[dict[str, Any]]) -> list[str]:
    return [f"{c.get('iso', '?'):4} {c.get('name', '')}" for c in sorted(countries, key=lambda c: c.get("name", ""))]


def write_body(body: bytes, output: Path | None, stdout: BinaryIO) -> None:
    if output is None:
        stdout.write(body)
        return
    output.write_bytes(body)


def _ask_mfa_code(kind: str) -> str:
    return input(f"MFA code ({kind}): ").strip()

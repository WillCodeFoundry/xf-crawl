"""xf tunnelbear: log in to TunnelBear, fetch a URL or run a local proxy through it."""

import argparse
import asyncio
import getpass
import os
import random
import ssl
import sys
from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path
from typing import BinaryIO

import requests

from xfcrawl.browser import Browser
from xfcrawl.errors import XfError
from xfcrawl.layout import StoreLayout
from xfcrawl.proxy import proxied_session
from xfcrawl.tunnelbear.account import Credentials, CredentialsFile, Endpoints, TokenFile, TunnelBearAccount
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
    fetch = actions.add_parser("fetch", help="fetch one URL through TunnelBear, e.g. to check your IP")
    fetch.add_argument("url")
    fetch.add_argument("-o", "--output", type=Path, help="write the body to this file")
    fetch.set_defaults(func=fetch_main)
    serve = actions.add_parser("serve", help="a local HTTP proxy that relays through TunnelBear")
    serve.add_argument("-p", "--port", type=int, default=8118)
    serve.set_defaults(func=serve_main)


# -- composition roots ---------------------------------------------------------------------------------
def login_main(_args: argparse.Namespace) -> int:
    account = open_account(StoreLayout.in_directory(Path.cwd()), os.environ)
    account.logout()
    layout = StoreLayout.in_directory(Path.cwd())
    # typed in a terminal, so a particular account can be chosen; else from the credentials file
    account.login(ask_for_credentials(layout, interactive=sys.stdin.isatty(), typed_first=True)(),
                  mfa_code=_ask_mfa_code)
    print(f"Logged in. Saved to {token_path(StoreLayout.in_directory(Path.cwd()), os.environ)}")
    return 0


def logout_main(_args: argparse.Namespace) -> int:
    open_account(StoreLayout.in_directory(Path.cwd()), os.environ).logout()
    print("Logged out (the login saved here is deleted).")
    return 0


def fetch_main(args: argparse.Namespace) -> int:
    rotation = ServerRotation(logged_in_account(StoreLayout.in_directory(Path.cwd()), os.environ))
    session = proxied_session(os.environ.get("REQUESTS_CA_BUNDLE"))
    session.headers.update(Browser.pick(random.Random(), date.today()).headers)
    response = fetch_through(rotation, session, args.url)
    print(f"HTTP {response.status_code}, {len(response.content)} bytes", file=sys.stderr)
    write_body(response.content, args.output, sys.stdout.buffer)
    return 0


def serve_main(args: argparse.Namespace) -> int:
    rotation = ServerRotation(logged_in_account(StoreLayout.in_directory(Path.cwd()), os.environ))
    upstream_ssl = ssl.create_default_context(cafile=os.environ.get("REQUESTS_CA_BUNDLE"))
    asyncio.run(LocalProxy(rotation, args.port, upstream_ssl).serve())
    return 0


# -- shared with xf crawl ------------------------------------------------------------------------------
def token_path(layout: StoreLayout, environment: Mapping[str, str]) -> Path:
    return Path(environment["TB_STATE"]) if environment.get("TB_STATE") else layout.tunnelbear_tokens


def open_account(layout: StoreLayout, environment: Mapping[str, str]) -> TunnelBearAccount:
    """The account whose login is saved here; its API is reached directly, not through any proxy."""
    return TunnelBearAccount(
        Endpoints.from_environment(environment), TokenFile(token_path(layout, environment)), requests.Session(),
        relogin=CredentialsFile(layout.tunnelbear_credentials).pick,
    )


def logged_in_account(layout: StoreLayout, environment: Mapping[str, str]) -> TunnelBearAccount:
    account = open_account(layout, environment)
    if not account.is_logged_in():
        account.login(ask_for_credentials(layout, interactive=sys.stdin.isatty())(), mfa_code=_ask_mfa_code)
    return account


def ask_for_credentials(
    layout: StoreLayout, *, interactive: bool, typed_first: bool = False
) -> Callable[[], Credentials]:
    """How to get a TunnelBear email and password when no login is saved: an account picked at random from
    tunnelbear-credentials.json, else, in a terminal, typed; with ``typed_first``, typed if there is a
    terminal. Asked only when needed, so a saved login never prompts."""
    accounts = CredentialsFile(layout.tunnelbear_credentials)

    def typed() -> Credentials | None:
        if not interactive:
            return None
        return Credentials(input("TunnelBear email: "), getpass.getpass("TunnelBear password: "))

    def credentials() -> Credentials:
        sources = (typed, accounts.pick) if typed_first else (accounts.pick, typed)
        for source in sources:
            found = source()
            if found is not None and found.email and found.password:
                return found
        raise XfError(f"not logged in to TunnelBear: run `uv run xf tunnelbear login`, or list accounts in "
                      f"{accounts.path}")

    return credentials


def write_body(body: bytes, output: Path | None, stdout: BinaryIO) -> None:
    if output is None:
        stdout.write(body)
        return
    output.write_bytes(body)


def _ask_mfa_code(kind: str) -> str:
    return input(f"MFA code ({kind}): ").strip()

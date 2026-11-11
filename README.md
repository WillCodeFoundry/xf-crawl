# xf-crawl

[![CI](https://github.com/WillCodeFoundry/xf-crawl/actions/workflows/ci.yml/badge.svg)](https://github.com/WillCodeFoundry/xf-crawl/actions/workflows/ci.yml)

Polite, resumable crawler for XenForo 2.x forums. `xf-crawl` downloads pages
into `./.crawl-store`; `xf-mine` analyses them offline; `tb-proxy` uses a
TunnelBear account as a proxy.

## Setup

Needs [uv](https://docs.astral.sh/uv/). Nothing to install: run the commands
from this folder with `uv run`. The first run creates `.venv/` here with
Python and the dependencies, and code changes take effect immediately.

```bash
git clone https://github.com/WillCodeFoundry/xf-crawl.git
cd xf-crawl
```

## Use

The crawl is kept in `.crawl-store/` in this folder.

```bash
uv run xf-crawl http://localhost/forum/     # first run
uv run xf-crawl                             # continue / fill gaps (start URL is remembered)
uv run xf-crawl --list                      # what is still to crawl (offline)
uv run xf-mine                              # statistics (--json for JSON)
```

### More than one forum

`.crawl-store/` is created in the directory you run from, so keep one
directory per forum and point uv at this folder:

```bash
mkdir ~/crawls/forum-a && cd ~/crawls/forum-a
uv run --project ~/path/to/xf-crawl xf-crawl https://forum-a.example/
```

## Proxies

With a proxy set, the crawler never connects directly: if the proxy fails,
the run stops. The choice is remembered in `.crawl-store/config.json`.

### TunnelBear

```bash
uv run tb-proxy login                       # once; asks for email, password (and MFA code)
uv run xf-crawl https://forum.example/ --tunnelbear FR
uv run xf-crawl                             # keeps using TunnelBear
uv run xf-crawl --direct                    # stop using it
```

`tb-proxy login` saves login tokens, never your password, in
`.crawl-store/tunnelbear.json` in the current directory (readable only by you).
The tokens are renewed automatically, so you only log in again after
`uv run tb-proxy logout` or if TunnelBear revokes them. The login belongs to
the directory: run `tb-proxy login` in each crawl directory that uses TunnelBear.

Without a terminal to type into (cron, scripts), give the credentials as
environment variables instead. They are only used when no tokens are saved yet,
and don't work for accounts with MFA:

```bash
TB_USER='you@example.com' TB_PASS='secret' uv run xf-crawl
```

`TB_STATE=/other/path.json` keeps the tokens somewhere else instead.

### Any other proxy

```bash
uv run xf-crawl --proxy http://127.0.0.1:8118                # e.g. uv run tb-proxy serve -c FR
uv run xf-crawl --proxy 'https://user:pass@proxy.example:443'
```

Put the username and password in the URL. URL-encode special characters
(`@` -> `%40`, `:` -> `%3A`). The URL, password included, is stored in plain
text in `.crawl-store/config.json`.

## Layout

```
.crawl-store/
  config.json                                start URL and proxy setting
  tunnelbear.json                            TunnelBear login tokens (if used)
  pages/index.html.gz
  pages/forums/french-english.46/page-0001.html.gz
  pages/threads/1234/parler-le-francais.1234567/page-0001.html.gz
  pages/threads/1234/parler-le-francais.1234567/page-0002.failed.jsonl
```

Folders are named like the site's URLs. The number is what counts: if a forum
or thread is renamed on the site, its folder is renamed to match. Threads are
grouped in folders of 1000 by number (`1234567 // 1000 = 1234`). A `.failed.jsonl`
file has one line per failed attempt to fetch that page.

## Upgrading a crawl store

After a code update, `xf-crawl` and `xf-mine` refuse to run on a crawl store
in an older layout. Back it up and convert it (nothing is downloaded):

```bash
tar czf crawl-store-backup.tgz .crawl-store
uv run xf-migrate
```

## Development

```bash
uv run pytest          # tests: a fake XenForo forum and a fake TunnelBear run locally
uv run ruff check .    # lint
```

The tests never touch the network. `tests/test_proxy.py` checks that with a
proxy set, no request ever reaches the forum directly: the fake proxy marks
what it forwards, and the fake forum records anything that arrives without
that mark. CI (`.github/workflows/ci.yml`) runs both on every pull request and
on `main` (Python 3.10–3.13 on Linux, 3.13 on macOS); new jobs must be added
to `ci-gate`'s `needs`, the one check the repository's rules require.

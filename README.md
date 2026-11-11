# xf-crawl

[![CI](https://github.com/WillCodeFoundry/xf-crawl/actions/workflows/ci.yml/badge.svg)](https://github.com/WillCodeFoundry/xf-crawl/actions/workflows/ci.yml)

Polite, resumable crawler for XenForo 2.x forums. `xf crawl` downloads a forum into `./.crawl-store`; `xf mine` analyses it offline; `xf tunnelbear` uses a TunnelBear account as a proxy.

## Setup

Needs [uv](https://docs.astral.sh/uv/). Nothing to install: run the commands from this folder with `uv run`. The first run creates `.venv/` here with Python and the dependencies.

```bash
git clone https://github.com/WillCodeFoundry/xf-crawl.git
cd xf-crawl
uv run xf --help
```

## Use

```bash
uv run xf crawl http://localhost/forum/     # first run
uv run xf crawl                             # carry on / fill gaps (the URL is remembered)
uv run xf pending                           # what is still to fetch (no network)
uv run xf mine                              # statistics (--json for JSON)
```

- **Level by level:** first the forum list, then every forum's thread list, then every thread's first page, then the threads' other pages. Stop it at any stage and you have everything above it: all forums, then all thread titles, then every thread's opening.
- **Resumable:** stop it at any time, or let it crash. The next run fetches only what isn't stored yet.
- **Polite:** about 1 to 1.5 seconds between requests (`--delay`, `--jitter`), robots.txt obeyed (`--ignore-robots` not to), and waits and retries when the site is busy.
- **Failures:** a page that can't be fetched is never stored. Its failures are recorded beside it and retried on later runs, waiting longer after each one: 1h, 2h, 4h… for temporary errors, 1d, 2d… for pages that look gone. `--retry-failed` tries them now.

### More than one forum

`.crawl-store/` is created in the directory you run from, so keep one directory per forum and point uv at this folder:

```bash
mkdir ~/crawls/forum-a && cd ~/crawls/forum-a
uv run --project ~/path/to/xf-crawl xf crawl https://forum-a.example/
```

## Proxies

With a proxy set, the crawler never connects to the forum directly. If the proxy fails, the run stops. The choice is remembered in `.crawl-store/config.json`.

### TunnelBear

```bash
uv run xf tunnelbear login                     # once; asks for email, password (and MFA code)
uv run xf crawl https://forum.example/ --tunnelbear FR
uv run xf crawl                                # keeps using TunnelBear
uv run xf crawl --direct                       # stop using it
uv run xf tunnelbear fetch https://ifconfig.me -c FR    # check the IP sites see
uv run xf tunnelbear serve -c FR --port 8118   # a local proxy any program can use
```

`xf tunnelbear login` saves login tokens, never your password, in `.crawl-store/tunnelbear.json` in the current directory (readable only by you). The tokens are renewed automatically, so you only log in again after `uv run xf tunnelbear logout` or if TunnelBear revokes them. The login belongs to the directory: log in in each crawl directory that uses TunnelBear, or set `TB_STATE=/path/to/file.json` to share one.

Without a terminal to type into (cron, scripts), set `TB_USER` and `TB_PASS` instead. They are only used when no login is saved, and don't work for accounts with MFA.

This uses TunnelBear's private API, as its browser extension does; it can change without notice, and using it may conflict with TunnelBear's terms. Every page counts against the account's data allowance.

### Any other proxy

```bash
uv run xf crawl --proxy http://127.0.0.1:8118
uv run xf crawl --proxy 'https://user:pass@proxy.example:443'
```

URL-encode special characters in the username and password (`@` -> `%40`, `:` -> `%3A`). The URL, password included, is stored in plain text in `.crawl-store/config.json`.

## Layout

```
.crawl-store/
  config.json                                                   start URL and proxy setting
  tunnelbear.json                                               TunnelBear login tokens (if used)
  pages/index.html.gz
  pages/forums/french-english.46/page-0001.html.gz
  pages/threads/1234/parler-le-francais.1234567/page-0001.html.gz
  pages/threads/1234/parler-le-francais.1234567/page-0002.failed.jsonl
```

Folders are named like the site's URLs. The number is what counts: if a forum or thread is renamed on the site, its folder is renamed to match. Threads are grouped in folders of 1000 by number (`1234567 // 1000 = 1234`). A `.failed.jsonl` file has one line per failed attempt to fetch that page.

## Upgrading a crawl store

After a code update, `xf crawl`, `xf pending` and `xf mine` refuse to run on a crawl store in an older layout. Back it up and convert it (nothing is downloaded):

```bash
tar czf crawl-store-backup.tgz .crawl-store
uv run xf migrate
```

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for the checks and tests, and [ARCHITECTURE.md](ARCHITECTURE.md) for how the code is organised and the rules it follows.

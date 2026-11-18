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
uv run xf status                            # how far it has got, and time left (--forums for each forum)
uv run xf pending                           # what is still to fetch (no network)
uv run xf mine                              # statistics (--json for JSON)
```

- **Level by level:** first the forum list, then every forum's thread list, then every thread's first page, then the threads' other pages. Stop it at any stage and you have everything above it: all forums, then all thread titles, then every thread's opening.
- **Resumable:** stop it at any time, or let it crash. The next run fetches only what isn't stored yet.
- **Polite:** about 1 to 1.5 seconds between requests (`--delay`, `--jitter`), robots.txt obeyed (`--ignore-robots` not to), and pauses when pages keep failing.
- **Looks like a person browsing:**
  - **One browser:** the first run picks a common desktop browser at random (Chrome, Edge, Firefox or Safari, on Windows, macOS or Linux, a version or two from the latest) and sends its user agent and headers. Later runs keep it, updating its version as browsers update themselves. The first lines of a run say which, e.g. `Passing for Firefox 158 on Linux, back with 1 cookies`.
  - **Cookies:** the site's cookies are kept, within a run and from run to run, in `.crawl-store/visitor.json` (readable only by you). `--new-browser` starts afresh with a new browser and no cookies. `--cookie` replaces the kept cookies for that run.
  - **Followed links:** each page is asked for as if from a link on the page a person would come from: the page before it, the forum listing a thread, or the forum index.
  - **Script checks:** some sites, WordReference among them, answer a visitor they don't know yet with a short page whose script sets a cookie and reloads (WordReference uses HTTP 418 for it). A browser does this without anyone noticing; the crawler sets the cookie the page asks for and asks again, once. The cookie is then kept like any other.
  - **Breaks:** about one gap in a hundred is a break of 5 to 20 times `--delay`. That adds about a tenth to a crawl's time.
  - **robots.txt** rules are still matched as `xf-crawl`, falling back to the rules for every robot.
- **Failures:** a page that can't be fetched is never stored. Its failures are recorded beside it and tried again later, waiting longer after each failure: 15 min, 30 min, 1h… for trouble (below), coming back within the same run once its wait is over; 1d, 2d… for pages that look gone. A page is never asked again straight away. A thread merged into another (its address redirects to the other thread) is recorded once and never tried again: the other thread is crawled instead. A page past a thread's end is rechecked, as the thread may grow. `--retry-failed` tries all of them now.
- **When pages keep failing:** an answer that may be about the crawler rather than the page is *trouble*: 429, a 403 from in front of the forum (not XenForo's own "no permission" page), 5xx, a 418 that persists, an error page, a timeout. Each page failing in a row for trouble takes a step up this ladder before the next request:

  | Failing in a row | Pause | New server and visitor |
  |---|---|---|
  | 1 | 30 s | |
  | 2 | 1 min | ✓ |
  | 3 | 2 min | ✓ |
  | 4 | 5 min | ✓ |
  | 5 | 10 min | ✓ |
  | 6 and on | 15 min | ✓ |

  A site's `Retry-After` lengthens a pause, and `--longest-pause SECONDS` (default 900) caps every one. A page that comes through, or a normal answer (a deleted, merged or private thread), goes back to the bottom. A new visitor is a new browser with none of the site's cookies. Without TunnelBear there's no other server, so only the visitor changes. The ladder is `LADDER` in `src/xfcrawl/trouble.py`.
- **Stops when that doesn't help:** after the 15-minute step has been tried three times in a row and the next page still fails (9 pages in a row, over about an hour, 7 servers and visitors), the run stops with an error and exit code 1, saying what it tried and what the site answered. A run stopped because the proxy failed also exits with 1.

### Only some forums

```bash
uv run xf crawl https://forum.example/ --only-forums 46,47   # remembered for later runs
uv run xf crawl --all-forums                                  # back to every forum
```

Only those forums' thread lists are fetched, and only the threads they list. A forum's number is the one in its URL: `/forums/french-english.46/` is forum 46; `xf status --forums` lists them too. Sub-forums are not included: list their numbers as well. Pages already stored from other forums stay on disk, left out of the crawl and of `xf status`.

### More than one forum site

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
uv run xf crawl https://forum.example/ --tunnelbear
uv run xf crawl                                # keeps using TunnelBear
uv run xf crawl --direct                       # stop using it
uv run xf tunnelbear fetch https://ipinfo.io/json  # check the IP sites see
uv run xf tunnelbear serve --port 8118         # a local proxy any program can use
uv run xf tunnelbear data                      # how much of the account's data allowance is left
```

TunnelBear's proxy servers in every country are used in turn, in a shuffled order: one server for 8,000 requests, then the next, and the next at once if one fails. Whenever the site would see the crawler from a new server, it also sees a new visitor: a new browser, with none of its cookies, so the two servers can't be linked. The log says which, e.g. `After 8,000 requests, moving to TunnelBear server de-123.lazerpenguin.com (DE)`. The 8,000 is `REQUESTS_PER_SERVER` in `src/xfcrawl/tunnelbear/rotation.py`. The server list is fetched at the start and every 6 hours; if a request can't get through on any of 10 servers in a row, TunnelBear is taken to be down and the crawl stops.

`xf tunnelbear login` saves login tokens, never your password, in `.crawl-store/tunnelbear.json` in the current directory (readable only by you). The tokens are renewed automatically, so you only log in again after `uv run xf tunnelbear logout` or if TunnelBear revokes them. The login belongs to the directory: log in in each crawl directory that uses TunnelBear, or set `TB_STATE=/path/to/file.json` to share one.

#### Logging in without anyone typing

List one or more accounts in `.crawl-store/tunnelbear-credentials.json`, readable only by you (xf refuses it otherwise). Git ignores it, wherever it is:

```bash
mkdir -p .crawl-store
cat > .crawl-store/tunnelbear-credentials.json <<'JSON'
[
  {"email": "me@example.com", "password": "..."},
  {"email": "other@example.com", "password": "..."}
]
JSON
chmod 600 .crawl-store/tunnelbear-credentials.json
```

Whenever a login is needed and nobody is there to type, xf picks one of these accounts at random:

- **No saved login:** a crawl, or `xf tunnelbear login` without a terminal, logs in with one. `xf tunnelbear login` in a terminal still asks, so you can choose the account; press Enter at the prompts to pick from the file instead.
- **Saved login refused:** the tokens are refreshed as TunnelBear asks, so a saved login lasts. If TunnelBear stops accepting it (it expired after a long break, or was revoked), a crawl logs in again with an account from the file and carries on. Without the file it stops, nothing sent, and says to run `uv run xf tunnelbear login`.

xf only reads this file, never writes it; the passwords stay there and nowhere else. Accounts with MFA can't log in this way.

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
  links.jsonl                                                   what each page links to, so restarts are quick
  tunnelbear.json                                               TunnelBear login tokens (if used)
  tunnelbear-credentials.json                                   TunnelBear accounts, kept by you (if used)
  visitor.json                                                  the browser the crawl passes for, and the site's cookies
  pages/index.html.gz
  pages/forums/french-english.46/page-0001.html.gz
  pages/threads/1234/parler-le-francais.1234567/page-0001.html.gz
  pages/threads/1234/parler-le-francais.1234567/page-0002.failed.jsonl
```

`links.jsonl` only saves re-reading pages when a run starts. If it is deleted, the next run works it out again from the pages, more slowly.

Folders are named like the site's URLs. The number is what counts: if a forum or thread is renamed on the site, its folder is renamed to match. Threads are grouped in folders of 1000 by number (`1234567 // 1000 = 1234`). A `.failed.jsonl` file has one line per failed attempt to fetch that page.

## Upgrading a crawl store

After a code update, `xf crawl`, `xf pending` and `xf mine` refuse to run on a crawl store in an older layout. Back it up and convert it (nothing is downloaded):

```bash
tar czf crawl-store-backup.tgz .crawl-store
uv run xf migrate
```

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for the checks and tests, and [ARCHITECTURE.md](ARCHITECTURE.md) for how the code is organised and the rules it follows.

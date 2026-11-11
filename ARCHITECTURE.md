# Architecture

How xf-crawl is put together, and the rules its code follows.

## Bird's-eye view

`xf` downloads a XenForo 2 forum into `./.crawl-store`, and reads it back without the network. A crawl, end to end:

1. `xf crawl URL` (`commands/crawl.py`) refuses a store in an older layout (`migration`), loads or creates the crawl's config, and opens the store: `PageStore` reads every file name under `.crawl-store/pages` once.
2. `CrawlPlan` works out what is still to fetch from what is stored: every link on the index and forum pages, and each thread's page navigation. There is no queue on disk, so stopping, crashing or deleting a page is always safe.
3. If anything is to fetch, `Crawl` connects: it builds the fetcher (through the proxy, if one is set), reads robots.txt through it, and fetches page after page. `judge` decides whether each answer is the page asked for, whole. If so, `PageStore` saves it and the plan discovers its links. If not, the failure is recorded against the page, and nothing of it is stored.
4. A page that failed is retried on later runs, with a wait that doubles after each failure (`RetryPolicy`).

`xf pending` and `xf mine` open the same store and plan, and never touch the network.

## Codemap

### `src/xfcrawl/`

| Module | What it does |
|---|---|
| `cli.py` | The `xf` command. It prints the errors xf's code raises, and shows progress on stderr. |
| `commands/` | One module per subcommand: `crawl`, `pending`, `mine`, `migrate`, `tunnelbear`. Each `main` is a composition root: it builds the real store, clock, sessions and TunnelBear account, and passes them to the code that uses them (DR7). Helpers beside it hold any choice, so tests reach them. |
| `urls.py` | XenForo's URL rules: `PageRef` names a page (kind, number, page), `parse_page_url` recognises one, `slug_of` reads its words. |
| `layout.py` | Where everything sits in `.crawl-store`: `StoreLayout`, and how folders and page files are named. |
| `store.py` | `PageStore`: reading and writing stored pages and failure histories, naming and renaming item folders. |
| `failures.py` | `Failure`, its JSON-lines format, and `RetryPolicy`: when a failing page may be tried again. |
| `plan.py` | `CrawlPlan`: what is still to fetch, rebuilt from the store, growing as pages are fetched. |
| `xenforo.py` | What the crawler reads from a page's HTML: links, the template name, whether it arrived whole. |
| `fetching.py` | `PoliteFetcher`: the only module that sends the crawler's requests. Pacing, retries, and failing closed with a proxy. |
| `proxy.py` | The `ProxyRoute` protocol, `FixedProxy`, `ProxyDown`, and `proxied_session`, which makes the proxy the only way out. |
| `robots.py` | `RobotsRules`: the site's robots.txt, fetched through the fetcher. |
| `crawler.py` | The crawl loop: `judge`, `connect`, `Crawl`. |
| `config.py` | `CrawlConfig`: the start URL and proxy a store remembers. |
| `stored_crawl.py` | A crawl as stored, for the commands that read one without crawling. |
| `migration.py` | Converting older stores; the only module that knows their layouts. |
| `mining/` | `pages.py` reads XenForo pages with BeautifulSoup; `stats.py` computes `xf mine`'s statistics. |
| `tunnelbear/` | `account.py` talks to TunnelBear's API; `rotation.py` makes the account a `ProxyRoute`; `local_proxy.py` is `xf tunnelbear serve`. |
| `atomic_files.py`, `errors.py` | Writing a file in one step; `XfError`, a problem the user can fix. |

### Everything else

| Path | What it is |
|---|---|
| `tests/` | One test module per module or command. `tests/support/` holds the fake forum, the fake TunnelBear and `ProcessEnvironment`; `conftest.py` holds only fixtures. |
| `.github/workflows/ci.yml` | Lint, types, and the tests with coverage. `ci-gate` is the check the repository's rules require. |

## Design rules

Follow these when changing the code. Each names what holds it. Cite a rule by its number, as `DR3`.

1. **One spelling per shared name.** A URL rule is spelled in `urls.py`, the store's layout in `layout.py`, the failure format in `failures.py`, and older layouts only in `migration.py`. *Held by review, and by the tests of each module.*
2. **With a proxy set, nothing reaches the forum directly.** Every request the crawler sends goes through `PoliteFetcher`, which takes the proxy's settings before each request and raises `ProxyDown` rather than send without them. `proxied_session` ignores the proxy environment variables and always verifies the proxy's certificate. A proxy failure stops the run; it is never blamed on a page. *Held by `tests/test_proxy_leaks.py` and `tests/test_fetching.py`, which fail if the code is changed to send without the proxy.*
3. **One door to each outside system.** The forum is reached through `fetching.py`, TunnelBear's API through `tunnelbear/account.py`, and `.crawl-store/pages` through `store.py`. Tests never patch: they run a fake forum and a fake TunnelBear on localhost, and pass fakes where a module takes them. *Held by `tests/test_rules.py`; ruff bans `unittest.mock`.*
4. **The store is the state.** There is no queue or log on disk to keep in step with the pages. Anything the crawler needs to know is worked out from the stored pages and their failure files on each run. *Held by `tests/test_plan.py` and `tests/test_crawl_command.py`.*
5. **Never store what isn't the page asked for.** Error pages, login pages, cut-off pages, redirects to other pages and HTTP errors are recorded as failures; only whole, real pages are saved, each in one atomic step. *Held by `tests/test_crawl_command.py` and `tests/test_store.py`.*
6. **Fail fast, with a message; only entry points print.** An unexpected state raises; it is not repaired, defaulted or retried around. An exception's text is the message the user reads. Library modules report progress to their `logging` logger, and `cli.py` shows it. *Held by ruff's `BLE`, `S110` and `T20` rules and its ban on `sys.stdout` and `sys.stderr` outside `cli.py` and `commands/`.*
7. **A composition root only builds and passes.** A command's `main` builds the real store, clock, sessions and accounts, and passes them on. Any check or choice is in a function the tests call with their own store, fakes and clock. *Held by review.*

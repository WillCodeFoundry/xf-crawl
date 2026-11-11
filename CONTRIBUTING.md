# Contributing

How to develop and test xf-crawl. Read [ARCHITECTURE.md](ARCHITECTURE.md) first: its design rules apply to every change.

## Run the checks

```console
$ uv run pytest                 # the tests; about 30 seconds, no network
$ uv run pytest --cov           # the same, failing if coverage drops below fail_under, as CI runs it
$ uv run ruff check .
$ uv run mypy
```

`uv run` installs the dev dependencies (pytest, pytest-cov, ruff, mypy, trustme) into `.venv/` on first use.

### What the checks catch

| Check | Catches |
|---|---|
| `ruff check` | blanket and silent `except`, `print` and `sys.stdout`/`sys.stderr` outside `cli.py` and `commands/`, `assert` in `src/`, functions past the size limits, boolean flag parameters, private access across objects, commented-out code, blanket `noqa` and `type: ignore`, unsafe calls, and `unittest.mock` |
| `mypy` | strict typing, plus unreachable code, redundant expressions and always-true conditions |
| `pytest --cov` | branches no test runs: CI fails when coverage of `src/` drops below `fail_under` in `pyproject.toml` |
| `tests/test_rules.py` | `requests` or BeautifulSoup imported outside their doors (DR3), and tests using `monkeypatch` |

`pyproject.toml` says why each ruff rule is on. Silence a finding on one line with `# noqa: <code>` and the reason.

## Tests

Each module has its tests in `tests/test_<module>.py`; each command in `tests/test_<command>_command.py` or with its module. The commands are tested through the real `xf` entry point, in-process, with the `xf` fixture.

Tests never patch (DR3). They run against fakes in `tests/support/`, on localhost:

| Need | Helper |
|---|---|
| A XenForo forum: pages, sub-forums, multi-page threads, 404s, error pages, 503s, renames, robots.txt | `MockForum` in `tests/support/mock_forum.py`; the `forum` fixture |
| Whether a request reached the forum without the proxy | `forum.direct_hits()`: the fake proxy marks what it forwards |
| TunnelBear: its API (with MFA, and a token that goes stale) and a TLS proxy with a throwaway CA | `MockTunnelBear` in `tests/support/mock_tunnelbear.py`; the `tunnelbear` fixture |
| An environment variable or the working directory, for this test only | the `environment` fixture, a `ProcessEnvironment` |
| The real `xf` command | the `xf` fixture: `xf("crawl", url)` gives the exit code, stdout and stderr |

Every test runs in an empty directory of its own, with no proxy variables or TunnelBear login from the machine running it. A test changes its own process only through `ProcessEnvironment`, never `monkeypatch`; `tests/test_rules.py` checks that.

The tests that hold DR2 fail when the code is changed to send a request without the proxy. When you change `fetching.py`, `proxy.py` or `tunnelbear/`, break the rule on purpose and check a test fails.

## CI

`.github/workflows/ci.yml` runs on every pull request and every push to main: actionlint, ruff, mypy, and the tests with coverage on Python 3.10 to 3.13 (Linux) and 3.13 (macOS). Its last job, `ci-gate`, passes only if every other job did, and is the check the repository's rules require. Add a new job to `ci-gate`'s `needs`, or it won't block anything.

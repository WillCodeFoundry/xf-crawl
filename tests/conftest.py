from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.support import OUTSIDE_VARIABLES, CliResult, ProcessEnvironment, process_environment
from tests.support.mock_forum import MockForum
from tests.support.mock_tunnelbear import MockTunnelBear
from xfcrawl import cli


@pytest.fixture(autouse=True)
def environment(tmp_path: Path) -> Iterator[ProcessEnvironment]:
    """Every test runs in an empty directory of its own, so .crawl-store is fresh, with nothing from the
    machine running the tests that could route requests or log in to a real TunnelBear."""
    with process_environment() as env:
        for name in OUTSIDE_VARIABLES:
            env.unset_env(name)
        env.change_dir(tmp_path)
        yield env


@pytest.fixture
def forum() -> Iterator[MockForum]:
    mock = MockForum().start()
    yield mock
    mock.stop()


@pytest.fixture
def tunnelbear(tmp_path: Path, environment: ProcessEnvironment) -> Iterator[MockTunnelBear]:
    mock = MockTunnelBear(tmp_path).start(environment)
    yield mock
    mock.stop()


@pytest.fixture
def xf(capsys: pytest.CaptureFixture[str]) -> Callable[..., CliResult]:
    """The real xf command, in-process: ``xf("crawl", url)``. Crawls don't wait between requests unless
    the test passes --delay itself."""

    def run(*argv: str) -> CliResult:
        if argv[0] == "crawl" and "--delay" not in argv:
            argv = (*argv, "--delay", "0", "--jitter", "0")
        code = cli.main(list(argv))
        captured = capsys.readouterr()
        return CliResult(code, captured.out, captured.err)

    return run

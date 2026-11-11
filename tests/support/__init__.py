"""Helpers the tests share. The conftest holds only fixtures."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

# Variables that would route requests elsewhere, or point at a real TunnelBear login.
OUTSIDE_VARIABLES = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "REQUESTS_CA_BUNDLE", "TB_STATE", "TB_USER", "TB_PASS",
    "TB_DASHBOARD_URL", "TB_API_URL", "TB_PB_API_URL", "TB_PROXY_PORT",
)


class ProcessEnvironment:
    """The one way a test changes its own process: environment variables and the working directory.
    Everything is put back when the test ends."""

    def __init__(self, patcher: pytest.MonkeyPatch) -> None:
        self._patcher = patcher

    def set_env(self, name: str, value: str) -> None:
        self._patcher.setenv(name, value)

    def unset_env(self, name: str) -> None:
        self._patcher.delenv(name, raising=False)

    def change_dir(self, directory: Path) -> None:
        self._patcher.chdir(directory)


@contextmanager
def process_environment() -> Iterator[ProcessEnvironment]:
    with pytest.MonkeyPatch.context() as patcher:
        yield ProcessEnvironment(patcher)


@dataclass(frozen=True)
class CliResult:
    code: int
    out: str
    err: str


import contextlib
import io
import json
import sys

import pytest
from mock_forum import MockForum
from mock_tunnelbear import MockTunnelBear

from xfcrawl import crawl, migrate, mine


@pytest.fixture(autouse=True)
def workdir(tmp_path, monkeypatch):
    """Every test runs in its own empty directory, so .crawl-store is fresh,
    and with no proxy settings inherited from the machine running the tests."""
    monkeypatch.chdir(tmp_path)
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                "http_proxy", "https_proxy", "all_proxy", "no_proxy", "TB_STATE"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


@pytest.fixture
def forum():
    f = MockForum().start()
    yield f
    f.stop()


@pytest.fixture
def tunnelbear(tmp_path, monkeypatch):
    tb = MockTunnelBear(tmp_path).start(monkeypatch)
    yield tb
    tb.stop()


class Result:
    def __init__(self, code, out, err):
        self.code, self.out, self.err = code, out, err

    def json(self):
        return json.loads(self.out)


def _run(main, prog, args, monkeypatch):
    monkeypatch.setattr(sys, "argv", [prog, *args])
    out, err, code = io.StringIO(), io.StringIO(), 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            main()
        except SystemExit as e:
            if isinstance(e.code, str):
                print(e.code, file=sys.stderr)
                code = 1
            else:
                code = e.code or 0
    return Result(code, out.getvalue(), err.getvalue())


@pytest.fixture
def run(monkeypatch):
    """run("xf-crawl", URL, "--delay", "0") -> Result(code, out, err), in-process."""
    mains = {"xf-crawl": crawl.main, "xf-mine": mine.main, "xf-migrate": migrate.main}

    def runner(cmd, *args):
        if cmd == "xf-crawl" and "--delay" not in args:
            args = (*args, "--delay", "0", "--jitter", "0")
        return _run(mains[cmd], cmd, list(args), monkeypatch)

    return runner

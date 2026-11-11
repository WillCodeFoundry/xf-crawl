"""What the xf command itself does: report errors without a traceback, and show progress on stderr."""

import logging

import pytest

from xfcrawl import cli


def test_an_error_is_one_line_and_exit_code_1(xf):
    result = xf("pending")
    assert result.code == 1
    assert result.err.startswith("error: no crawl in")
    assert "Traceback" not in result.err


def test_progress_goes_to_stderr_and_results_to_stdout(forum, xf):
    result = xf("crawl", forum.url, "--max-fetches", "1")
    assert "GET index:0:1" in result.err
    assert result.out.startswith("Stopped: reached --max-fetches 1")


def test_nothing_is_left_behind_on_the_logger(forum, xf):
    before = list(logging.getLogger("xfcrawl").handlers)
    xf("crawl", forum.url, "--max-fetches", "1")
    assert logging.getLogger("xfcrawl").handlers == before


@pytest.mark.parametrize("command", ["crawl", "status", "pending", "mine", "migrate", "tunnelbear"])
def test_every_command_has_help(command, capsys):
    with pytest.raises(SystemExit) as done:
        cli.main([command, "--help"])
    assert done.value.code == 0
    assert capsys.readouterr().out.startswith(f"usage: xf {command}")

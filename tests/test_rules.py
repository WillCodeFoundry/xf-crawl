"""Rules from ARCHITECTURE.md that a test can hold."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src" / "xfcrawl"


def imports(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


def modules_importing(name: str) -> set[str]:
    return {str(path.relative_to(SOURCE)) for path in SOURCE.rglob("*.py") if name in imports(path)}


def test_dr3_the_network_is_reached_only_through_its_doors():
    assert modules_importing("requests") == {
        "fetching.py", "proxy.py", "tunnelbear/account.py", "tunnelbear/rotation.py",
        "commands/crawl.py", "commands/tunnelbear.py",   # composition roots: they build the sessions
    }


def test_dr3_only_mining_parses_html_with_beautifulsoup():
    assert modules_importing("bs4") == {"mining/pages.py"}


def test_tests_change_their_process_only_through_process_environment():
    """Not monkeypatch: tests/support.ProcessEnvironment is the one place, and what it can change is
    reviewed there."""
    offenders = [
        str(path.relative_to(ROOT)) for path in (ROOT / "tests").rglob("*.py")
        if path.name != "__init__.py" and "monkeypatch" in path.read_text() and path.name != "test_rules.py"
    ]
    assert offenders == []

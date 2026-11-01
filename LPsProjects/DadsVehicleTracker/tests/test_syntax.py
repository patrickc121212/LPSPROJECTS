"""Every Python file in the project must at least parse.

On 2026-11-01 an edit left an unterminated f-string in supervisor.pyw. The
running process still held the old code in memory, so nothing failed for
three hours; the next restart took the whole tracker down and left no
traceback, because pythonw.exe has no stderr. 405 tests passed throughout.

Nothing imported supervisor.pyw: it is not a .py file, pytest does not
collect it, and no test ran it. The launcher -- the one file whose failure
takes everything with it -- was the one file nothing checked.
"""
from __future__ import annotations

import py_compile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# .pyw matters as much as .py here: it is what the scheduled task runs.
SOURCES = sorted(
    p for pat in ("*.py", "*.pyw")
    for p in ROOT.glob(pat)
)


def test_there_are_sources_to_check():
    """Guard the guard: a glob that quietly matches nothing would make every
    test below pass without checking anything."""
    names = {p.name for p in SOURCES}
    assert "supervisor.pyw" in names, f"supervisor.pyw not collected, got {names}"
    assert "app.py" in names
    assert len(SOURCES) > 10


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_source_compiles(path, tmp_path):
    """Parse it. Not a substitute for running it, but it would have caught
    the one that mattered."""
    try:
        py_compile.compile(str(path), cfile=str(tmp_path / "out.pyc"),
                           doraise=True)
    except py_compile.PyCompileError as exc:
        pytest.fail(f"{path.name} does not compile:\n{exc}")

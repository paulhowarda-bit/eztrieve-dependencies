"""This package must never depend on the COBOL or JCL ones.

The three are peers: the COBOL tools say what a program does, the JCL one says which
dataset a ddname is, and this one says which BYTES of one record become which bytes of
another. They meet at plain dicts. Nothing about the source layout enforces that - a single
stray import would erase it while every other test still passed, and the cost is not
abstract: an Easytrieve box would start carrying a COBOL modelling engine (``cobol_xstate``)
or a JCL parser (``jcl_dependencies``) it never executes, and this repository could no
longer be released on its own. An artifacts+eztrieve install must be unable to find any of
them.

A note on how, because getting it wrong is easy and silent: ``sys.meta_path`` finders are
consulted through ``find_spec``. ``find_module`` was REMOVED in Python 3.12, so a blocker
that only defines it is ignored entirely and every test here passes vacuously.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from _mainframe_common import CHECKOUT

SRC = Path(__file__).resolve().parents[1] / "src"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
# The child interpreters cannot inherit conftest's sys.path insertion, so they get the same
# trees explicitly: this repo's src plus the sibling checkout's mainframe-artifacts (a
# nonexistent path is inert - the pip-installed distribution carries the run then).
_TREES = (str(CHECKOUT / "mainframe-artifacts" / "src"), str(SRC))

_BLOCKED = ("cobol_xstate", "cobol_parser", "jcl_dependencies")

_PREAMBLE = textwrap.dedent("""
    import sys
    for _tree in %r:
        sys.path.insert(0, _tree)

    class Blocker:
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in %r:
                raise ImportError("BLOCKED " + name)
            return None

    sys.meta_path.insert(0, Blocker())
""")


def _isolated(body):
    """A fresh interpreter: blocking a module already in sys.modules does nothing."""
    return subprocess.run([sys.executable, "-c",
                           _PREAMBLE % (_TREES, _BLOCKED) + textwrap.dedent(body)],
                          capture_output=True, text=True)


@pytest.mark.parametrize("package", _BLOCKED)
def test_the_blocker_actually_blocks(package):
    """Guard the guard. If this passes when it should not, everything below is vacuous."""
    proc = _isolated("import {0}".format(package))
    assert proc.returncode != 0
    assert "BLOCKED {0}".format(package) in proc.stderr


def test_the_package_works_with_the_sibling_packages_unavailable():
    proc = _isolated("""
        from eztrieve_dependencies import (parse_eztrieve, build_eztrieve_lineage,
                                           build_eztrieve_artifacts)
        from eztrieve_dependencies.api import analyze
        a = analyze("FILE F FB(80 800)\\n"
                    "  A 1 5 N\\n"
                    "FILE G FB(80 800)\\n"
                    "  B 1 5 N\\n"
                    "JOB INPUT F\\n"
                    "  B = A\\n"
                    "  PUT G\\n", retrieve=False)
        assert len(a.program.activities) == 1
        flow = a.lineage()["fieldFlow"]
        assert flow[0]["origins"][0]["originKey"] == "F:A"
        assert a.artifacts()["artifacts"]
        print("OK")
    """)
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


def test_the_cli_works_with_the_sibling_packages_unavailable(tmp_path):
    proc = _isolated("""
        import io, contextlib, os
        from eztrieve_dependencies.cli import run
        out = {out!r}
        with contextlib.redirect_stderr(io.StringIO()):
            rc = run([{src!r}, "--outdir", out, "-q"])
        assert rc == 0, rc
        print("FILES", len([f for f in os.listdir(out) if f.endswith(".json")]))
    """.format(out=str(tmp_path / "o"),
               src=str(SRC.parent / "examples" / "payroll.ezt")))
    assert proc.returncode == 0, proc.stderr
    assert "FILES 4" in proc.stdout       # both views + both retrieval reports


def test_the_jcl_join_takes_a_dict_and_needs_no_jcl_types():
    """bind_jcl_ddnames is the one function that serves a JCL feature. It consumes a plain
    lineage dict, which is precisely what keeps this dependency from existing."""
    proc = _isolated("""
        import json, pathlib
        from eztrieve_dependencies import bind_jcl_ddnames, parse_eztrieve
        from eztrieve_dependencies.views import build_eztrieve_artifacts
        lineage = json.loads(pathlib.Path({fixture!r}).read_text(encoding="utf-8"))
        program = parse_eztrieve(
            pathlib.Path({src!r}).read_text(encoding="utf-8"),
            source_name="payroll.ezt")
        out = bind_jcl_ddnames(build_eztrieve_artifacts(program), lineage)
        row = next(a for a in out["artifacts"] if a.get("ddname") == "PERSNL")
        print("DATASET", row["dataset"])
    """.format(fixture=str(FIXTURES / "payroll.jcl.lineage.json"),
               src=str(SRC.parent / "examples" / "payroll.ezt")))
    assert proc.returncode == 0, proc.stderr
    assert "DATASET PROD.HR.PERSNL.MASTER" in proc.stdout


@pytest.mark.parametrize("module", ["lexer", "model", "macros", "parser", "lineage",
                                    "views", "detect", "prefetch", "api", "cli",
                                    "__init__"])
def test_no_module_imports_a_sibling_package(module):
    """Read the source too: an import inside a rarely-taken branch would not show up in a
    passing import test."""
    src = (SRC / "eztrieve_dependencies" / "{0}.py".format(module)).read_text(
        encoding="utf-8")
    for line in src.splitlines():
        s = line.strip()
        if s.startswith(("import ", "from ")):
            for blocked in _BLOCKED:
                assert ("{0}.".format(blocked) not in s
                        and s != "import {0}".format(blocked)
                        and not s.startswith("from {0} ".format(blocked))), (
                    "eztrieve_dependencies/{0}.py imports {1}: {2}".format(
                        module, blocked, s))

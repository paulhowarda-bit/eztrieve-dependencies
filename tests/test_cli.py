"""The eztrieve-dependencies command line."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # so --fetcher fakes.… loads

from eztrieve_dependencies.cli import run                            # noqa: E402

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAKE = "fakes.estate:fetch_artifact"


def test_it_writes_both_views_and_both_reports(tmp_path):
    out = tmp_path / "o"
    assert run([str(EXAMPLES / "payroll.ezt"), "--outdir", str(out),
                "--fetcher", FAKE, "--jobs", "1", "-q"]) == 0
    assert {p.name.split(".", 1)[1] for p in out.glob("*.json")} == {
        "ezt.artifacts.json", "ezt.lineage.json",
        "ezt.prefetch.json", "ezt.fetch.json"}


def test_target_selects_views_but_never_drops_the_retrieval_account(tmp_path):
    """The reports are not a view you can opt out of: what was retrieved decides whether
    the model is right."""
    for target, expected in [
        ("artifacts", {"ezt.artifacts.json", "ezt.prefetch.json", "ezt.fetch.json"}),
        ("lineage", {"ezt.lineage.json", "ezt.prefetch.json", "ezt.fetch.json"}),
    ]:
        out = tmp_path / target
        assert run([str(EXAMPLES / "payroll.ezt"), "--outdir", str(out),
                    "--target", target, "--fetcher", FAKE, "--jobs", "1", "-q"]) == 0
        assert {p.name.split(".", 1)[1] for p in out.glob("*.json")} == expected


def test_a_jcl_source_is_flagged_rather_than_parsed_as_a_program(tmp_path, capsys):
    run([str(EXAMPLES / "payroll.jcl"), "--outdir", str(tmp_path / "o"),
         "--no-fetch", "-q"])
    assert "looks like JCL" in capsys.readouterr().err


def test_a_cobol_source_is_flagged_too(tmp_path, capsys):
    src = tmp_path / "t.cbl"
    src.write_text("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. T.\n")
    run([str(src), "--outdir", str(tmp_path / "o"), "--no-fetch", "-q"])
    assert "looks like COBOL" in capsys.readouterr().err


def test_max_rounds_is_exposed_and_the_bound_is_reported(tmp_path):
    """The closure bound must stay visible when hit: a silently truncated closure looks
    exactly like a program with no more members."""
    out = tmp_path / "o"
    assert run([str(EXAMPLES / "macroed.ezt"), "--outdir", str(out),
                "--max-rounds", "1", "--fetcher", FAKE, "--jobs", "1", "-q",
                "-I", str(tmp_path)]) == 0
    pre = json.loads(next(out.glob("*.ezt.prefetch.json")).read_text())
    closure = [r for r in pre["members"] if r["member"] == "<closure>"]
    assert closure and "1 resolution rounds" in closure[0]["reason"]


def test_a_macro_beside_the_program_resolves_without_the_estate(tmp_path):
    out = tmp_path / "o"
    assert run([str(EXAMPLES / "macroed.ezt"), "--outdir", str(out),
                "--no-fetch", "-q"]) == 0
    pre = json.loads(next(out.glob("*.ezt.prefetch.json")).read_text())
    assert {r["member"]: r["status"] for r in pre["members"]} == {
        "CUSTREC": "local", "EDITCHK": "local"}
    lineage = json.loads(next(out.glob("*.ezt.lineage.json")).read_text())
    assert len(next(f for f in lineage["files"] if f["file"] == "EDITIN")["record"]) == 4


def test_bind_jcl_closes_the_ddnames_and_implies_the_artifacts_view(tmp_path):
    out = tmp_path / "o"
    assert run([str(EXAMPLES / "payroll.ezt"), "--outdir", str(out), "--no-fetch",
                "--target", "lineage",
                "--bind-jcl", str(FIXTURES / "payroll.jcl.lineage.json"), "-q"]) == 0
    art = json.loads(next(out.glob("*.ezt.artifacts.json")).read_text())
    assert art["jclBinding"]["steps"] == ["RUNPAY"]
    datasets = {a["artifact"]: a.get("dataset") for a in art["artifacts"]
                if a["kind"] == "file"}
    assert datasets == {"PERSNL": "PROD.HR.PERSNL.MASTER",
                        "PAYEXT": "PROD.FIN.PAY.EXTRACT"}


def test_bind_step_overrides_the_sysin_match(tmp_path):
    out = tmp_path / "o"
    assert run([str(EXAMPLES / "payroll.ezt"), "--outdir", str(out), "--no-fetch",
                "--bind-jcl", str(FIXTURES / "payroll.jcl.lineage.json"),
                "--bind-step", "RUNAUD", "-q"]) == 0
    art = json.loads(next(out.glob("*.ezt.artifacts.json")).read_text())
    row = next(a for a in art["artifacts"] if a["artifact"] == "PERSNL")
    assert row["dataset"] == "TEST.HR.PERSNL.SAMPLE"


def test_bind_jcl_refuses_the_wrong_view_with_a_message_not_a_traceback(tmp_path, capsys):
    wrong = tmp_path / "w.json"
    wrong.write_text(json.dumps({"format": "jcl-dependencies-artifacts"}))
    rc = run([str(EXAMPLES / "payroll.ezt"), "--outdir", str(tmp_path / "o"),
              "--no-fetch", "--bind-jcl", str(wrong), "-q"])
    assert rc == 1
    assert "not a jcl-dependencies LINEAGE view" in capsys.readouterr().err


def test_a_missing_source_is_an_exit_code_not_a_traceback(tmp_path, capsys):
    assert run([str(tmp_path / "nope.ezt"), "--outdir", str(tmp_path / "o")]) == 2
    assert "no such file" in capsys.readouterr().err


def _wide_program(tmp_path):
    """A member whose statements carry meaning past column 72 - which the compiler would
    not read, and which a checkout that is not in fixed 80-byte format may well have."""
    src = tmp_path / "wide.ezt"
    src.write_text("FILE F FB(200 2000)\n"
                   + "  A 1 5 N".ljust(72) + " HEADING ('ACCOUNT')\n"
                   + "JOB INPUT F\n  STOP\n")
    return src


def _first_record(out):
    lineage = json.loads(next(out.glob("*.ezt.lineage.json")).read_text())
    return lineage["files"][0]["record"][0]


def test_by_default_nothing_past_column_72_is_read_and_the_loss_is_reported(tmp_path):
    out = tmp_path / "o"
    assert run([str(_wide_program(tmp_path)), "--outdir", str(out), "--no-fetch",
                "-q"]) == 0
    assert "heading" not in _first_record(out)
    lineage = json.loads(next(out.glob("*.ezt.lineage.json")).read_text())
    assert any("past column 72" in f for f in lineage["flags"])


def test_the_right_margin_is_settable_for_a_member_that_is_not_80_bytes(tmp_path):
    out = tmp_path / "o"
    assert run([str(_wide_program(tmp_path)), "--outdir", str(out), "--no-fetch",
                "--right-margin", "0", "-q"]) == 0
    assert _first_record(out)["heading"] == ["'ACCOUNT'"]


def test_gather_then_replay_reproduces_the_views(tmp_path):
    bundle, live, offline = tmp_path / "b", tmp_path / "live", tmp_path / "off"
    program = str(EXAMPLES / "macroed.ezt")
    assert run([program, "--outdir", str(tmp_path / "g"), "--fetcher", FAKE,
                "--gather-only", str(bundle), "--jobs", "1", "-q"]) == 0
    assert run([program, "--outdir", str(live), "--fetcher", FAKE,
                "--jobs", "1", "-q"]) == 0
    # No --fetcher at all on the replay: the bundle is the service.
    assert run([program, "--outdir", str(offline), "--from-bundle", str(bundle),
                "--jobs", "1", "-q"]) == 0
    for name in ("macroed.ezt.artifacts.json", "macroed.ezt.lineage.json"):
        assert (offline / name).read_text() == (live / name).read_text()


def test_summary_names_the_file_flow(tmp_path, capsys):
    # No -q: the summary is INFO, and -q is exactly the flag that hides progress.
    run([str(EXAMPLES / "payroll.ezt"), "--outdir", str(tmp_path / "o"), "--no-fetch",
         "--summary"])
    err = capsys.readouterr().err
    assert "FLOW PERSNL -> PAYEXT (4 field(s))" in err
    assert "sink(s) traced to an input" in err


def test_python_dash_m_works():
    import os
    import subprocess
    # PREPEND to the inherited PYTHONPATH rather than replacing it, and hand the child the
    # sibling mainframe-artifacts tree the parent found the same way (conftest's sys.path
    # insertion does not survive into a subprocess; a nonexistent path is inert when the
    # distribution is pip-installed instead).
    from _mainframe_common import CHECKOUT
    inherited = os.environ.get("PYTHONPATH", "")
    pypath = os.pathsep.join(p for p in (
        str(REPO / "src"), str(CHECKOUT / "mainframe-artifacts" / "src"),
        inherited) if p)
    proc = subprocess.run(
        [sys.executable, "-m", "eztrieve_dependencies", "--help"],
        capture_output=True, text=True, cwd=str(REPO),
        env={**os.environ, "PYTHONPATH": pypath})
    assert proc.returncode == 0
    assert "eztrieve-dependencies" in proc.stdout

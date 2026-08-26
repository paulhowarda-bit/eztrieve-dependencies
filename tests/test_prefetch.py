"""Stage 1 for Easytrieve: the closure over %MACRO members.

The tests that matter are the ones that fail *silently* without prefetch - a record layout
that simply is not there - because silence is the whole problem: nothing raises, and the
output looks like a finished answer about a simpler program than the one that runs.
"""

from eztrieve_dependencies.lineage import build_graph
from eztrieve_dependencies.parser import parse_eztrieve
from eztrieve_dependencies.prefetch import prefetch_eztrieve
from eztrieve_dependencies.views import build_eztrieve_artifacts, build_eztrieve_lineage

from fakes.estate import fetch_artifact  # noqa: F401

# A program whose record layout and edit rules both live in macros. Everything real about
# it is somewhere else.
PROGRAM = (
    "FILE EDITIN FB(200 2000)\n"
    "%CUSTREC 1\n"
    "DEFINE WS-ERRORS W 2 N 0\n"
    "JOB INPUT EDITIN NAME EDITJOB\n"
    "  WS-ERRORS = 0\n"
    "%CUSTEDIT\n"
)


def _row(result, member):
    return next(r for r in result.rows if r["member"] == member)


def test_a_record_layout_inside_a_macro_appears_only_after_prefetch():
    """Parsed without CUSTREC, EDITIN has NO FIELDS - so nothing that references them
    resolves, the lineage is empty, and none of that raises."""
    blind = parse_eztrieve(PROGRAM, resolver=None, source_name="p.ezt")
    assert not blind.files["EDITIN"].fields
    blind_lineage = build_eztrieve_lineage(blind)
    # Not a crash and not an error - just a program whose input file touches nothing.
    assert not _mentions(blind_lineage, "EDITIN")
    assert any("macro CUSTREC" in f for f in blind_lineage["flags"])

    pre = prefetch_eztrieve(PROGRAM, fetch_artifact, source_name="p.ezt")
    seeing = parse_eztrieve(PROGRAM, resolver=pre.resolver(), source_name="p.ezt")
    assert [f.name for f in seeing.files["EDITIN"].fields] == [
        "CU-NUMBER", "CU-NAME", "CU-STATE", "CU-BALANCE"]
    assert _mentions(build_eztrieve_lineage(seeing), "EDITIN")


def _mentions(lineage, file_name):
    """Whether any field edge reads, writes or is gated by a field of ``file_name``."""
    prefix = file_name + ":"
    for edge in lineage["fieldLineage"]:
        rows = [edge["target"]] + list(edge.get("sources") or [])
        if any(r.get("file") == file_name for r in rows):
            return True
        for cond in edge.get("conditions") or []:
            if any(str(f).startswith(prefix) for f in cond.get("fields") or []):
                return True
    return False


def test_the_closure_reaches_a_macro_named_inside_another_macro():
    """STDLIMIT is invoked by CUSTEDIT - so it cannot be discovered until CUSTEDIT has been
    retrieved. A single-pass scan of the program text finds neither."""
    log = []

    def logging_fetch(name, type=None, copy=None):     # noqa: A002
        log.append(str(name))
        return fetch_artifact(name, type=type, copy=copy)

    pre = prefetch_eztrieve(PROGRAM, logging_fetch, source_name="p.ezt")
    # In that order, necessarily: one round per level of nesting.
    assert log == ["CUSTREC", "CUSTEDIT", "STDLIMIT"]
    assert _row(pre, "STDLIMIT")["status"] == "fetched"
    # ...and it converged rather than hitting the bound, so no <closure> row was filed.
    assert not [r for r in pre.rows if r["member"] == "<closure>"]


def test_the_nested_macros_statements_reach_the_field_lineage():
    """The point of the closure, stated as an outcome rather than a member count: an edit
    two macros deep is what gates the output, and without it the gate is invisible."""
    pre = prefetch_eztrieve(PROGRAM, fetch_artifact, source_name="p.ezt")
    program = parse_eztrieve(PROGRAM, resolver=pre.resolver(), source_name="p.ezt")
    tests = {c["test"] for e in build_graph(program).edges for c in e.conditions}
    assert "CU-NAME = ' '" in tests            # from CUSTEDIT
    assert "CU-BALANCE LT 0" in tests          # from STDLIMIT, one level deeper


def test_a_macro_the_estate_does_not_have_is_not_found_not_an_error():
    pre = prefetch_eztrieve("FILE F FB(80 800)\n%NOSUCH\n", fetch_artifact,
                            source_name="p.ezt")
    row = _row(pre, "NOSUCH")
    assert row["status"] == "not-found"
    assert "asked and had nothing" in row["reason"]


def test_a_failed_request_is_an_error_row_never_an_absence():
    """The one invariant of the estate contract: raising means THE REQUEST FAILED
    (fixable); returning found:False means the estate was asked and had nothing. A client
    that blurs them makes a whole estate read as empty."""
    pre = prefetch_eztrieve("FILE F FB(80 800)\n%BOOM\n", fetch_artifact,
                            source_name="p.ezt")
    row = _row(pre, "BOOM")
    assert row["status"] == "error"
    assert "share unreachable" in row["error"]
    assert "NOT evidence the member is absent" in row["reason"]


def test_a_closure_deeper_than_the_bound_says_so_instead_of_looking_complete():
    """Silently stopping would look exactly like a program that had no more members."""
    def fetcher(name, type=None, copy=None):        # noqa: A002
        key = str(name).upper().strip("'\"")
        return "MACRO\n%{0}X\n".format(key[:7])     # each macro invokes the next, forever

    pre = prefetch_eztrieve("FILE F FB(80 800)\n%OUTER\n", fetcher, source_name="p.ezt",
                            max_rounds=3)
    closure = [r for r in pre.rows if r["member"] == "<closure>"]
    assert len(closure) == 1
    assert closure[0]["status"] == "skipped"
    assert "3 resolution rounds" in closure[0]["reason"]


def test_a_macro_on_the_local_search_path_costs_no_round_trip(tmp_path):
    (tmp_path / "LOCALMAC.mac").write_text("MACRO\n  A 1 5 N\n", encoding="utf-8")
    asked = []

    def fetcher(name, type=None, copy=None):        # noqa: A002
        asked.append(str(name))
        return {"found": False}

    pre = prefetch_eztrieve("FILE F FB(80 800)\n%LOCALMAC\n", fetcher,
                            paths=[str(tmp_path)], source_name="p.ezt")
    assert asked == []                              # the estate was never troubled
    assert _row(pre, "LOCALMAC")["status"] == "local"


def test_the_unresolved_macro_is_still_a_manifest_row_so_the_gap_is_visible():
    program = parse_eztrieve(PROGRAM, resolver=None, source_name="p.ezt")
    row = next(a for a in build_eztrieve_artifacts(program)["artifacts"]
               if a["artifact"] == "CUSTREC")
    assert row["kind"] == "macro" and row["status"] == "unresolved"
    assert "a record layout or a whole activity can live inside it" in row["needs"]

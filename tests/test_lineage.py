"""Field-level lineage: the edges, the end-to-end flow, and what is deliberately not
traversed.

The tests that matter here are the ones that fail *quietly* without them - a chain that
stops one hop short, a control dependency reported as no dependency at all, an overlap
traversed until every field depends on every other. Each of those produces output that is
shorter, or larger, than the truth while still looking finished.
"""

from pathlib import Path

from eztrieve_dependencies.lineage import build_graph
from eztrieve_dependencies.parser import parse_eztrieve
from eztrieve_dependencies.views import build_eztrieve_lineage

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
MACROS = {p.stem.upper(): p.read_text(encoding="utf-8")
          for p in EXAMPLES.glob("*.mac")}


def _lineage(name: str, resolver=None):
    program = parse_eztrieve((EXAMPLES / name).read_text(encoding="utf-8"),
                             resolver=resolver, source_name=name)
    return build_eztrieve_lineage(program)


def _sink(lineage, key):
    return next(r for r in lineage["fieldFlow"] if r["sinkKey"] == key)


def _origins(lineage, key):
    return {o["originKey"] for o in _sink(lineage, key)["origins"]}


def _edge(lineage, target_key):
    return next(e for e in lineage["fieldLineage"] if e["targetKey"] == target_key)


# --------------------------------------------------------------------------- #
# the headline: input bytes -> output bytes
# --------------------------------------------------------------------------- #

def test_an_output_fields_bytes_trace_back_to_an_input_fields_bytes():
    """The whole point. PERSNL bytes 100-103 become PAYEXT bytes 25-30, through a
    working-storage field, multiplied by 12 - and no single statement says so."""
    lineage = _lineage("payroll.ezt")
    row = _sink(lineage, "PAYEXT:PX-ANNUAL")
    assert row["sink"]["bytes"] == "25-30"
    origin = row["origins"][0]
    assert origin["originKey"] == "PERSNL:GROSS"
    assert origin["origin"]["bytes"] == "100-103"
    assert [h["via"] for h in origin["path"]] == ["assignment", "assignment"]
    assert origin["path"][0]["operators"] == ["*"]
    assert origin["constants"] == ["12"]


def test_the_path_carries_the_condition_that_gates_it():
    origin = _sink(_lineage("payroll.ezt"), "PAYEXT:PX-ANNUAL")["origins"][0]
    assert origin["influencedBy"] == ["PERSNL:STATUS"]
    assert origin["path"][0]["conditions"][0]["test"] == "STATUS = 'A'"


def test_a_field_built_only_from_literals_still_reports_what_decides_it():
    """PX-GRADE's VALUE comes from no field - it is always 'H' or 'L'. Which one is decided
    by ANNUAL-PAY, and therefore by GROSS. Reporting "no origin" would be true about the
    value and false about the dependency, which is the thing a reader is asking about."""
    row = _sink(_lineage("payroll.ezt"), "PAYEXT:PX-GRADE")
    assert row["origins"] == []
    assert sorted(row["constants"]) == ["H", "L"]
    influences = {i["field"]: i for i in row["influencedBy"]}
    assert set(influences) == {"STATUS", "ANNUAL-PAY"}
    # ...and the gate is itself traced back to the input it came from.
    assert [t["field"] for t in influences["ANNUAL-PAY"]["tracesTo"]] == ["GROSS"]
    assert "flags" not in row


def test_a_sort_work_file_is_traversed_rather_than_treated_as_a_boundary():
    """SALESRT is both written (by the SORT) and read (by the report). Stopping there would
    end every chain one hop short of the input it actually came from."""
    lineage = _lineage("sortrpt.ezt")
    origin = _sink(lineage, "@SALESRPT.SUM.SL-AMOUNT")["origins"][0]
    assert origin["originKey"] == "SALESIN:SL-AMOUNT"
    assert [h["via"] for h in origin["path"]] == ["sort-copy", "report-sum"]


def test_move_like_pairs_fields_by_name_and_says_so():
    lineage = _lineage("macroed.ezt", resolver=MACROS.get)
    edge = _edge(lineage, "EDITGOOD:CU-BALANCE")
    assert edge["via"] == "move-like"
    assert edge["sources"][0]["file"] == "EDITIN"
    assert "by NAME, not by position" in edge["note"]


def test_a_table_lookup_separates_the_value_from_the_row_that_was_chosen():
    lineage = _lineage("custupd.ezt")
    edge = _edge(lineage, "WS-DESC")
    assert edge["via"] == "table-lookup"
    sources = {s["field"] for s in edge["sources"]}
    assert sources == {"CT-DESC", "TR-CODE"}
    assert "argument decides which row" in edge["note"]


def test_point_records_which_record_is_read_not_what_the_value_is_made_of():
    lineage = _lineage("custupd.ezt")
    edge = next(e for e in lineage["fieldLineage"]
                if e["via"] == "positioned-by" and e["targetKey"] == "CUSTMAST:CM-BAL")
    assert [s["field"] for s in edge["sources"]] == ["TR-CUST"]
    assert "selects WHICH record" in edge["note"]


def test_an_accumulator_is_marked_and_its_chain_is_not_unrolled():
    lineage = _lineage("payroll.ezt")
    edge = _edge(lineage, "PAY-COUNT")
    assert edge["accumulates"] is True
    display = next(r for r in lineage["fieldFlow"] if r["sinkKey"].startswith("@DISPLAY"))
    path = display["origins"][0]
    assert path["cyclic"] is True
    # ...and the statement appears ONCE, not twice: a self-source is on-path immediately.
    assert [h["line"] for h in path["path"]].count(edge["line"]) == 1


# --------------------------------------------------------------------------- #
# what is deliberately not traversed
# --------------------------------------------------------------------------- #

def test_positional_overlaps_are_listed_but_never_traversed():
    """A group and its elements overlap by design. Traversing that would make every field
    of a record depend on every other - true, and useless."""
    program = parse_eztrieve(
        "FILE F FB(80 800)\n"
        "  FULL-DATE 1 8 N\n"
        "  YEAR      1 4 N\n"
        "  MONTH     5 2 N\n", source_name="f.ezt")
    lineage = build_eztrieve_lineage(program)
    record = {f["field"]: f for f in lineage["files"][0]["record"]}
    assert record["YEAR"]["overlaps"] == ["FULL-DATE"]
    assert sorted(record["FULL-DATE"]["overlaps"]) == ["MONTH", "YEAR"]
    assert not lineage["fieldLineage"]                 # nothing was made an edge
    assert any("NOT traversed as lineage" in f for f in lineage["flags"])


def test_an_explicit_redefinition_is_an_alias_and_is_traversed_both_ways():
    """A declaration whose location names another field is a stated intent to alias, which
    a positional overlap is not."""
    program = parse_eztrieve(
        "FILE F FB(80 800)\n"
        "  FULL-DATE 1 8 N\n"
        "  YEAR FULL-DATE 4 N\n", source_name="f.ezt")
    lineage = build_eztrieve_lineage(program)
    pairs = {(e["targetKey"], e["sources"][0]["field"])
             for e in lineage["fieldLineage"] if e["via"] == "redefine-alias"}
    assert pairs == {("F:YEAR", "FULL-DATE"), ("F:FULL-DATE", "YEAR")}


def test_a_call_argument_is_marked_as_possibly_changed_rather_than_assumed_intact():
    lineage = _lineage("custupd.ezt")
    call = lineage["calls"][0]
    assert call["program"] == "AUDITLOG"
    assert [a["field"] for a in call["arguments"]] == ["TR-CUST", "TR-CODE", "TR-AMT"]
    graph = build_graph(parse_eztrieve(
        (EXAMPLES / "custupd.ezt").read_text(encoding="utf-8"), source_name="custupd.ezt"))
    assert any("may change it" in r for r in graph.opaque["TRANS:TR-AMT"])


# --------------------------------------------------------------------------- #
# names, scope and honesty about what could not be resolved
# --------------------------------------------------------------------------- #

def test_a_duplicate_name_is_read_against_the_activitys_own_record_and_says_so():
    """A sort work file declared with COPY carries every field name the original has, so
    every unqualified reference is ambiguous by the letter of the rule. Reading it against
    the record the activity works on is what Easytrieve does - and recording that it was
    needed is what keeps it from being a silent guess."""
    lineage = _lineage("sortrpt.ezt")
    assert _origins(lineage, "@SALESRPT.LINE1.1") == {"SALESIN:SL-REGION"}
    assert any("declared in more than one file" in f and "SALESRT" in f
               for f in lineage["flags"])
    assert not lineage["unresolved"]


def test_an_unresolvable_reference_is_reported_with_its_reason():
    program = parse_eztrieve(
        "FILE F FB(80 800)\n  A 1 5 N\n"
        "JOB INPUT F\n  A = NOSUCHFIELD\n", source_name="f.ezt")
    lineage = build_eztrieve_lineage(program)
    row = lineage["unresolved"][0]
    assert row["reference"] == "NOSUCHFIELD"
    assert row["reason"] == "no field of this name is declared"
    assert row["line"] == 4


def test_a_qualified_reference_settles_an_otherwise_ambiguous_name():
    program = parse_eztrieve(
        "FILE A FB(80 800)\n  X 1 5 N\n"
        "FILE B FB(80 800)\n  X 1 5 N\n"
        "JOB INPUT A\n  B:X = A:X\n  PUT B\n", source_name="f.ezt")
    lineage = build_eztrieve_lineage(program)
    assert _origins(lineage, "B:X") == {"A:X"}
    assert not lineage["unresolved"]
    # No scope was needed - the qualification settled it on its own.
    assert not [f for f in lineage["flags"] if "declared in more than one file" in f]


# --------------------------------------------------------------------------- #
# file-level aggregation
# --------------------------------------------------------------------------- #

def test_file_direction_comes_from_how_the_program_actually_uses_the_file():
    files = {f["file"]: f["io"] for f in _lineage("custupd.ezt")["files"]}
    assert files == {"TRANS": "read", "CUSTMAST": "read-write", "ERRLOG": "write",
                     "CODETAB": "read"}


def test_the_field_edges_add_up_to_a_file_level_dataflow():
    flow = {(e["from"], e["to"]): e["fields"] for e in _lineage("custupd.ezt")["fileFlow"]}
    assert flow[("TRANS", "CUSTMAST")] >= 2
    assert ("CODETAB", "ERRLOG") in flow


def test_a_declared_but_untouched_file_keeps_its_row_with_the_direction_unknown():
    """The ddname still has to be allocated, so the file is a dependency either way - but
    inventing a direction for it would not be."""
    program = parse_eztrieve(
        "FILE USED FB(80 800)\n  A 1 5 N\n"
        "FILE SPARE FB(80 800)\n  B 1 5 N\n"
        "JOB INPUT USED\n  STOP\n", source_name="f.ezt")
    files = {f["file"]: f["io"] for f in build_eztrieve_lineage(program)["files"]}
    assert files == {"USED": "read", "SPARE": "unknown"}

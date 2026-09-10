"""The reverse direction: what depends on what this program provides.

An Easytrieve program says which bytes of which record become which bytes of which other
record. It cannot say which job runs it, or which downstream work reads the ddname it
writes - those live in an estate index only the host can read - so the answer arrives
through a door and is reported as given.
"""

import json
from pathlib import Path

from mainframe_artifacts.dependents import DependentsLookup

from eztrieve_dependencies.api import analyze
from eztrieve_dependencies.parser import parse_eztrieve
from eztrieve_dependencies.views import FORMAT_DEPENDENTS, build_eztrieve_dependents

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
SOURCE = (EXAMPLES / "payroll.ezt").read_text(encoding="utf-8")

#: What a host index holds: a job runs PAYROLL, and one job reads what it writes.
_INDEX = {
    "PAYROLL|program": [
        {"name": "PAYRUN", "kind": "JOB", "via": "EXEC PGM=EZTPA00 SYSIN",
         "match_strength": "qualified", "detail": "step EZT names PAYROLL as SYSIN"},
    ],
    "PAYEXT|file": [
        {"name": "PAYPOST", "kind": "JOB", "via": "DD DSN=... DISP=SHR",
         "match_strength": "bare"},
    ],
}


def _view(mapping=None, resolver=None):
    program = parse_eztrieve(SOURCE, source_name="payroll.ezt")
    return build_eztrieve_dependents(program, DependentsLookup(mapping, resolver))


def _row(view, name):
    return next(r for r in view["provides"] if r["name"] == name)


# --- what is asked about --------------------------------------------------------------

def test_the_program_and_the_ddnames_it_writes_are_what_is_asked_about():
    asked = []

    def index(name, kind=None):
        asked.append((name, kind))
        return []

    _view(resolver=index)
    # PERSNL is READ, so it is not asked about: that is what this program depends on.
    assert asked == [("PAYROLL", "program"), ("PAYEXT", "file")]


def test_dependents_attach_to_the_name_that_was_asked_about():
    view = _view(_INDEX)
    assert [d["name"] for d in _row(view, "PAYROLL")["dependents"]] == ["PAYRUN"]
    assert [d["name"] for d in _row(view, "PAYEXT")["dependents"]] == ["PAYPOST"]
    assert _row(view, "PAYEXT")["provides"] == "written as ddname PAYEXT"
    assert _row(view, "PAYROLL")["dependents"][0]["matchStrength"] == "qualified"


def test_the_view_has_the_family_keys_and_the_program_subject_key():
    view = _view(_INDEX)
    assert list(view) == ["format", "formatVersion", "program", "source", "note",
                          "suppliedBy", "provides", "unanswered", "flags"]
    assert (view["format"], view["program"]) == (FORMAT_DEPENDENTS, "PAYROLL")


def test_rows_are_sorted_so_the_hosts_order_cannot_change_the_bytes():
    two = {"PAYEXT|file": [{"name": "ZLAST", "kind": "JOB"},
                           {"name": "AFIRST", "kind": "JOB"}]}
    flipped = {"PAYEXT|file": list(reversed(two["PAYEXT|file"]))}
    assert json.dumps(_view(two)) == json.dumps(_view(flipped))


# --- the three answers ----------------------------------------------------------------

def test_no_lookup_supplied_is_no_view_at_all():
    program = parse_eztrieve(SOURCE, source_name="payroll.ezt")
    assert build_eztrieve_dependents(program, None) is None
    assert build_eztrieve_dependents(program, DependentsLookup()) is None


def test_a_name_the_index_does_not_cover_is_unanswered_not_empty():
    view = _view({"PAYROLL|program": _INDEX["PAYROLL|program"]})
    assert [r["name"] for r in view["provides"]] == ["PAYROLL"]
    assert view["unanswered"] == [
        {"name": "PAYEXT", "kind": "file",
         "reason": "the lookup does not cover this name"}]


def test_asked_and_nothing_depends_on_it_is_said_with_an_empty_list():
    view = _view(resolver=lambda name, kind=None: [])
    assert _row(view, "PAYEXT")["dependents"] == []
    assert view["unanswered"] == []


def test_a_lookup_that_breaks_is_flagged():
    def boom(name, kind=None):
        raise RuntimeError("index unreachable")

    view = _view(resolver=boom)
    assert view["provides"] == []
    assert "index unreachable" in view["flags"][0]


def test_a_reported_fan_out_cap_reaches_the_view():
    def capped(name, kind=None):
        return ({"rows": _INDEX["PAYEXT|file"], "truncated": True, "total": 77}
                if name == "PAYEXT" else None)

    row = _row(_view(resolver=capped), "PAYEXT")
    assert (row["truncated"], row["total"]) == (True, 77)


# --- through analyze() and the bundle -------------------------------------------------

def test_analyze_without_the_parameter_is_the_run_it_always_was():
    analysis = analyze(SOURCE, source_name="payroll.ezt", retrieve=False)
    assert analysis.dependents_lookup is None
    assert analysis.dependents() is None


def test_a_gathered_bundle_replays_the_reverse_direction(tmp_path):
    from mainframe_artifacts.bundle import open_bundle

    from eztrieve_dependencies.api import gather

    def index(name, kind=None):
        return _INDEX.get("{0}|{1}".format(name, kind))

    live = analyze(SOURCE, source_name="payroll.ezt", retrieve=False,
                   dependents_resolver=index).dependents()
    root = tmp_path / "bundle"
    gather(SOURCE, source_name="payroll.ezt", dest=str(root),
           dependents_resolver=index)

    bundle = open_bundle(root)
    assert bundle.has_dependents()
    replayed = analyze(bundle.source(), source_name="payroll.ezt",
                       bundle=bundle).dependents()
    assert replayed == live

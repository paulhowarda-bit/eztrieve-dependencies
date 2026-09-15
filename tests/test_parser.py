"""Parsing: record layouts, activities, conditions, and macro expansion."""

from pathlib import Path

from eztrieve_dependencies.model import STORAGE_RESET, STORAGE_STATIC
from eztrieve_dependencies.parser import default_program_name, parse_eztrieve

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
MACROS = {p.stem.upper(): p.read_text(encoding="utf-8")
          for p in EXAMPLES.glob("*.mac")}


def _program(name: str, resolver=None):
    return parse_eztrieve((EXAMPLES / name).read_text(encoding="utf-8"),
                          resolver=resolver, source_name=name)


def _field(program, file_name, field_name):
    return program.files[file_name].field_named(field_name)


# --------------------------------------------------------------------------- #
# identity and record layouts
# --------------------------------------------------------------------------- #

def test_the_program_is_identified_by_its_member_name():
    """Easytrieve has no PROGRAM-ID. The member IS the identity, and it is what a JCL
    step's SYSIN names - so inventing anything else would break the join."""
    assert default_program_name("payroll.ezt") == "PAYROLL"
    assert default_program_name("/a/b/PAYROLL") == "PAYROLL"
    assert _program("payroll.ezt").name == "PAYROLL"


def test_a_field_declaration_carries_its_bytes_format_and_decimals():
    """The whole basis of this tool: Easytrieve states the byte range outright, so there is
    no PIC chain to unwind before an edge has two concrete ends."""
    gross = _field(_program("payroll.ezt"), "PERSNL", "GROSS")
    assert (gross.start, gross.length, gross.end) == (100, 4, 103)
    assert (gross.fmt, gross.decimals) == ("P", 2)
    assert gross.bytes_text == "100-103"
    assert gross.mask == "'ZZZ,ZZ9.99'"


def test_a_file_carries_its_record_format_and_lengths():
    persnl = _program("payroll.ezt").files["PERSNL"]
    assert (persnl.recfm, persnl.record_length, persnl.block_size) == ("FB", 150, 1800)


def test_star_means_the_next_available_byte():
    program = parse_eztrieve(
        "FILE F FB(100 1000)\n"
        "  A 1 9 N\n"
        "  B * 30 A\n"
        "  C * 2 A\n", source_name="f.ezt")
    assert [(f.name, f.start, f.end) for f in program.files["F"].fields] == [
        ("A", 1, 9), ("B", 10, 39), ("C", 40, 41)]


def test_working_storage_keeps_its_two_kinds_apart():
    """`W` is retained across activities; `S` is reinitialised before each one. A value
    that cannot survive an activity boundary is a different fact from one that can."""
    program = parse_eztrieve("DEFINE A W 4 N 0\nDEFINE B S 4 N 0\n", source_name="f.ezt")
    kinds = {f.name: f.storage for f in program.working}
    assert kinds == {"A": STORAGE_STATIC, "B": STORAGE_RESET}
    assert all(f.start is None for f in program.working)   # no record to be positioned in


def test_a_redefinition_takes_the_position_of_the_field_it_names():
    program = parse_eztrieve(
        "FILE F FB(100 1000)\n"
        "  FULL-DATE 1 8 N\n"
        "  YEAR FULL-DATE 4 N\n", source_name="f.ezt")
    year = _field(program, "F", "YEAR")
    assert year.redefines == "FULL-DATE" and (year.start, year.end) == (1, 4)


def test_a_file_that_copies_itself_is_refused_rather_than_looped():
    """The loop appends to the very list it iterates, so a self-COPY does not raise - it
    simply never returns. Found by mutation-fuzzing the shipped examples."""
    program = parse_eztrieve("FILE F FB(80 800)\n  A 1 5 N\n  COPY F\n",
                             source_name="f.ezt")
    assert any("COPY names the file it is inside" in f for f in program.flags)
    assert [f.name for f in program.files["F"].fields] == ["A"]


def test_copy_takes_another_files_layout_at_the_same_positions():
    program = _program("sortrpt.ezt")
    assert program.files["SALESRT"].copied_from == "SALESIN"
    src = {(f.name, f.start, f.length) for f in program.files["SALESIN"].fields}
    dst = {(f.name, f.start, f.length) for f in program.files["SALESRT"].fields}
    assert src == dst


def test_file_attributes_are_recognised_and_the_rest_is_kept():
    program = _program("custupd.ezt")
    assert program.files["CUSTMAST"].organization == "VSAM"
    assert program.files["CUSTMAST"].usage == ["UPDATE"]
    assert program.files["ERRLOG"].device == "PRINTER"
    assert program.files["TRANS"].exit_program == "TRANEDIT"
    assert program.files["CODETAB"].instream is True
    assert [e.text.strip() for e in program.files["CODETAB"].table_data] == [
        "'D' DEPOSIT", "'W' WITHDRAWAL"]


# --------------------------------------------------------------------------- #
# activities
# --------------------------------------------------------------------------- #

def test_job_sort_and_report_are_all_activities():
    kinds = [(a.kind, a.name) for a in _program("sortrpt.ezt").activities]
    assert kinds == [("SORT", "SORTSALE"), ("JOB", "RPTJOB"), ("REPORT", "SALESRPT")]


def test_a_job_header_carries_its_input_and_its_procs():
    job = _program("payroll.ezt").activities[0]
    assert job.inputs == [{"file": "PERSNL"}]
    assert job.name == "MAINJOB" and job.finish_proc == "WRAPUP"


def test_a_sort_header_carries_its_output_and_its_keys():
    sort = _program("sortrpt.ezt").activities[0]
    assert sort.inputs == [{"file": "SALESIN"}] and sort.sort_to == "SALESRT"
    assert [k["field"] for k in sort.sort_keys] == ["SL-REGION", "SL-STORE", "SL-SKU"]


def test_a_report_carries_its_sequence_control_sums_and_lines():
    rpt = _program("payroll.ezt").activities[-1].report
    assert [s["field"] for s in rpt.sequence] == ["DEPARTMENT", "NAME"]
    assert rpt.control == [{"field": "DEPARTMENT", "options": ["NEWPAGE"]}]
    assert rpt.sums == ["ANNUAL-PAY"]
    assert rpt.headings == {"NAME": ["'EMPLOYEE'", "'NAME'"]}
    assert [i["field"] for i in rpt.lines[0].items if i["kind"] == "field"] == [
        "DEPARTMENT", "NAME", "ANNUAL-PAY", "GRADE"]


def test_a_procedure_owns_the_statements_after_its_label():
    program = _program("payroll.ezt")
    proc = next(p for p in program.activities[0].procs if p.name == "WRAPUP")
    assert [s.verb for s in proc.statements] == ["DISPLAY"]


# --------------------------------------------------------------------------- #
# conditions
# --------------------------------------------------------------------------- #

def _by_line(program):
    return {s.line: s for s in program.all_statements()}


def test_nested_ifs_conjoin_and_an_else_carries_the_negation():
    program = _program("payroll.ezt")
    grade_h = next(s for s in program.all_statements()
                   if s.text.startswith("GRADE = 'H'"))
    grade_l = next(s for s in program.all_statements()
                   if s.text.startswith("GRADE = 'L'"))
    assert [(c["kind"], c["test"], c["negated"]) for c in grade_h.conditions] == [
        ("IF", "STATUS = 'A'", False), ("IF", "ANNUAL-PAY GT 60000", False)]
    assert grade_l.conditions[-1] == {
        "kind": "ELSE", "test": "ANNUAL-PAY GT 60000", "negated": True,
        "fields": ["ANNUAL-PAY"], "afterFailing": ["ANNUAL-PAY GT 60000"]}
    # END-IF closes the scope: PUT is inside the outer IF only.
    put = next(s for s in program.all_statements() if s.verb == "PUT")
    assert [c["test"] for c in put.conditions] == ["STATUS = 'A'"]


def test_a_case_branch_reads_as_a_test_of_the_case_subject():
    program = _program("custupd.ezt")
    deposit = next(s for s in program.all_statements()
                   if s.text.startswith("WS-NEWBAL = CM-BAL +"))
    assert deposit.conditions[-1]["kind"] == "WHEN"
    assert deposit.conditions[-1]["test"] == "TR-CODE = 'D'"


def test_otherwise_says_which_branches_it_follows():
    program = _program("custupd.ezt")
    perform = next(s for s in program.all_statements() if s.verb == "PERFORM")
    cond = perform.conditions[-1]
    assert cond["kind"] == "OTHERWISE" and cond["negated"] is True
    assert cond["afterFailing"] == ["TR-CODE = 'D'", "TR-CODE = 'W'"]


def test_unbalanced_control_structures_are_flagged():
    program = parse_eztrieve(
        "FILE F FB(80 800)\n  A 1 5 N\n"
        "JOB INPUT F\n  IF A = 1\n     STOP\n", source_name="f.ezt")
    assert any("unterminated control structure" in f for f in program.flags)
    program2 = parse_eztrieve(
        "FILE F FB(80 800)\n  A 1 5 N\n"
        "JOB INPUT F\n  END-IF\n", source_name="f.ezt")
    assert any("END-IF with no matching opener" in f for f in program2.flags)


def test_a_verb_this_parser_does_not_model_is_recorded_and_flagged():
    """Silence here would be a hole in the lineage that reads as an absence of dataflow."""
    program = parse_eztrieve(
        "FILE F FB(80 800)\n  A 1 5 N\n"
        "JOB INPUT F\n  TRANSFORM A FROM SOMETHING\n", source_name="f.ezt")
    assert any("TRANSFORM" in f and "not modelled" in f for f in program.flags)
    assert [s.verb for s in program.all_statements()] == ["TRANSFORM"]


# --------------------------------------------------------------------------- #
# macros
# --------------------------------------------------------------------------- #

def test_a_macro_carries_a_record_layout_and_without_it_the_file_has_no_fields():
    """The reason stage 1 is not optional decoration: parsed blind, this program has two
    files with no fields and a lineage that is empty - and still looks finished."""
    blind = _program("macroed.ezt")
    assert all(not fd.fields for fd in blind.files.values())
    assert any("macro CUSTREC" in f for f in blind.flags)

    seeing = _program("macroed.ezt", resolver=MACROS.get)
    assert [f.name for f in seeing.files["EDITIN"].fields] == [
        "CU-NUMBER", "CU-NAME", "CU-STATE", "CU-BALANCE"]
    assert not seeing.flags


def test_a_macro_parameter_is_substituted_positionally():
    """`%CUSTREC 1` binds START=1, which positions the whole layout."""
    program = _program("macroed.ezt", resolver=MACROS.get)
    assert _field(program, "EDITIN", "CU-NUMBER").start == 1
    assert _field(program, "EDITIN", "CU-BALANCE").bytes_text == "42-46"


def test_a_field_declared_in_a_macro_says_which_macro():
    program = _program("macroed.ezt", resolver=MACROS.get)
    assert _field(program, "EDITIN", "CU-NAME").origin == "CUSTREC"


def test_an_unsupplied_parameter_is_left_visible_and_flagged_never_blanked():
    """A field silently declared at position '' is a layout that is WRONG rather than
    absent, and wrong is worse."""
    lib = {"REC": "MACRO 1 START\n  A &START 9 N\n"}
    program = parse_eztrieve("FILE F FB(80 800)\n%REC\n", resolver=lib.get,
                             source_name="f.ezt")
    assert any("&START was not supplied" in f for f in program.flags)
    field = _field(program, "F", "A")
    assert field.location_raw == "&START" and field.start is None


def test_a_recursive_macro_is_refused_rather_than_looped():
    lib = {"LOOP": "MACRO\n%LOOP\n"}
    program = parse_eztrieve("FILE F FB(80 800)\n%LOOP\n", resolver=lib.get,
                             source_name="f.ezt")
    assert any("recursive invocation" in f for f in program.flags)


def test_a_resolver_that_raises_does_not_crash_the_parse():
    def boom(name):
        raise RuntimeError("estate share unreachable")

    program = parse_eztrieve("FILE F FB(80 800)\n%REC\nDEFINE X W 4 N 0\n",
                             resolver=boom, source_name="f.ezt")
    assert any("resolver raised" in f for f in program.flags)
    assert [f.name for f in program.working] == ["X"]     # ...and the parse carried on


def test_every_external_member_is_asked_for_through_the_resolver():
    """One parse must funnel a macro and the macros it invokes through the ONE resolver
    call - that is what makes replaying the parse (prefetch) ask the right questions."""
    asked = []
    lib = {"OUTER": "MACRO\n%INNER\n", "INNER": "MACRO\n  A 1 5 N\n"}

    def recording(name):
        asked.append(name)
        return lib.get(str(name).upper())

    parse_eztrieve("FILE F FB(80 800)\n%OUTER\n", resolver=recording, source_name="f.ezt")
    # Sequence, not set: INNER cannot be known about until OUTER has come back.
    assert [a.upper() for a in asked] == ["OUTER", "INNER"]


# --------------------------------------------------------------------------- #
# report layout: the operands a column's position depends on, and line offsets
# --------------------------------------------------------------------------- #

def _report(header: str, *body: str):
    source = ("FILE INF FB(80 800)\n  A 1 2 A\n  B 3 2 A\n"
              "JOB INPUT INF NAME J1\n  PRINT R1\n"
              + header + "\n" + "".join("  " + line + "\n" for line in body))
    program = parse_eztrieve(source, source_name="layout.ezt")
    return next(a.report for a in program.activities if a.report)


def test_the_layout_operands_are_kept_exactly_as_coded():
    rpt = _report("REPORT R1 LINESIZE 100 SPACE 2 NOADJUST PAGESIZE 60", "LINE A B")
    assert rpt.layout == {"linesize": 100, "space": 2, "noadjust": True}


def test_spread_and_nospread_are_each_recorded_as_written():
    assert _report("REPORT R1 SPREAD", "LINE A").layout == {"spread": True}
    assert _report("REPORT R1 NOSPREAD", "LINE A").layout == {"nospread": True}


def test_an_uncoded_linesize_is_not_given_a_default():
    """LINESIZE defaults from the PRINTER file's record length or a site option - neither
    is in the program, so there is no value to state."""
    assert _report("REPORT R1 SUMMARY", "LINE A").layout == {}


def test_a_plus_offset_before_an_item_is_a_position_not_a_printed_literal():
    items = _report("REPORT R1", "LINE A +3 B").lines[0].items
    assert items == [{"kind": "field", "field": "A"},
                     {"kind": "position", "keyword": "offset", "value": "+3"},
                     {"kind": "field", "field": "B"}]


def test_a_minus_offset_before_an_item_is_a_position_too():
    items = _report("REPORT R1", "LINE A -1 'X'").lines[0].items
    assert items[1] == {"kind": "position", "keyword": "offset", "value": "-1"}
    assert items[2] == {"kind": "literal", "value": "'X'"}


def test_an_unsigned_number_on_a_line_is_still_a_numeric_literal():
    items = _report("REPORT R1", "LINE A 5").lines[0].items
    assert items[1] == {"kind": "literal", "value": "5"}


def test_a_leading_signed_number_is_an_offset_not_the_line_or_title_number():
    rpt = _report("REPORT R1", "TITLE -2 'T'", "LINE A", "LINE -2 B", "LINE 05 A")
    assert [ln.index for ln in rpt.lines] == [1, 2, 5]
    assert rpt.lines[1].items[0] == {"kind": "position", "keyword": "offset", "value": "-2"}
    assert rpt.titles[0]["index"] == 1
    assert rpt.titles[0]["items"][0]["value"] == "-2"


def test_a_trailing_signed_number_has_nothing_to_offset_and_is_left_alone():
    items = _report("REPORT R1", "LINE A -4").lines[0].items
    assert items[-1] == {"kind": "literal", "value": "-4"}

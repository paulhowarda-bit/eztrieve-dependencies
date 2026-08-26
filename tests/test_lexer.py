"""The three things that have to happen before a statement can be read at all.

Each of these corrupts the model SILENTLY when it goes wrong - a sequence number becomes a
field, a continued statement loses half its operands, a table's data becomes declarations -
so each has a test that fails loudly instead.
"""

from eztrieve_dependencies.lexer import (DATA_PREFIX, logical_lines,
                                         looks_like_field_definition, tokenize)


def _texts(source, **kw):
    lines, _flags = logical_lines(source, **kw)
    return [ln.text for ln in lines]


# --------------------------------------------------------------------------- #
# the right margin
# --------------------------------------------------------------------------- #

def test_the_sequence_number_area_is_not_part_of_the_statement():
    """Columns 73-80 are the sequence area and the compiler never sees them. Read as part
    of the line, `00010023` becomes a field named 00010023 in the middle of a layout."""
    line = "  EMP-NUMBER      1   5 N".ljust(72) + "PAYRL010"
    assert _texts(line) == ["EMP-NUMBER      1   5 N"]


def test_a_non_sequence_tail_past_the_margin_is_reported_not_silently_dropped():
    line = "  NAME 17 16 A".ljust(72) + "HEADING ('EMPLOYEE')"
    lines, flags = logical_lines(line)
    assert lines[0].text == "NAME 17 16 A"
    assert any("past column 72" in f for f in flags)


def test_the_margin_can_be_switched_off_for_a_member_that_is_not_80_bytes():
    line = "  NAME 17 16 A".ljust(72) + "HEADING ('EMPLOYEE')"
    lines, flags = logical_lines(line, margin=0)
    assert lines[0].text.endswith("HEADING ('EMPLOYEE')")
    assert not flags


def test_a_comment_is_never_reported_for_running_past_the_margin():
    """A comment is prose and routinely longer than 72 columns. Flagging every one of them
    would bury the flags that matter."""
    long_comment = "* " + "x" * 100
    lines, flags = logical_lines(long_comment)
    assert lines == [] and flags == []


# --------------------------------------------------------------------------- #
# continuation
# --------------------------------------------------------------------------- #

def test_a_minus_continues_with_no_separating_blank():
    """The `-` is the continuation MARK and is consumed, so a name may be split across the
    break - which is the only reason to use `-` rather than `+`."""
    assert _texts("  LINE DEPARTMENT NA-\nME AMOUNT\n") == ["LINE DEPARTMENT NAME AMOUNT"]


def test_a_plus_continues_with_one_blank():
    assert _texts("  LINE DEPT +\n  NAME AMOUNT\n") == ["LINE DEPT NAME AMOUNT"]


def test_a_trailing_hyphen_inside_an_open_literal_is_not_a_continuation():
    """`'A-` has an unclosed quote, so the hyphen is part of the text, not a join."""
    got = _texts("  TITLE 'END-\n")
    assert got == ["TITLE 'END-"]


# --------------------------------------------------------------------------- #
# instream table data
# --------------------------------------------------------------------------- #

_TABLE = (
    "FILE CODETAB TABLE INSTREAM\n"
    "  CT-CODE   1  1 A\n"
    "  CT-DESC   3 20 A\n"
    "'D' DEPOSIT\n"
    "'W' WITHDRAWAL\n"
    "ENDTABLE\n"
    "DEFINE WS-X W 4 N 0\n"
)


def test_a_tables_declarations_are_code_and_its_rows_are_data():
    """There is no marker between them: the declarations come first and the rows follow.
    Read the wrong way round, the table has no fields and its VALUES become field names."""
    got = _texts(_TABLE)
    assert got[:3] == ["FILE CODETAB TABLE INSTREAM",
                       "CT-CODE   1  1 A",
                       "CT-DESC   3 20 A"]
    assert got[3].startswith(DATA_PREFIX) and "DEPOSIT" in got[3]
    assert got[4].startswith(DATA_PREFIX) and "WITHDRAWAL" in got[4]
    assert got[5] == "ENDTABLE"
    assert got[6] == "DEFINE WS-X W 4 N 0"      # ...and the member carries on


def test_an_instream_table_with_no_rows_does_not_swallow_the_rest_of_the_member():
    got = _texts("FILE T TABLE INSTREAM\n  ARG 1 1 A\nJOB INPUT NULL\n  STOP\n")
    assert got[-2:] == ["JOB INPUT NULL", "STOP"]


def test_a_data_row_keeps_its_columns():
    """Table rows are POSITIONAL - stripping their leading blanks would move every value."""
    lines, _ = logical_lines("FILE T TABLE INSTREAM\n  ARG 1 1 A\n  'D' DEPOSIT\n")
    assert lines[-1].data == "  'D' DEPOSIT"


def test_field_definition_shape():
    assert looks_like_field_definition(tokenize("CU-NUMBER 1 9 N"))
    assert looks_like_field_definition(tokenize("CU-NAME * 30 A"))
    assert looks_like_field_definition(tokenize("WS-X W 4 N 0"))
    assert not looks_like_field_definition(tokenize("'D' DEPOSIT"))
    assert not looks_like_field_definition(tokenize("JOB INPUT PERSNL"))


# --------------------------------------------------------------------------- #
# tokens
# --------------------------------------------------------------------------- #

def test_a_hyphen_is_part_of_a_name_and_never_a_separator():
    """Splitting on `-` would turn every hyphenated field in the estate into a
    subtraction."""
    assert tokenize("TOTAL-PAY = GROSS * 12") == ["TOTAL-PAY", "=", "GROSS", "*", "12"]


def test_operators_split_even_without_blanks():
    assert tokenize("A=B+C") == ["A", "=", "B", "+", "C"]


def test_a_literal_stays_whole_including_its_doubled_quotes():
    assert tokenize("MSG = 'IT''S HERE'") == ["MSG", "=", "'IT''S HERE'"]


def test_a_typed_literal_keeps_its_prefix():
    assert tokenize("IF FLAG = X'0C'") == ["IF", "FLAG", "=", "X'0C'"]


def test_a_parenthesised_clause_tokenises_to_its_parts():
    assert tokenize("HEADING ('EMPLOYEE' 'NAME')") == [
        "HEADING", "(", "'EMPLOYEE'", "'NAME'", ")"]

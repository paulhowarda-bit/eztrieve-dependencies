"""Easytrieve source text -> logical statements and tokens.

Three things have to happen before a single Easytrieve statement can be read, and each of
them silently corrupts the model if it is skipped:

* **The scan area is columns 1-72.** Columns 73-80 are the sequence-number area and the
  compiler never sees them. A member kept in an 80-byte PDS almost always carries them, so
  a reader that takes the whole line finds a field named ``00010023`` in the middle of a
  record layout - which then shifts nothing, breaks nothing, and quietly appears as a
  dependency. We trim at the margin, and if the trimmed tail was NOT a sequence number we
  say so rather than dropping it in silence.
* **Continuation is explicit.** A line ending in ``-`` continues on the next line with no
  separating blank; one ending in ``+`` continues with a blank inserted. A ``LINE``
  statement or a long ``IF`` broken this way is one statement, and reading it as two loses
  every field after the break.
* **Instream data is not statements.** ``FILE X TABLE INSTREAM`` is followed by table rows
  until ``ENDTABLE``. Those rows are DATA - tokenising them would produce field names that
  do not exist, in the same way that content-sniffing JCL instream data invents control
  cards.

Nothing here knows what a FILE or a JOB is. This module produces logical lines and tokens;
:mod:`eztrieve_dependencies.parser` decides what they mean.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

#: The last column the Easytrieve compiler scans. 73-80 is the sequence-number area.
RIGHT_MARGIN = 72

#: A comment: an asterisk in column 1. (An asterisk anywhere else is multiplication, or a
#: ``*`` field location meaning "the next available byte".)
_COMMENT = re.compile(r"^\*")

#: Ends the instream data of a ``TABLE INSTREAM`` file.
_ENDTABLE = re.compile(r"^\s*ENDTABLE\b", re.I)

#: Characters that cannot occur inside an Easytrieve name, so a token may be split on them
#: even with no surrounding blanks. ``-`` is deliberately ABSENT: ``TOTAL-PAY`` is one
#: name, and splitting on it would turn every hyphenated field in the estate into a
#: subtraction.
_SPLIT_CHARS = "=()+*/,<>"

#: Prefix marking a captured instream-data row. It is not Easytrieve syntax - it is this
#: module's way of handing the parser a line it must never tokenise as code.
DATA_PREFIX = "@DATA "

#: Statements that open something new, and therefore end a ``TABLE INSTREAM`` file's data
#: even without an ``ENDTABLE``. Kept here rather than in the parser because the lexer has
#: to decide, line by line, whether it is looking at code or at data.
_SECTION_KEYWORDS = frozenset(("FILE", "DEFINE", "PARM", "COPY", "JOB", "SORT", "REPORT"))


@dataclass
class LogicalLine:
    """One Easytrieve statement, continuations already joined."""

    text: str
    line: int                       # 1-relative physical line the statement starts on
    raw: str = ""                   # the physical line(s), before trimming/joining
    #: The macro this statement came out of, if any - ``None`` for the program's own text.
    #: Carried all the way onto the field and lineage rows, because "this field is defined
    #: in macro CUSTREC" is a question a reader of the expanded text cannot otherwise ask.
    origin: Optional[str] = None
    #: Macro nesting depth at which it was produced (0 = the program source).
    depth: int = 0

    def tokens(self) -> List[str]:
        return tokenize(self.text)

    @property
    def is_data(self) -> bool:
        return self.text.startswith(DATA_PREFIX)

    @property
    def data(self) -> str:
        return self.text[len(DATA_PREFIX):] if self.is_data else ""


def _looks_like_sequence(tail: str) -> bool:
    """A sequence-number area: a short run of alphanumerics. Recognised only so the
    ordinary case does not emit a flag for every line of every member."""
    t = tail.strip()
    return bool(t) and len(t) <= 8 and re.fullmatch(r"[A-Z0-9]+", t, re.I) is not None


def _trim(line: str, margin: int, lineno: int, flags: List[str]) -> str:
    """Cut a physical line at the compiler's right margin, reporting a non-blank tail."""
    if margin <= 0 or len(line) <= margin:
        return line.rstrip()
    head, tail = line[:margin], line[margin:]
    if tail.strip() and not _looks_like_sequence(tail):
        flags.append(
            "line {n}: text past column {m} was ignored, as the Easytrieve compiler "
            "ignores it - {t!r}. If this member is not in fixed 80-byte format, re-run "
            "with --right-margin 0 to scan whole lines".format(
                n=lineno, m=margin, t=tail.strip()[:30]))
    return head.rstrip()


def _unclosed_literal(text: str) -> bool:
    """True when an odd number of quotes leaves a literal open at end of line."""
    return text.count("'") % 2 == 1


def _starts_instream_table(text: str) -> bool:
    up = [t.upper() for t in tokenize(text)]
    return bool(up) and up[0] == "FILE" and "INSTREAM" in up


def looks_like_field_definition(tokens: List[str]) -> bool:
    """``NAME location length [format]`` - the shape of an Easytrieve field declaration.

    This is what separates a ``TABLE INSTREAM`` file's DECLARATIONS from its DATA. The
    declarations come first and the rows follow, with no marker between them, so the
    boundary is the first line that is not shaped like a declaration. A data row starts
    with its argument - in practice a quoted literal - which is not a name, so the two do
    not collide in ordinary source. Getting this wrong in either direction is visible
    rather than silent: declarations read as data leave the table with no fields, and data
    read as declarations produces fields whose names are the table's values.
    """
    if len(tokens) < 3 or not NAME.match(tokens[0]):
        return False
    location, length = tokens[1], tokens[2]
    if not _NUMBER.match(length):
        return False
    return (location == "*" or bool(_NUMBER.match(location))
            or location.upper() in ("W", "S") or bool(NAME.match(location)))


def logical_lines(text: str, *, margin: int = RIGHT_MARGIN
                  ) -> Tuple[List[LogicalLine], List[str]]:
    """Physical source -> (logical lines, flags).

    Comments and blank lines are dropped; continuations are joined; ``TABLE INSTREAM`` data
    is passed through as ``@DATA`` pseudo-statements so the parser can attach it to its file
    without ever tokenising it as code.
    """
    flags: List[str] = []
    physical = text.splitlines()
    out: List[LogicalLine] = []
    i, n = 0, len(physical)
    in_table_data = False
    # A TABLE INSTREAM file's field DECLARATIONS come between its FILE statement and its
    # data rows, with no marker between them - so after the FILE statement we are
    # "pending", and the first line not shaped like a declaration begins the data.
    pending_table = False
    while i < n:
        lineno = i + 1
        raw = physical[i]
        if _COMMENT.match(raw) and not in_table_data:
            # A comment runs to the end of the line whatever the margin is, so trimming it
            # must not report a "tail past column 72" for ordinary prose.
            i += 1
            continue
        stripped = _trim(raw, margin, lineno, flags)

        if in_table_data:
            if _ENDTABLE.match(stripped):
                out.append(LogicalLine(text="ENDTABLE", line=lineno, raw=raw))
                in_table_data = False
            elif stripped.strip() and not _COMMENT.match(raw):
                # Verbatim, leading blanks kept: table rows are positional data.
                out.append(LogicalLine(text=DATA_PREFIX + stripped, line=lineno, raw=raw))
            i += 1
            continue

        if not stripped.strip():
            i += 1
            continue

        if pending_table and not looks_like_field_definition(tokenize(stripped)):
            # The declarations are over; from here to ENDTABLE it is data. Decided BEFORE
            # continuation joining, and passed on with its leading blanks intact, because a
            # table row is positional and a `-` at the end of one is a hyphen. A statement
            # that opens something else ends the table instead - an INSTREAM file with no
            # rows at all must not swallow the rest of the member.
            pending_table = False
            first = (tokenize(stripped) or [""])[0].upper()
            if _ENDTABLE.match(stripped):
                out.append(LogicalLine(text="ENDTABLE", line=lineno, raw=raw))
                i += 1
                continue
            if first not in _SECTION_KEYWORDS:
                in_table_data = True
                out.append(LogicalLine(text=DATA_PREFIX + stripped, line=lineno, raw=raw))
                i += 1
                continue

        joined = stripped
        raws = [raw]
        # Continuation: `-` joins with nothing between, `+` joins with one blank. A line
        # whose quote count is odd has an open literal, and its trailing character is part
        # of the literal rather than a continuation mark.
        while joined.endswith(("-", "+")) and not _unclosed_literal(joined) \
                and joined[:-1].strip():
            joiner = "" if joined.endswith("-") else " "
            joined = joined[:-1].rstrip() + joiner
            i += 1
            while i < n and _COMMENT.match(physical[i]):
                i += 1
            if i >= n:
                flags.append(
                    "line {n}: the statement ends the member with a continuation "
                    "character and nothing to continue onto".format(n=lineno))
                break
            raws.append(physical[i])
            joined = joined + _trim(physical[i], margin, i + 1, flags).strip()

        final = joined.strip()
        if final:
            out.append(LogicalLine(text=final, line=lineno, raw="\n".join(raws)))
            if _starts_instream_table(final):
                pending_table = True
        i += 1

    if in_table_data:
        flags.append("a TABLE INSTREAM block was never closed by ENDTABLE - its data rows "
                     "ran to the end of the member")
    return out, flags


def tokenize(text: str) -> List[str]:
    """Split one statement into tokens.

    Quoted literals stay whole (``''`` is an embedded quote); parentheses and operators are
    their own tokens; commas separate and are discarded. A hyphen is never a separator,
    because ``TOTAL-PAY`` is one name - so subtraction must be written with blanks around
    it, which is how Easytrieve is written anyway.
    """
    out: List[str] = []
    cur: List[str] = []
    i, n = 0, len(text)

    def flush() -> None:
        if cur:
            out.append("".join(cur))
            del cur[:]

    while i < n:
        ch = text[i]
        if ch == "'":
            # A typed literal (X'0C', P'123', C'A') keeps its prefix letter attached.
            prefix = "".join(cur) if len(cur) == 1 and "".join(cur).upper() in "XPBCZ" \
                else ""
            if prefix:
                del cur[:]
            else:
                flush()
            j = i + 1
            lit = [prefix, "'"]
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        lit.append("''")
                        j += 2
                        continue
                    break
                lit.append(text[j])
                j += 1
            lit.append("'")
            out.append("".join(lit))
            i = j + 1
            continue
        if ch.isspace() or ch == ",":
            flush()
            i += 1
            continue
        if ch in _SPLIT_CHARS:
            flush()
            # Two-character relational operators written without blanks.
            if ch in "<>" and i + 1 < n and text[i + 1] == "=":
                out.append(ch + "=")
                i += 2
                continue
            out.append(ch)
            i += 1
            continue
        cur.append(ch)
        i += 1
    flush()
    return out


def is_literal(token: str) -> bool:
    body = token[1:] if token[:1].upper() in ("X", "P", "B", "C", "Z") else token
    return len(body) >= 2 and body.startswith("'") and body.endswith("'")


def literal_value(token: str) -> str:
    body = token[1:] if token[:1].upper() in ("X", "P", "B", "C", "Z") else token
    if len(body) >= 2 and body.startswith("'") and body.endswith("'"):
        return body[1:-1].replace("''", "'")
    return token


_NUMBER = re.compile(r"^[+-]?\d+(\.\d+)?$")


def is_number(token: str) -> bool:
    return bool(_NUMBER.match(token))


def is_constant(token: str) -> bool:
    """A value, not a field reference: a quoted or typed literal, or a number."""
    return is_literal(token) or is_number(token)


#: A name Easytrieve will accept for a field, file, activity, report or procedure. The
#: colon form is a QUALIFIED field reference (``PERSNL:NAME``), used when two files declare
#: the same field name.
NAME = re.compile(r"^[A-Z@#$][A-Z0-9@#$_-]*(:[A-Z@#$][A-Z0-9@#$_-]*)?$", re.I)


def is_name(token: str) -> bool:
    return bool(NAME.match(token))


def split_qualified(token: str) -> Tuple[Optional[str], str]:
    """``PERSNL:NAME`` -> ``("PERSNL", "NAME")``; ``NAME`` -> ``(None, "NAME")``."""
    if ":" in token:
        owner, _, name = token.partition(":")
        return owner.upper(), name.upper()
    return None, token.upper()

"""Easytrieve program parser: statements -> a :mod:`eztrieve_dependencies.model` Program.

Easytrieve sits exactly between the two things the sibling tools model. Like COBOL it is a
program - it assigns, branches, loops, calls and writes. Like JCL it is where a name is
bound to something outside itself - its ``FILE`` statement names a **ddname**, and only the
JCL says which dataset that ddname is. And unlike either, it declares its record layouts
**positionally, in the same source file**: ``GROSS 100 4 P 2`` is bytes 100-103 of the
PERSNL record, packed, two decimals. That is why a field-level lineage is worth extracting
here specifically - both ends of every edge have a concrete byte range without a copybook,
a DCLGEN or a control card having to be resolved first.

What this module does NOT do is decide anything about lineage; it produces the structure,
and :mod:`eztrieve_dependencies.lineage` walks it.

**The resolver.** Macros live outside the source file and are retrieved by the caller's
``resolver(name) -> text | None`` (see :mod:`eztrieve_dependencies.macros`). Anything it
cannot return is flagged, never guessed.

**Honest limits, surfaced as flags rather than smoothed over.** A statement whose verb this
module does not model is recorded with a flag saying so, because an unmodelled verb that
moves data is a hole in the lineage and a silent one is worthless. Unbalanced ``IF`` /
``DO`` / ``CASE`` nesting is flagged, because it makes every condition after it wrong. A
field whose declaration this module cannot read keeps its raw text and says why. A ``CALL``
is opaque: its arguments are recorded as possibly modified, never as definitely unchanged.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Dict, List, Optional, Sequence, Tuple

from .lexer import (RIGHT_MARGIN, LogicalLine, is_constant, is_name, is_number,
                    logical_lines)
from .macros import MacroExpander, Resolver
from .model import (STORAGE_FILE, STORAGE_RESET, STORAGE_STATIC, Activity, Field,
                    FileDef, Procedure, Program, ReportDef, ReportLine, Statement,
                    TableEntry)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# vocabulary
# --------------------------------------------------------------------------- #

#: Statements that open an activity - Easytrieve's unit of execution.
_ACTIVITY = ("JOB", "SORT", "REPORT")

#: Library-section statements.
_LIBRARY = ("PARM", "FILE", "DEFINE", "COPY")

#: Report subordinate statements.
_REPORT_STMTS = ("SEQUENCE", "CONTROL", "SUM", "TITLE", "HEADING", "LINE")

#: Report exit procedures. Written as labels (``BEFORE-BREAK. PROC``) like any other.
_REPORT_PROCS = ("REPORT-INPUT", "BEFORE-BREAK", "AFTER-BREAK", "BEFORE-LINE",
                 "AFTER-LINE", "ENDPAGE", "TERMINATION")

#: Verbs this module models: it knows which operand is a file, which is a field, and which
#: direction data moves. A verb NOT in here is still recorded, and flagged - see
#: ``_unmodelled``.
_VERBS = (
    # data movement
    "MOVE", "ASSIGN",
    # file I/O
    "GET", "PUT", "POINT", "WRITE", "READ", "RETRIEVE", "CLOSE", "SELECT",
    # output
    "DISPLAY", "PRINT", "NEWPAGE",
    # lookup / database
    "SEARCH", "SQL",
    # flow
    "PERFORM", "CALL", "GOTO", "GO", "STOP", "REFRESH", "LINK", "EXIT",
)

#: Control-structure keywords, handled by the condition stack rather than as statements.
_CONTROL = ("IF", "ELSE", "ELSE-IF", "END-IF", "DO", "END-DO", "UNTIL",
            "CASE", "WHEN", "OTHERWISE", "END-CASE", "PROC", "END-PROC")

#: FILE operands this module understands. Everything else is kept in ``attributes``.
_DEVICES = ("DISK", "TAPE", "PRINTER", "CARD", "TERMINAL", "DUMMY")
_ORGANIZATIONS = {"VS": "VSAM", "VSAM": "VSAM", "SQL": "SQL", "TABLE": "TABLE",
                  "DLI": "DL/I", "IDMS": "IDMS", "IS": "INDEXED",
                  "INDEXED": "INDEXED", "SEQUENTIAL": "SEQUENTIAL"}
_RECFM = ("F", "FB", "FBS", "V", "VB", "VBS", "U", "VS")
_USAGE = ("CREATE", "UPDATE", "RETRIEVE", "DEFER", "NOVERIFY", "EXTENDED", "RESET",
          "STATUS", "PASSWORD", "SEQUENTIAL", "NOPRINT")

#: Field attribute clauses.
_FIELD_ATTRS = ("MASK", "HEADING", "VALUE", "OCCURS", "INDEX", "RESET", "SIGNED",
                "BWZ", "HEX")

#: Numeric formats - only these may carry a decimal-position operand.
_NUMERIC_FORMATS = ("N", "P", "U", "B")
_FORMATS = _NUMERIC_FORMATS + ("A", "K", "M")

_LABEL = re.compile(r"^([A-Z@#$][A-Z0-9@#$_-]*)\.$", re.I)


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def parse_eztrieve(text: str, resolver: Optional[Resolver] = None,
                   source_name: str = "<eztrieve>",
                   program_name: Optional[str] = None,
                   margin: int = RIGHT_MARGIN) -> Program:
    """Parse an Easytrieve program.

    ``resolver(name) -> text | None`` is the caller's retrieval for macro members; anything
    it cannot return is flagged, never guessed. ``margin`` is the compiler's right-hand
    scan column (72; pass 0 to scan whole lines for a member that is not in fixed 80-byte
    format).
    """
    return _Parser(text, resolver, source_name, program_name, margin).parse()


def default_program_name(source_name: str) -> str:
    """The program's identity.

    Easytrieve has no ``PROGRAM-ID``: a program is identified by the member it is stored
    as, and that is what the JCL step names on its ``SYSIN``. So the member name IS the
    identity, and inventing anything else here would break the join to the JCL.
    """
    stem = str(source_name).replace("\\", "/").rsplit("/", 1)[-1]
    for _ in range(2):                      # payroll.ezt -> payroll ; x.ezt.bak -> x.ezt
        if "." in stem:
            stem = stem.rsplit(".", 1)[0]
        else:
            break
    return stem.upper() or "PROGRAM"


class _Parser:
    def __init__(self, text: str, resolver: Optional[Resolver], source_name: str,
                 program_name: Optional[str], margin: int):
        self.program = Program(name=program_name or default_program_name(source_name),
                               source_name=source_name)
        self.resolver = resolver
        self.margin = margin
        self._text = text
        # library-section state
        self._file: Optional[FileDef] = None
        self._cursor: Dict[str, int] = {}       # file -> next free byte, for a `*` location
        # activity state
        self._activity: Optional[Activity] = None
        self._proc: Optional[Procedure] = None
        self._report: Optional[ReportDef] = None
        self._conds: List[dict] = []
        self._unmodelled: Dict[str, int] = {}   # verb -> first line seen
        self._anon = 0

    # -- top level ----------------------------------------------------------
    def parse(self) -> Program:
        lines, lex_flags = logical_lines(self._text, margin=self.margin)
        self._flag_all(lex_flags)

        expander = MacroExpander(self.resolver, margin=self.margin)
        lines = expander.expand(lines)
        self.program.macros = expander.uses
        self._flag_all(expander.flags)

        for ln in lines:
            try:
                self._line(ln)
            except Exception as exc:            # one bad statement must not lose the rest
                self._flag("line {0}: could not be parsed ({1!r}) - {2!r}".format(
                    ln.line, exc, ln.text[:60]))
                logger.debug("Easytrieve statement failed to parse", exc_info=True)

        self._close_conditions()
        for verb, line in sorted(self._unmodelled.items()):
            self._flag(
                "statement verb {0} (first at line {1}) is not modelled - any field it "
                "reads or writes is NOT in the field lineage".format(verb, line))
        self.program.notes.append(
            "The program name is the member name: Easytrieve has no PROGRAM-ID, and the "
            "member is what a JCL step names on its SYSIN.")
        return self.program

    # -- helpers ------------------------------------------------------------
    def _flag(self, msg: str) -> None:
        if msg not in self.program.flags:
            self.program.flags.append(msg)

    def _flag_all(self, msgs: Sequence[str]) -> None:
        for m in msgs:
            self._flag(m)

    def _line(self, ln: LogicalLine) -> None:
        if ln.is_data:
            self._table_row(ln)
            return
        toks = ln.tokens()
        if not toks:
            return
        head = toks[0].upper()

        if head == "ENDTABLE":
            self._file = None
            return
        if head in _ACTIVITY and self._opens_activity(head, toks):
            self._start_activity(head, toks, ln)
            return
        if self._activity is None:
            self._library_line(head, toks, ln)
            return
        self._activity_line(head, toks, ln)

    def _opens_activity(self, head: str, toks: List[str]) -> bool:
        """``SORT`` is an activity header; it is also a plausible field name, and ``REPORT``
        opens an activity only in the activity section. Require the shape."""
        if head == "JOB":
            return True
        if head == "SORT":
            return len(toks) >= 2 and "TO" in [t.upper() for t in toks]
        if head == "REPORT":
            return len(toks) >= 2 and is_name(toks[1])
        return False

    # ------------------------------------------------------------------ #
    # library section
    # ------------------------------------------------------------------ #
    def _library_line(self, head: str, toks: List[str], ln: LogicalLine) -> None:
        if head == "PARM":
            self.program.parms.append(ln.text)
            return
        if head == "FILE":
            self._file_statement(toks, ln)
            return
        if head == "DEFINE":
            self._field_statement(toks[1:], ln, from_define=True)
            return
        if head == "COPY":
            self._copy_statement(toks, ln)
            return
        # Anything else in the library section is a field definition.
        self._field_statement(toks, ln, from_define=False)

    def _file_statement(self, toks: List[str], ln: LogicalLine) -> None:
        if len(toks) < 2 or not is_name(toks[1]):
            self._flag("line {0}: FILE statement with no usable file name - {1!r}".format(
                ln.line, ln.text[:60]))
            return
        fd = FileDef(name=toks[1].upper(), line=ln.line, raw=ln.text, origin=ln.origin)
        rest = toks[2:]
        i = 0
        while i < len(rest):
            tok = rest[i]
            up = tok.upper()
            fd.attributes.append(tok)
            if up in _RECFM and i + 1 < len(rest) and rest[i + 1] == "(":
                fd.recfm = up
                group, i = self._paren_group(rest, i + 1)
                nums = [int(g) for g in group if is_number(g)]
                if nums:
                    fd.record_length = nums[0]
                if len(nums) > 1:
                    fd.block_size = nums[1]
                fd.attributes.extend(["("] + group + [")"])
                continue
            if up in _RECFM and up not in _ORGANIZATIONS:
                # `VS` is both a record format (variable spanned) and Easytrieve's VSAM
                # indicator, and the second is what it nearly always means. A record format
                # carries its lengths - `VS(200 4000)` - and is caught by the branch above;
                # a bare `VS` falls through to the organization below.
                fd.recfm = up
            elif up in _DEVICES:
                fd.device = up
            elif up in _ORGANIZATIONS:
                fd.organization = _ORGANIZATIONS[up]
                fd.table = fd.table or up == "TABLE"
            elif up == "VIRTUAL":
                fd.virtual = True
                fd.device = fd.device or "VIRTUAL"
            elif up == "INSTREAM":
                fd.instream = True
            elif up in _USAGE:
                fd.usage.append(up)
            elif up in ("EXIT", "KEY", "WORKAREA", "PASSWORD") and i + 1 < len(rest) \
                    and rest[i + 1] == "(":
                group, i = self._paren_group(rest, i + 1)
                fd.attributes.extend(["("] + group + [")"])
                if up == "EXIT" and group:
                    fd.exit_program = group[0].upper()
                elif up == "KEY":
                    fd.key_fields.extend(g.upper() for g in group if is_name(g))
                continue
            i += 1

        if fd.organization == "SQL":
            fd.sql_text = ln.text
            self._flag(
                "file {0} at line {1} is declared SQL - its columns come from the "
                "database, so any field this tool did not see declared here is not in "
                "the model".format(fd.name, ln.line))
        if fd.name in self.program.files:
            self._flag("file {0} is declared more than once (line {1}) - the later "
                       "declaration's fields are added to the first".format(
                           fd.name, ln.line))
            self._file = self.program.files[fd.name]
            return
        self.program.files[fd.name] = fd
        self._cursor[fd.name] = 1
        self._file = fd

    def _paren_group(self, toks: List[str], open_at: int) -> Tuple[List[str], int]:
        """Contents of the parenthesised group starting at ``open_at``, and the index just
        past its close."""
        depth = 0
        out: List[str] = []
        i = open_at
        while i < len(toks):
            t = toks[i]
            if t == "(":
                depth += 1
                if depth == 1:
                    i += 1
                    continue
            elif t == ")":
                depth -= 1
                if depth == 0:
                    return out, i + 1
            out.append(t)
            i += 1
        return out, i

    def _copy_statement(self, toks: List[str], ln: LogicalLine) -> None:
        """``COPY other-file`` inside a file's field list: take that file's declarations.

        Copied at the SAME byte positions, which is what Easytrieve does and what makes the
        two records comparable - and it is why a sorted copy of an input file has a lineage
        that is a whole-record identity rather than an unknown."""
        if self._file is None:
            self._flag("line {0}: COPY outside a FILE declaration - ignored".format(
                ln.line))
            return
        if len(toks) < 2 or not is_name(toks[1]):
            self._flag("line {0}: COPY with no usable file name".format(ln.line))
            return
        source = toks[1].upper()
        src = self.program.files.get(source)
        if src is None:
            self._flag(
                "file {0}: COPY {1} names a file that is not declared before it - its "
                "fields are NOT in the model".format(self._file.name, source))
            return
        if src is self._file:
            self._flag(
                "file {0}: COPY names the file it is inside - nothing to copy".format(
                    source))
            return
        # Snapshotted BEFORE appending. Without it, a file copying one that (through a
        # chain of COPYs) resolves back to itself would iterate the very list it is
        # extending - which does not raise, it simply never returns.
        fields = list(src.fields)
        self._file.copied_from = source
        for f in fields:
            self._file.fields.append(replace(f, owner=self._file.name, line=ln.line,
                                             origin=ln.origin,
                                             raw="COPY {0}: {1}".format(source, f.raw),
                                             flags=list(f.flags),
                                             heading=list(f.heading),
                                             index=list(f.index)))
        highest = max((f.end or 0) for f in fields) if fields else 0
        self._cursor[self._file.name] = max(self._cursor.get(self._file.name, 1),
                                            highest + 1)

    def _field_statement(self, toks: List[str], ln: LogicalLine,
                         from_define: bool) -> None:
        if not toks or not is_name(toks[0]):
            self._flag("line {0}: not a statement this parser recognises - {1!r}".format(
                ln.line, ln.text[:60]))
            return
        name = toks[0].upper()
        if len(toks) < 2:
            self._flag("line {0}: field {1} has no location operand - not in the "
                       "model".format(ln.line, name))
            return

        loc = toks[1]
        up = loc.upper()
        fld = Field(name=name, owner="", location_raw=loc, line=ln.line,
                    origin=ln.origin, raw=ln.text)

        if up in ("W", "S"):
            fld.storage = STORAGE_STATIC if up == "W" else STORAGE_RESET
        elif is_number(loc):
            fld.start = int(loc)
        elif loc == "*":
            fld.start = None                # filled in below, once the owner is known
        elif is_name(loc):
            fld.redefines = up
        else:
            fld.flags.append("location operand {0!r} was not recognised - this field has "
                             "no byte range in the model".format(loc))

        rest = toks[2:]
        if rest and is_number(rest[0]):
            fld.length = int(rest[0])
            rest = rest[1:]
        else:
            fld.flags.append("no length operand - this field has no byte range in the "
                             "model")

        if rest and rest[0].upper() in _FORMATS:
            fld.fmt = rest[0].upper()
            rest = rest[1:]
            if rest and is_number(rest[0]) and fld.fmt in _NUMERIC_FORMATS:
                fld.decimals = int(rest[0])
                rest = rest[1:]
        elif fld.storage != STORAGE_FILE or fld.start is not None or fld.redefines:
            fld.fmt = "A"                   # Easytrieve's default when none is written

        self._field_attributes(fld, rest)
        self._place(fld, from_define, ln)

    def _field_attributes(self, fld: Field, rest: List[str]) -> None:
        i = 0
        while i < len(rest):
            up = rest[i].upper()
            if up in ("MASK", "HEADING", "INDEX") and i + 1 < len(rest) \
                    and rest[i + 1] == "(":
                group, i = self._paren_group(rest, i + 1)
                if up == "MASK":
                    fld.mask = " ".join(group)
                elif up == "HEADING":
                    fld.heading = [g for g in group]
                else:
                    fld.index = [g.upper() for g in group if is_name(g)]
                continue
            if up == "MASK" and i + 1 < len(rest):
                fld.mask = rest[i + 1]
                i += 2
                continue
            if up == "VALUE" and i + 1 < len(rest):
                fld.value = rest[i + 1]
                i += 2
                continue
            if up == "OCCURS" and i + 1 < len(rest) and is_number(rest[i + 1]):
                fld.occurs = int(rest[i + 1])
                i += 2
                continue
            if up in _FIELD_ATTRS:
                i += 1
                continue
            fld.flags.append("attribute {0!r} was not recognised and is not in the "
                             "model".format(rest[i]))
            i += 1

    def _place(self, fld: Field, from_define: bool, ln: LogicalLine) -> None:
        """File the field under its owner, resolving ``*`` and redefinition positions."""
        working = fld.storage in (STORAGE_STATIC, STORAGE_RESET)
        if working or self._file is None:
            if not working:
                # A DEFINE outside any FILE, with a positional location: the language would
                # have nothing to position it in, so it is working storage - said out loud.
                fld.storage = STORAGE_STATIC
                fld.start = None
                self._flag(
                    "field {0} at line {1} is declared with a record position but no FILE "
                    "is open - treated as working storage".format(fld.name, ln.line))
            if any(w.name == fld.name for w in self.program.working):
                self._flag("field {0} at line {1} is defined more than once in working "
                           "storage".format(fld.name, ln.line))
            self.program.working.append(fld)
            return

        fd = self._file
        fld.owner = fd.name
        if fld.location_raw == "*":
            fld.start = self._cursor.get(fd.name, 1)
        elif fld.redefines:
            base = fd.field_named(fld.redefines)
            if base is not None and base.start is not None:
                fld.start = base.start
            else:
                base_any = next((f for f in self.program.all_fields()
                                 if f.name == fld.redefines and f.start is not None),
                                None)
                if base_any is not None:
                    fld.start = base_any.start
                else:
                    fld.flags.append(
                        "redefines {0}, which is not declared with a byte position before "
                        "it - this field has no byte range in the model".format(
                            fld.redefines))
        if fld.start is not None and fld.length:
            self._cursor[fd.name] = max(self._cursor.get(fd.name, 1),
                                        fld.start + fld.length)
        if fd.field_named(fld.name) is not None:
            self._flag("field {0} is defined more than once in file {1} (line {2}) - "
                       "references to it are ambiguous".format(fld.name, fd.name, ln.line))
        fd.fields.append(fld)

    def _table_row(self, ln: LogicalLine) -> None:
        if self._file is None:
            return
        self._file.table_data.append(TableEntry(line=ln.line, text=ln.data))

    # ------------------------------------------------------------------ #
    # activities
    # ------------------------------------------------------------------ #
    def _start_activity(self, head: str, toks: List[str], ln: LogicalLine) -> None:
        self._close_conditions()
        self._file = None
        self._proc = None
        self._report = None
        act = Activity(kind=head, name="", line=ln.line, origin=ln.origin)
        if head == "JOB":
            self._job_header(act, toks[1:])
        elif head == "SORT":
            self._sort_header(act, toks[1:], ln)
        else:
            self._report_header(act, toks[1:], ln)
        if not act.name:
            self._anon += 1
            act.name = "{0}{1}".format(head, self._anon)
        self.program.activities.append(act)
        self._activity = act

    def _job_header(self, act: Activity, rest: List[str]) -> None:
        i = 0
        while i < len(rest):
            up = rest[i].upper()
            if up == "INPUT":
                i += 1
                if i < len(rest) and rest[i] == "(":
                    group, i = self._paren_group(rest, i)
                    act.inputs.extend(self._input_group(group))
                    continue
                if i < len(rest):
                    if rest[i].upper() == "NULL":
                        act.inputs.append({"file": None, "null": True})
                    else:
                        act.inputs.append({"file": rest[i].upper()})
                    i += 1
                continue
            if up in ("NAME", "START", "FINISH") and i + 1 < len(rest):
                value = rest[i + 1].upper()
                if up == "NAME":
                    act.name = value
                elif up == "START":
                    act.start_proc = value
                else:
                    act.finish_proc = value
                i += 2
                continue
            act.flags.append("JOB operand {0!r} was not recognised".format(rest[i]))
            i += 1
        if not act.inputs:
            act.flags.append("JOB names no INPUT file - it reads nothing this tool can "
                             "see")

    def _input_group(self, group: List[str]) -> List[dict]:
        """``(FILE-A KEY (K1) FILE-B KEY (K2))`` - a synchronised multi-file read."""
        out: List[dict] = []
        i = 0
        while i < len(group):
            tok = group[i]
            if tok.upper() == "KEY":
                keys: List[str] = []
                if i + 1 < len(group) and group[i + 1] == "(":
                    keys, i = self._paren_group(group, i + 1)
                elif i + 1 < len(group):
                    keys, i = [group[i + 1]], i + 2
                if out:
                    out[-1]["key"] = [k.upper() for k in keys if is_name(k)]
                continue
            if is_name(tok):
                out.append({"file": tok.upper()})
            i += 1
        return out

    def _sort_header(self, act: Activity, rest: List[str], ln: LogicalLine) -> None:
        i = 0
        if i < len(rest) and is_name(rest[i]):
            act.inputs.append({"file": rest[i].upper()})
            i += 1
        while i < len(rest):
            up = rest[i].upper()
            if up == "TO" and i + 1 < len(rest):
                act.sort_to = rest[i + 1].upper()
                i += 2
                continue
            if up == "USING":
                i += 1
                group: List[str] = []
                if i < len(rest) and rest[i] == "(":
                    group, i = self._paren_group(rest, i)
                elif i < len(rest):
                    group, i = [rest[i]], i + 1
                j = 0
                while j < len(group):
                    if is_name(group[j]):
                        key = {"field": group[j].upper(), "descending": False}
                        if j + 1 < len(group) and group[j + 1].upper() in ("D", "DESC"):
                            key["descending"] = True
                            j += 1
                        act.sort_keys.append(key)
                    j += 1
                continue
            if up in ("NAME", "BEFORE") and i + 1 < len(rest):
                if up == "NAME":
                    act.name = rest[i + 1].upper()
                else:
                    act.before_proc = rest[i + 1].upper()
                i += 2
                continue
            act.flags.append("SORT operand {0!r} was not recognised".format(rest[i]))
            i += 1
        if not act.sort_to:
            self._flag("line {0}: SORT with no TO file - its output is not in the "
                       "model".format(ln.line))

    def _report_header(self, act: Activity, rest: List[str], ln: LogicalLine) -> None:
        rpt = ReportDef(name=rest[0].upper() if rest and is_name(rest[0]) else "",
                        line=ln.line)
        act.name = rpt.name
        i = 1
        while i < len(rest):
            up = rest[i].upper()
            rpt.attributes.append(rest[i])
            if up == "SUMMARY":
                rpt.summary = True
            elif up in ("PRINTER", "SUMFILE") and i + 1 < len(rest):
                if up == "PRINTER":
                    rpt.printer = rest[i + 1].upper()
                else:
                    rpt.sumfile = rest[i + 1].upper()
                rpt.attributes.append(rest[i + 1])
                i += 2
                continue
            i += 1
        act.report = rpt
        self._report = rpt

    # ------------------------------------------------------------------ #
    # statements inside an activity
    # ------------------------------------------------------------------ #
    def _activity_line(self, head: str, toks: List[str], ln: LogicalLine) -> None:
        label = _LABEL.match(toks[0])
        if label:
            self._open_proc(label.group(1).upper(), toks[1:], ln)
            return
        if head == "END-PROC":
            self._proc = None
            return
        if head in ("IF", "ELSE-IF", "ELSE", "END-IF", "DO", "END-DO", "CASE", "WHEN",
                    "OTHERWISE", "END-CASE"):
            self._control(head, toks, ln)
            return
        if self._report is not None and head in _REPORT_STMTS and self._proc is None:
            self._report_statement(head, toks, ln)
            return
        self._statement(head, toks, ln)

    def _open_proc(self, name: str, rest: List[str], ln: LogicalLine) -> None:
        if self._activity is None:
            self._flag("line {0}: procedure {1} appears before any activity".format(
                ln.line, name))
            return
        self._close_conditions()
        kind = "PROC"
        if name in _REPORT_PROCS:
            kind = name
        proc = Procedure(name=name, kind=kind, line=ln.line, activity=self._activity.name)
        # Inside a REPORT activity every procedure belongs to the report - both its named
        # exits (BEFORE-BREAK, ENDPAGE, ...) and any ordinary PROC written among them.
        target = self._report.procs if self._report is not None else self._activity.procs
        target.append(proc)
        self._proc = proc
        rest = [t for t in rest if t.upper() != "PROC"]
        if rest:
            self._statement(rest[0].upper(), rest, ln)

    # -- condition stack ----------------------------------------------------
    def _control(self, head: str, toks: List[str], ln: LogicalLine) -> None:
        rest = toks[1:]
        if head == "IF":
            self._conds.append(self._frame("IF", rest))
            return
        if head == "DO":
            kind = "DO WHILE"
            if rest and rest[0].upper() in ("WHILE", "UNTIL"):
                kind = "DO " + rest[0].upper()
                rest = rest[1:]
            self._conds.append(self._frame(kind, rest))
            return
        if head == "CASE":
            frame = self._frame("CASE", rest)
            frame["_case"] = True
            self._conds.append(frame)
            return
        if head in ("ELSE", "ELSE-IF"):
            if not self._conds:
                self._flag("line {0}: {1} with no matching IF - conditions on the "
                           "statements after it may be wrong".format(ln.line, head))
                return
            prior = self._conds[-1]
            failed = list(prior.get("afterFailing", []))
            if prior.get("test"):
                failed.append(prior["test"])
            if head == "ELSE":
                frame = dict(prior)
                frame.update({"kind": "ELSE", "negated": True, "afterFailing": failed})
            else:
                frame = self._frame("ELSE-IF", rest)
                frame["afterFailing"] = failed
            self._conds[-1] = frame
            return
        if head in ("WHEN", "OTHERWISE"):
            case = self._enclosing_case()
            if case is None:
                self._flag("line {0}: {1} outside a CASE - conditions on the statements "
                           "after it may be wrong".format(ln.line, head))
                return
            if self._conds and self._conds[-1].get("_branch"):
                prior = self._conds.pop()
                seen = list(prior.get("afterFailing", [])) + [prior["test"]]
            else:
                seen = []
            subject = case.get("test", "")
            if head == "WHEN":
                test = "{0} = {1}".format(subject, " ".join(rest)) if rest else subject
                frame = {"kind": "WHEN", "test": test, "negated": False,
                         "fields": list(case.get("fields", [])) +
                                   self._names(rest), "_branch": True}
            else:
                frame = {"kind": "OTHERWISE", "test": subject, "negated": True,
                         "fields": list(case.get("fields", [])), "_branch": True}
            if seen:
                frame["afterFailing"] = seen
            self._conds.append(frame)
            return
        # END-IF / END-DO / END-CASE
        want = {"END-IF": ("IF", "ELSE", "ELSE-IF"),
                "END-DO": ("DO WHILE", "DO UNTIL"),
                "END-CASE": ("CASE",)}[head]
        for idx in range(len(self._conds) - 1, -1, -1):
            if self._conds[idx].get("kind") in want:
                del self._conds[idx:]
                return
        self._flag("line {0}: {1} with no matching opener - conditions on the statements "
                   "after it may be wrong".format(ln.line, head))

    def _enclosing_case(self) -> Optional[dict]:
        for frame in reversed(self._conds):
            if frame.get("_case"):
                return frame
        return None

    def _frame(self, kind: str, rest: List[str]) -> dict:
        text = " ".join(rest).strip()
        return {"kind": kind, "test": text, "negated": False, "fields": self._names(rest)}

    def _names(self, toks: Sequence[str]) -> List[str]:
        return [t.upper() for t in toks
                if is_name(t) and not is_constant(t) and t.upper() not in
                ("AND", "OR", "NOT", "EQ", "NE", "GT", "LT", "GE", "LE", "TO", "THRU",
                 "EOF", "WHILE", "UNTIL", "NULL")]

    def _close_conditions(self) -> None:
        if self._conds:
            open_kinds = ", ".join(sorted({c["kind"] for c in self._conds}))
            self._flag(
                "{0} unterminated control structure(s) ({1}) at the end of an activity or "
                "procedure - conditions on the statements after them may be wrong".format(
                    len(self._conds), open_kinds))
            self._conds = []

    # -- ordinary statements -------------------------------------------------
    def _statement(self, head: str, toks: List[str], ln: LogicalLine) -> None:
        if self._activity is None:
            self._flag("line {0}: statement outside any activity - {1!r}".format(
                ln.line, ln.text[:60]))
            return
        verb = head
        operands = toks[1:]
        if len(toks) > 1 and toks[1] == "=":
            verb, operands = "ASSIGN", toks
        elif head not in _VERBS and head not in _CONTROL:
            self._unmodelled.setdefault(head, ln.line)

        st = Statement(verb=verb, operands=list(operands), text=ln.text, line=ln.line,
                       origin=ln.origin, activity=self._activity.name,
                       proc=self._proc.name if self._proc else None,
                       conditions=[self._public(c) for c in self._conds])
        if verb == "CALL":
            st.flags.append(
                "CALL is opaque: the called program may change any argument, so its "
                "arguments are recorded as possibly modified, not as unchanged")
        if self._proc is not None:
            self._proc.statements.append(st)
        else:
            self._activity.statements.append(st)

    @staticmethod
    def _public(frame: dict) -> dict:
        return {k: v for k, v in frame.items() if not k.startswith("_")}

    # -- report subordinate statements --------------------------------------
    def _report_statement(self, head: str, toks: List[str], ln: LogicalLine) -> None:
        rpt = self._report
        assert rpt is not None
        rest = toks[1:]
        if head == "SEQUENCE":
            i = 0
            while i < len(rest):
                if is_name(rest[i]):
                    entry = {"field": rest[i].upper(), "descending": False}
                    if i + 1 < len(rest) and rest[i + 1].upper() in ("D", "DESC"):
                        entry["descending"] = True
                        i += 1
                    rpt.sequence.append(entry)
                i += 1
            return
        if head == "CONTROL":
            i = 0
            while i < len(rest):
                tok = rest[i]
                if tok.upper() == "FINAL" or is_name(tok):
                    entry = {"field": tok.upper(), "options": []}
                    j = i + 1
                    while j < len(rest) and rest[j].upper() in (
                            "NEWPAGE", "NOPRINT", "RENUM", "ALL", "TALLY"):
                        entry["options"].append(rest[j].upper())
                        j += 1
                    rpt.control.append(entry)
                    i = j
                    continue
                i += 1
            return
        if head == "SUM":
            rpt.sums.extend(t.upper() for t in rest if is_name(t))
            return
        if head == "TITLE":
            rpt.titles.append({"index": int(rest[0]) if rest and is_number(rest[0]) else 1,
                               "items": self._render_items(
                                   rest[1:] if rest and is_number(rest[0]) else rest)})
            return
        if head == "HEADING":
            if rest and is_name(rest[0]):
                group, _ = self._paren_group(rest, 1) if len(rest) > 1 \
                    and rest[1] == "(" else (rest[1:], len(rest))
                rpt.headings[rest[0].upper()] = list(group)
            return
        if head == "LINE":
            idx = int(rest[0]) if rest and is_number(rest[0]) else len(rpt.lines) + 1
            body = rest[1:] if rest and is_number(rest[0]) else rest
            rpt.lines.append(ReportLine(index=idx, items=self._render_items(body),
                                        line=ln.line))
            return

    def _render_items(self, toks: Sequence[str]) -> List[dict]:
        """The items across a TITLE or LINE, in printed order."""
        out: List[dict] = []
        i = 0
        toks = list(toks)
        while i < len(toks):
            tok = toks[i]
            up = tok.upper()
            if up in ("POS", "COL", "SKIP", "SPACE") and i + 1 < len(toks):
                out.append({"kind": "position", "keyword": up, "value": toks[i + 1]})
                i += 2
                continue
            if is_constant(tok):
                out.append({"kind": "literal", "value": tok})
            elif is_name(tok):
                out.append({"kind": "field", "field": up})
            elif tok not in ("(", ")"):
                out.append({"kind": "other", "value": tok})
            i += 1
        return out

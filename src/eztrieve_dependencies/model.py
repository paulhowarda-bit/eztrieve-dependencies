"""The parsed shape of an Easytrieve program.

Separate from the parser because three other modules read it and none of them should have
to import a parser to do so - and because the one thing this model exists to carry is the
thing the whole tool is for: **every field knows its bytes**.

Easytrieve declares a record layout positionally (``GROSS 100 4 P 2`` is bytes 100-103,
packed, two decimals), which is a stronger statement than COBOL's ``PIC`` chain: there is
no arithmetic to get wrong and no ``REDEFINES`` to unwind before a byte range is known. So
a field reference in this model is always resolvable to a concrete byte range in a concrete
record, and a lineage edge between two fields is therefore an edge between two byte ranges
in two datasets - which is what "field-level lineage" has to mean to be worth anything.

Storage classes, kept apart because they behave differently and a reader needs to know
which one a field is:

``file``            a field of a record - it has a byte range and an owning file.
``working-static``  ``DEFINE X W ...`` - working storage, initialised once before the
                    first activity and retained across activities.
``working-reset``   ``DEFINE X S ...`` - working storage the runtime reinitialises at the
                    start of each activity, so a value cannot carry across one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

#: Storage classes (the strings are output; see the module docstring).
STORAGE_FILE = "file"
STORAGE_STATIC = "working-static"
STORAGE_RESET = "working-reset"

#: Easytrieve data formats and what each one is, spelt out because the letter alone is
#: not self-explanatory outside the language and it decides how many bytes a value needs.
FORMATS: Dict[str, str] = {
    "A": "alphanumeric",
    "N": "zoned decimal (numeric display)",
    "P": "packed decimal",
    "U": "unsigned packed decimal",
    "B": "binary",
    "K": "Kanji (DBCS)",
    "M": "mixed SBCS/DBCS",
}


@dataclass
class Field:
    """One declared field: a name, a storage class, and - for a record field - its bytes."""

    name: str
    owner: str                              # file name, or "" for working storage
    storage: str = STORAGE_FILE
    location_raw: str = ""                  # the location operand exactly as written
    start: Optional[int] = None             # 1-relative first byte within the record
    length: Optional[int] = None
    fmt: Optional[str] = None               # A / N / P / U / B ...
    decimals: Optional[int] = None
    heading: List[str] = field(default_factory=list)
    mask: Optional[str] = None
    value: Optional[str] = None             # VALUE clause literal
    occurs: Optional[int] = None
    index: List[str] = field(default_factory=list)
    #: Set when the location operand named another field rather than a byte position - an
    #: Easytrieve redefinition, the same aliasing hazard COBOL's REDEFINES carries.
    redefines: Optional[str] = None
    line: int = 0
    origin: Optional[str] = None            # the macro this declaration came from
    raw: str = ""
    flags: List[str] = field(default_factory=list)

    @property
    def end(self) -> Optional[int]:
        if self.start is None or self.length is None:
            return None
        return self.start + self.length - 1

    @property
    def key(self) -> str:
        """The identity a lineage edge is keyed on: qualified for a record field, bare for
        working storage (which has no owner to qualify it with)."""
        return "{0}:{1}".format(self.owner, self.name) if self.owner else self.name

    @property
    def bytes_text(self) -> Optional[str]:
        return None if self.end is None else "{0}-{1}".format(self.start, self.end)


@dataclass
class TableEntry:
    """One row of a ``TABLE INSTREAM`` file, kept verbatim."""
    line: int
    text: str


@dataclass
class FileDef:
    """A declared file. Its NAME is the ddname - which is the whole reason this tool joins
    to a JCL model: Easytrieve says ``PERSNL``, and only the JCL says which dataset that
    is."""

    name: str
    line: int = 0
    raw: str = ""
    origin: Optional[str] = None
    attributes: List[str] = field(default_factory=list)   # the operands as written
    device: Optional[str] = None            # DISK / TAPE / PRINTER / CARD / VIRTUAL
    organization: Optional[str] = None      # SEQUENTIAL / VS / SQL / TABLE / DLI / IDMS
    recfm: Optional[str] = None             # F / FB / V / VB / U / VBS
    record_length: Optional[int] = None
    block_size: Optional[int] = None
    usage: List[str] = field(default_factory=list)        # CREATE / UPDATE / RETRIEVE ...
    exit_program: Optional[str] = None      # EXIT(program) - a real program dependency
    key_fields: List[str] = field(default_factory=list)
    instream: bool = False
    table: bool = False
    virtual: bool = False
    sql_text: Optional[str] = None
    copied_from: Optional[str] = None       # library-section COPY of another file's fields
    fields: List[Field] = field(default_factory=list)
    table_data: List[TableEntry] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)

    def field_named(self, name: str) -> Optional[Field]:
        want = name.upper()
        for f in self.fields:
            if f.name == want:
                return f
        return None


@dataclass
class Statement:
    """One executable statement, with the conditions that gate it already attached.

    ``conditions`` is the open IF / DO WHILE / CASE nesting at this point, outermost first,
    each ``{kind, test, fields, negated}``. It rides on the statement rather than being
    recomputed later for the same reason the JCL model stamps IF/THEN/ELSE onto a step: an
    assignment that only happens for ``STATUS = 'A'`` is not the same dependency as one
    that always happens, and a lineage that cannot say so is overstating what it knows.
    """

    verb: str
    operands: List[str] = field(default_factory=list)
    text: str = ""
    line: int = 0
    origin: Optional[str] = None
    activity: str = ""
    proc: Optional[str] = None
    conditions: List[dict] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)


@dataclass
class Procedure:
    name: str
    kind: str = "PROC"                      # PROC, or a report exit (BEFORE-BREAK, ...)
    line: int = 0
    activity: str = ""
    statements: List[Statement] = field(default_factory=list)


@dataclass
class ReportLine:
    """One ``LINE`` of a report: the items across it, in order."""
    index: int
    items: List[dict] = field(default_factory=list)     # {kind: field|literal|position}
    line: int = 0


@dataclass
class ReportDef:
    name: str
    line: int = 0
    attributes: List[str] = field(default_factory=list)
    summary: bool = False
    printer: Optional[str] = None           # PRINTER ddname - a file dependency
    sumfile: Optional[str] = None           # SUMFILE file - a file dependency
    #: The REPORT operands a column's print position depends on, only as coded:
    #: ``linesize`` / ``space`` (ints), ``spread`` / ``nospread`` / ``noadjust`` (True).
    #: Nothing is defaulted - LINESIZE defaults from the PRINTER file or a site option,
    #: and SPREAD can be a site default, so an absent key means "not coded here".
    layout: Dict[str, object] = field(default_factory=dict)
    sequence: List[dict] = field(default_factory=list)   # {field, descending}
    control: List[dict] = field(default_factory=list)    # {field, options}
    sums: List[str] = field(default_factory=list)
    titles: List[dict] = field(default_factory=list)
    headings: Dict[str, List[str]] = field(default_factory=dict)
    lines: List[ReportLine] = field(default_factory=list)
    procs: List[Procedure] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)


@dataclass
class Activity:
    """A JOB, SORT or REPORT activity - Easytrieve's unit of execution, and the analogue of
    a JCL step: it names its inputs and outputs and runs in sequence with its siblings."""

    kind: str                               # JOB / SORT / REPORT
    name: str
    line: int = 0
    origin: Optional[str] = None
    inputs: List[dict] = field(default_factory=list)    # {file, key}
    #: SORT only.
    sort_to: Optional[str] = None
    sort_keys: List[dict] = field(default_factory=list)
    start_proc: Optional[str] = None
    finish_proc: Optional[str] = None
    before_proc: Optional[str] = None
    statements: List[Statement] = field(default_factory=list)
    procs: List[Procedure] = field(default_factory=list)
    report: Optional[ReportDef] = None
    flags: List[str] = field(default_factory=list)

    def all_statements(self) -> List[Statement]:
        out = list(self.statements)
        for p in self.procs:
            out.extend(p.statements)
        if self.report is not None:
            for p in self.report.procs:
                out.extend(p.statements)
        return out


@dataclass
class MacroUse:
    """One ``%NAME`` invocation, whether or not it resolved."""
    name: str
    line: int
    arguments: List[str] = field(default_factory=list)
    resolved: bool = False
    depth: int = 0
    in_macro: Optional[str] = None          # the macro that invoked it, for nesting


@dataclass
class Program:
    """A whole Easytrieve program: its library section, its activities, and the honest
    account of what could not be resolved."""

    name: str = ""
    source_name: str = "<eztrieve>"
    parms: List[str] = field(default_factory=list)
    files: Dict[str, FileDef] = field(default_factory=dict)
    working: List[Field] = field(default_factory=list)
    activities: List[Activity] = field(default_factory=list)
    macros: List[MacroUse] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def all_fields(self) -> List[Field]:
        out: List[Field] = []
        for f in self.files.values():
            out.extend(f.fields)
        out.extend(self.working)
        return out

    def all_statements(self) -> List[Statement]:
        out: List[Statement] = []
        for act in self.activities:
            out.extend(act.all_statements())
        return out

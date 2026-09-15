"""Field-level dependency lineage over a parsed Easytrieve program.

This is what the package is for. Easytrieve declares its records **positionally in the
source** and then moves data between them with ordinary statements, so both ends of every
edge are a concrete byte range in a concrete record - no copybook, DCLGEN or control card
has to be resolved first for ``PERSNL bytes 100-103 -> PAYEXT bytes 25-30`` to be a
provable fact about the program.

Three layers are built here, each answering a different question:

``edges``      *statement level.* One row per assignment, MOVE, table lookup, sort copy or
               report reference: which field it wrote, which fields it read, by what means,
               and under which conditions.
``flows``      *end to end.* For every sink - a field of a file the program writes, a
               report column, a DISPLAY - the chain back to the input-file fields it
               ultimately derives from, through however many working-storage hops. This is
               the row a modernization actually wants, and no single statement contains it.
``file flow``  *file level.* The same edges aggregated, so the program reads as a dataflow
               between ddnames the JCL can then resolve to datasets.

**Conditions ride on the edges.** ``PX-GRADE`` is assigned the literal ``'H'`` or ``'L'`` -
its *value* comes from no field at all. But which literal is chosen depends on
``ANNUAL-PAY``, which came from ``GROSS``. A lineage that reported only value flow would
say ``PX-GRADE`` has no origin, which is false in every sense a reader cares about. So
every path carries ``influencedBy`` - the fields tested in the ``IF`` / ``CASE`` / ``DO``
nesting that gates it - kept separate from the value sources rather than mixed in with
them.

**What is NOT traversed, and why it is said rather than silently done.** Fields in one
Easytrieve record overlap by design (a group and its elements share bytes). Treating every
positional overlap as an edge would make every field of a record depend on every other and
the output would be true and useless. So overlaps are *listed* on each field, and only an
explicit redefinition - a declaration whose location operand names another field - becomes
a traversable alias edge. Likewise a ``CALL`` may change any argument; those fields are
marked opaque and a path through one says so, instead of the walk pretending the value it
found is the whole story.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .lexer import is_constant, is_name, literal_value, split_qualified
from .model import Activity, Field, FileDef, Program, ReportDef, Statement

#: A SQL table name, possibly schema-qualified. NOT the Easytrieve name rule: a table is
#: ``GLDB.TRANSACTIONS``, and an Easytrieve field name may not contain a dot - so reading
#: SQL operands with the language's own rule finds no tables at all.
_SQL_NAME = re.compile(r"^[A-Z@#$_][A-Z0-9@#$_]*(\.[A-Z@#$_][A-Z0-9@#$_]*)*$", re.I)

#: How many distinct paths may be reported for one sink before the walk stops and says so.
MAX_PATHS = 64
#: How many hops a single path may take before the walk stops and says so.
MAX_DEPTH = 24

#: Words that appear inside a statement's operands and are never field references.
_NOISE = frozenset((
    "TO", "FROM", "WITH", "GIVING", "USING", "BY", "KEY", "EQ", "NE", "GT", "LT", "GE",
    "LE", "AND", "OR", "NOT", "EOF", "NULL", "THRU", "ALL", "ADD", "UPDATE", "DELETE",
    "NEWPAGE", "SKIP", "SPACE", "COL", "POS", "STATUS", "SEQUENTIAL", "D", "DESC",
))


# --------------------------------------------------------------------------- #
# symbols
# --------------------------------------------------------------------------- #

@dataclass
class Resolution:
    """One attempt to turn a token into a declared field."""
    token: str
    field: Optional[Field] = None
    candidates: List[str] = dc_field(default_factory=list)
    reason: Optional[str] = None
    #: Set to the file that decided an otherwise-ambiguous name, so a resolution that
    #: needed the activity's scope is distinguishable from one the name settled on its own.
    scoped: Optional[str] = None


class Symbols:
    """Name -> declared field, with Easytrieve's own qualification rule.

    A bare name that two files both declare is ambiguous in the language too (Easytrieve
    requires ``FILE:FIELD``), so an ambiguous reference is reported with its candidates
    rather than resolved to whichever happened to be declared first."""

    def __init__(self, program: Program):
        self.program = program
        self.by_name: Dict[str, List[Field]] = {}
        for f in program.all_fields():
            self.by_name.setdefault(f.name, []).append(f)

    def resolve(self, token: str, scope: Sequence[str] = ()) -> Resolution:
        """``scope`` is the files in play where the reference was written - the activity's
        INPUT (and a SORT's output), most preferred first.

        It exists for one very common shape: a sort work file declared with
        ``COPY OTHERFILE`` carries every field name the original has, so *every*
        unqualified reference in the program becomes ambiguous by the letter of the rule.
        Easytrieve reads such a reference against the record the activity is working on, so
        this does too - but only when exactly one candidate is in scope, and the resolution
        records that it needed the scope (``scoped``), so a reader can see which references
        were decided that way rather than by the name alone."""
        owner, name = split_qualified(token)
        matches = self.by_name.get(name, [])
        if owner is not None:
            exact = [f for f in matches if f.owner == owner]
            if len(exact) == 1:
                return Resolution(token, exact[0])
            if not exact:
                return Resolution(token, None,
                                  reason="{0} declares no field {1}".format(owner, name))
            return Resolution(token, None, [f.key for f in exact],
                              "{0} declares {1} more than once".format(owner, name))
        if len(matches) == 1:
            return Resolution(token, matches[0])
        if not matches:
            return Resolution(token, None, reason="no field of this name is declared")
        for want in scope:
            in_scope = [f for f in matches if f.owner == want]
            if len(in_scope) == 1:
                return Resolution(token, in_scope[0],
                                  candidates=sorted(f.key for f in matches), scoped=want)
        return Resolution(token, None, sorted(f.key for f in matches),
                          "the name is declared in more than one place, this reference is "
                          "not qualified, and no single file in scope for this activity "
                          "declares it")


# --------------------------------------------------------------------------- #
# edges
# --------------------------------------------------------------------------- #

@dataclass
class Edge:
    """One recorded dependency: ``target`` was written from ``sources``."""

    target: str                                     # node key
    sources: List[str] = dc_field(default_factory=list)
    constants: List[str] = dc_field(default_factory=list)
    via: str = "assignment"
    operators: List[str] = dc_field(default_factory=list)
    activity: str = ""
    proc: Optional[str] = None
    line: int = 0
    origin: Optional[str] = None
    statement: str = ""
    conditions: List[dict] = dc_field(default_factory=list)
    note: Optional[str] = None
    accumulates: bool = False


@dataclass
class Sink:
    """A node that is not a field: a report column, a control break, a DISPLAY."""
    key: str
    kind: str
    detail: dict = dc_field(default_factory=dict)


@dataclass
class FileAccess:
    file: str
    activity: str
    io: str                                          # read / write / update
    how: str                                         # INPUT / GET / PUT / WRITE / SORT ...
    line: int = 0
    keys: List[str] = dc_field(default_factory=list)
    operation: Optional[str] = None                  # ADD / UPDATE / DELETE for WRITE
    conditions: List[dict] = dc_field(default_factory=list)


@dataclass
class CallSite:
    program: str
    activity: str
    line: int
    arguments: List[str] = dc_field(default_factory=list)
    conditions: List[dict] = dc_field(default_factory=list)


@dataclass
class LineageGraph:
    """Everything the views project. Built once, read many times."""

    program: Program
    symbols: Symbols
    edges: List[Edge] = dc_field(default_factory=list)
    sinks: Dict[str, Sink] = dc_field(default_factory=dict)
    access: List[FileAccess] = dc_field(default_factory=list)
    calls: List[CallSite] = dc_field(default_factory=list)
    sql: List[dict] = dc_field(default_factory=list)
    #: field key -> reasons its value may come from outside anything modelled here
    opaque: Dict[str, List[str]] = dc_field(default_factory=dict)
    #: report name -> the activities that PRINT it
    printed_by: Dict[str, List[str]] = dc_field(default_factory=dict)
    unresolved: List[dict] = dc_field(default_factory=list)
    flags: List[str] = dc_field(default_factory=list)

    # -- derived ---------------------------------------------------------
    def field_index(self) -> Dict[str, Field]:
        """``field key -> Field``, built once. Every view turns keys back into descriptors,
        and rebuilding the list per lookup makes that quadratic in a real program's field
        count - which for a wide record layout is not a small number."""
        index = getattr(self, "_field_index", None)
        if index is None:
            index = {f.key: f for f in self.program.all_fields()}
            self._field_index = index
        return index

    def file_io(self) -> Dict[str, str]:
        """ddname -> read / write / read-write, from every access recorded."""
        dirs: Dict[str, Set[str]] = {}
        for a in self.access:
            dirs.setdefault(a.file, set()).add(a.io)
        out: Dict[str, str] = {}
        for name, kinds in dirs.items():
            r = "read" in kinds or "update" in kinds
            w = "write" in kinds or "update" in kinds
            out[name] = "read-write" if (r and w) else ("read" if r else "write")
        for name, fd in self.program.files.items():
            if name in out:
                continue
            # Declared but never touched by a statement or an activity header. The
            # declaration alone is a dependency (the ddname must be allocated), so the file
            # is kept - with its direction unknown rather than invented.
            out[name] = "unknown"
        return out

    def incoming(self) -> Dict[str, List[Edge]]:
        by_target: Dict[str, List[Edge]] = {}
        for e in self.edges:
            by_target.setdefault(e.target, []).append(e)
        return by_target


# --------------------------------------------------------------------------- #
# building
# --------------------------------------------------------------------------- #

def build_graph(program: Program) -> LineageGraph:
    """Walk every activity and record what each statement makes depend on what."""
    return _Builder(program).build()


class _Builder:
    def __init__(self, program: Program):
        self.program = program
        self.symbols = Symbols(program)
        self.graph = LineageGraph(program=program, symbols=self.symbols)
        #: The files in play where the statements now being walked were written. See
        #: ``Symbols.resolve``.
        self._scope: List[str] = []

    # -- helpers ---------------------------------------------------------
    def _flag(self, msg: str) -> None:
        if msg not in self.graph.flags:
            self.graph.flags.append(msg)

    def _note_scoped(self, res: "Resolution") -> None:
        """Say when a name needed the activity's record to settle it. Every route to a
        field goes through here, so a scoped resolution is reported whether it came from a
        statement's operands or from a condition's test."""
        if not res.scoped:
            return
        self._flag(
            "reference {0} is declared in more than one file ({1}); it was read as {2}'s, "
            "because that is the record the activity is working on. Qualify it as {2}:{0} "
            "in the source to remove the doubt".format(
                str(res.token).upper(), ", ".join(res.candidates), res.scoped))

    def _resolve(self, token: str, st: Optional[Statement], role: str) -> Optional[Field]:
        res = self.symbols.resolve(token, self._scope)
        if res.field is not None:
            self._note_scoped(res)
            return res.field
        row = {"reference": token, "role": role, "reason": res.reason}
        if st is not None:
            row.update({"activity": st.activity, "line": st.line,
                        "statement": st.text})
        if res.candidates:
            row["candidates"] = res.candidates
        self.graph.unresolved.append(row)
        return None

    def _refs(self, tokens: Sequence[str], st: Optional[Statement],
              role: str) -> Tuple[List[str], List[str]]:
        """(field keys, constant literals) for a run of operand tokens."""
        keys: List[str] = []
        consts: List[str] = []
        for tok in tokens:
            if tok in ("(", ")"):
                continue
            if is_constant(tok):
                consts.append(tok)
                continue
            if not is_name(tok) or tok.upper() in _NOISE:
                continue
            fld = self._resolve(tok, st, role)
            if fld is not None and fld.key not in keys:
                keys.append(fld.key)
        return keys, consts

    def _add(self, edge: Edge) -> None:
        edge.accumulates = edge.target in edge.sources
        self.graph.edges.append(edge)

    def _conds(self, st: Statement) -> List[dict]:
        """A statement's gating conditions with their tested names resolved to field keys.

        The parser records the names as written, because it has no symbol table. Resolving
        them here is what lets a control dependency be followed like any other: ``PX-GRADE``
        is gated by ``ANNUAL-PAY``, and only a KEY says which declared field that is."""
        out: List[dict] = []
        for cond in st.conditions:
            row = dict(cond)
            keys: List[str] = []
            for name in cond.get("fields", []):
                res = self.symbols.resolve(name, self._scope)
                self._note_scoped(res)
                key = res.field.key if res.field is not None else name
                if key not in keys:
                    keys.append(key)
            if keys:
                row["fields"] = keys
            out.append(row)
        return out

    def _sink(self, key: str, kind: str, detail: dict) -> str:
        self.graph.sinks.setdefault(key, Sink(key=key, kind=kind, detail=detail))
        return key

    # -- entry -----------------------------------------------------------
    def build(self) -> LineageGraph:
        self._redefinition_aliases()
        for act in self.program.activities:
            self._scope = self._activity_scope(act)
            self._activity_header(act)
            for st in act.all_statements():
                self._statement(st, act)
        # Reports LAST, and in a second pass: a report's field references are read against
        # the record of the JOB that PRINTs it, and which JOB that is only becomes known
        # once every activity's statements have been walked.
        for act in self.program.activities:
            if act.report is not None:
                self._scope = self._report_scope(act)
                self._report(act, act.report)
        self._scope = []
        self._sort_copies()
        self._note_overlaps()
        return self.graph

    def _activity_scope(self, act: Activity) -> List[str]:
        scope = [i["file"] for i in act.inputs if i.get("file")]
        if act.sort_to:
            scope.append(act.sort_to)
        return scope

    def _report_scope(self, act: Activity) -> List[str]:
        rpt = act.report
        names = self.graph.printed_by.get(rpt.name, []) if rpt else []
        scope: List[str] = []
        for other in self.program.activities:
            if other.name not in names:
                continue
            for name in self._activity_scope(other):
                if name not in scope:
                    scope.append(name)
        return scope or self._activity_scope(act)

    # -- declarations ----------------------------------------------------
    def _redefinition_aliases(self) -> None:
        """An explicit redefinition is a declared alias: the two names are the same bytes,
        so a write through either is a write through both. Recorded in BOTH directions,
        because that is what an alias is."""
        for fd in self.program.files.values():
            for f in fd.fields:
                if not f.redefines:
                    continue
                base = fd.field_named(f.redefines)
                if base is None:
                    continue
                for a, b in ((f, base), (base, f)):
                    self._add(Edge(target=a.key, sources=[b.key], via="redefine-alias",
                                   line=f.line, origin=f.origin,
                                   statement=f.raw,
                                   note="{0} and {1} are declared over the same bytes of "
                                        "{2} - a write through either is a write through "
                                        "both".format(f.name, base.name, fd.name)))

    def _note_overlaps(self) -> None:
        for fd in self.program.files.values():
            explicit = {f.name for f in fd.fields if f.redefines}
            positional = [f.name for f in fd.fields
                          if f.name not in explicit and self._overlaps(fd, f)]
            if positional:
                self._flag(
                    "file {0}: field(s) {1} share bytes with another field of the same "
                    "record. Overlaps are listed on each field but are NOT traversed as "
                    "lineage - in Easytrieve a group and its elements overlap by design, "
                    "and traversing them would make every field depend on every "
                    "other".format(fd.name, ", ".join(sorted(positional)[:8])))

    @staticmethod
    def _overlaps(fd: FileDef, f: Field) -> bool:
        return bool(overlapping(fd, f))

    # -- activities ------------------------------------------------------
    def _activity_header(self, act: Activity) -> None:
        for inp in act.inputs:
            name = inp.get("file")
            if not name:
                continue
            self.graph.access.append(FileAccess(
                file=name, activity=act.name, io="read",
                how="{0} INPUT".format(act.kind), line=act.line,
                keys=list(inp.get("key") or [])))
        if act.kind == "SORT" and act.sort_to:
            self.graph.access.append(FileAccess(
                file=act.sort_to, activity=act.name, io="write", how="SORT TO",
                line=act.line, keys=[k["field"] for k in act.sort_keys]))

    def _sort_copies(self) -> None:
        """``SORT IN TO OUT`` copies whole records: every field OUT declares that IN also
        declares carries the same bytes across. That is a real field-level edge, and it is
        the reason a sorted work file's downstream lineage still reaches the original
        input."""
        for act in self.program.activities:
            if act.kind != "SORT" or not act.sort_to:
                continue
            src = self.program.files.get(act.inputs[0]["file"]) if act.inputs else None
            dst = self.program.files.get(act.sort_to)
            if src is None or dst is None:
                self._flag(
                    "activity {0}: SORT names a file this program does not declare, so "
                    "its record copy is not in the field lineage".format(act.name))
                continue
            keys = [k["field"] for k in act.sort_keys]
            for f in dst.fields:
                base = src.field_named(f.name)
                if base is None:
                    continue
                self._add(Edge(
                    target=f.key, sources=[base.key], via="sort-copy",
                    activity=act.name, line=act.line,
                    statement="SORT {0} TO {1}".format(src.name, dst.name),
                    note="the SORT copies the record unchanged; only the ORDER of the "
                         "records differs" + (
                             ", by {0}".format(", ".join(keys)) if keys else "")))

    # -- statements ------------------------------------------------------
    def _statement(self, st: Statement, act: Activity) -> None:
        handler = getattr(self, "_do_" + st.verb.replace("-", "_").lower(), None)
        if handler is not None:
            handler(st, act)

    def _do_assign(self, st: Statement, act: Activity) -> None:
        toks = list(st.operands)
        if len(toks) < 3 or toks[1] != "=":
            return
        target = self._resolve(toks[0], st, "assignment target")
        if target is None:
            return
        expr = toks[2:]
        sources, consts = self._refs(expr, st, "assignment source")
        operators = [t for t in expr if t in ("+", "-", "*", "/")]
        self._add(Edge(target=target.key, sources=sources, constants=consts,
                       via="assignment", operators=operators, activity=act.name,
                       proc=st.proc, line=st.line, origin=st.origin, statement=st.text,
                       conditions=self._conds(st)))

    def _do_move(self, st: Statement, act: Activity) -> None:
        toks = list(st.operands)
        if toks and toks[0].upper() == "LIKE":
            self._move_like(st, act, toks[1:])
            return
        upper = [t.upper() for t in toks]
        if "TO" in upper:
            cut = upper.index("TO")
            src_toks, dst_toks = toks[:cut], toks[cut + 1:]
        elif len(toks) >= 2:
            src_toks, dst_toks = toks[:-1], toks[-1:]
            self._flag(
                "line {0}: MOVE written without TO - the last operand was taken as the "
                "target. Verify against the source if this program uses a different "
                "MOVE form".format(st.line))
        else:
            return
        target = None
        for tok in dst_toks:
            if is_name(tok) and tok.upper() not in _NOISE:
                target = self._resolve(tok, st, "MOVE target")
                break
        if target is None:
            return
        sources, consts = self._refs(src_toks, st, "MOVE source")
        self._add(Edge(target=target.key, sources=sources, constants=consts, via="move",
                       activity=act.name, proc=st.proc, line=st.line, origin=st.origin,
                       statement=st.text, conditions=self._conds(st)))

    def _move_like(self, st: Statement, act: Activity, toks: List[str]) -> None:
        names = [t.upper() for t in toks if is_name(t)]
        if len(names) < 2:
            return
        src = self.program.files.get(names[0])
        dst = self.program.files.get(names[1])
        if src is None or dst is None:
            self._flag(
                "line {0}: MOVE LIKE names {1} - at least one is not a file this program "
                "declares, so the fields it moves are not in the lineage".format(
                    st.line, " / ".join(names[:2])))
            return
        moved = 0
        for f in dst.fields:
            base = src.field_named(f.name)
            if base is None:
                continue
            moved += 1
            self._add(Edge(target=f.key, sources=[base.key], via="move-like",
                           activity=act.name, proc=st.proc, line=st.line,
                           origin=st.origin, statement=st.text,
                           conditions=self._conds(st),
                           note="MOVE LIKE pairs fields by NAME, not by position - this "
                                "edge holds because both records declare {0}".format(
                                    f.name)))
        if not moved:
            self._flag(
                "line {0}: MOVE LIKE {1} {2} moves nothing this tool can see - the two "
                "records share no field name".format(st.line, names[0], names[1]))

    # ---- file I/O ------------------------------------------------------
    def _file_operand(self, st: Statement) -> Optional[str]:
        for tok in st.operands:
            if is_name(tok) and tok.upper() not in _NOISE:
                name = tok.upper()
                if name in self.program.files:
                    return name
                return name
        return None

    def _access(self, st: Statement, act: Activity, io: str, how: str,
                operation: Optional[str] = None, keys: Optional[List[str]] = None) -> None:
        name = self._file_operand(st)
        if not name:
            return
        if name not in self.program.files:
            self._flag(
                "line {0}: {1} names {2}, which this program does not declare as a FILE - "
                "its record layout is not in the model".format(st.line, how, name))
        self.graph.access.append(FileAccess(
            file=name, activity=act.name, io=io, how=how, line=st.line,
            operation=operation, keys=keys or [], conditions=self._conds(st)))

    def _do_get(self, st: Statement, act: Activity) -> None:
        self._access(st, act, "read", "GET")

    def _do_read(self, st: Statement, act: Activity) -> None:
        self._access(st, act, "read", "READ")

    def _do_retrieve(self, st: Statement, act: Activity) -> None:
        self._access(st, act, "read", "RETRIEVE")

    def _do_select(self, st: Statement, act: Activity) -> None:
        self._access(st, act, "read", "SELECT")

    def _do_point(self, st: Statement, act: Activity) -> None:
        """``POINT file EQ key`` positions a keyed file. The key field decides WHICH record
        the following GET returns, so every field of that file depends on it."""
        name = self._file_operand(st)
        rest = [t for t in st.operands if t.upper() != (name or "")]
        keys, _ = self._refs(rest, st, "POINT key")
        self._access(st, act, "read", "POINT", keys=keys)
        fd = self.program.files.get(name or "")
        if fd is None or not keys:
            return
        for f in fd.fields:
            self._add(Edge(target=f.key, sources=list(keys), via="positioned-by",
                           activity=act.name, proc=st.proc, line=st.line,
                           origin=st.origin, statement=st.text,
                           conditions=self._conds(st),
                           note="POINT selects WHICH record of {0} is read; the key "
                                "decides which value this field takes, not what the value "
                                "is computed from".format(fd.name)))

    def _do_put(self, st: Statement, act: Activity) -> None:
        self._access(st, act, "write", "PUT")
        self._record_copy(st, act, "PUT")

    def _do_write(self, st: Statement, act: Activity) -> None:
        op = next((t.upper() for t in st.operands
                   if t.upper() in ("ADD", "UPDATE", "DELETE")), None)
        io = "update" if op in ("UPDATE", "DELETE") else "write"
        self._access(st, act, io, "WRITE", operation=op)
        self._record_copy(st, act, "WRITE")

    def _record_copy(self, st: Statement, act: Activity, how: str) -> None:
        """``PUT out FROM area`` writes ``area``'s bytes, not the fields assigned into
        ``out``. Recorded at the RECORD level and flagged, because the mapping is
        positional and this tool does not have the two layouts' byte maps aligned."""
        upper = [t.upper() for t in st.operands]
        if "FROM" not in upper:
            return
        src = next((t.upper() for t in st.operands[upper.index("FROM") + 1:]
                    if is_name(t)), None)
        dst = self._file_operand(st)
        if not src or not dst:
            return
        self._flag(
            "line {0}: {1} {2} FROM {3} writes {3}'s bytes wholesale. The per-field "
            "mapping is POSITIONAL and is not in the field lineage - only the record-level "
            "edge is".format(st.line, how, dst, src))
        target_file = self.program.files.get(dst)
        source_file = self.program.files.get(src)
        if target_file is None or source_file is None:
            return
        for f in target_file.fields:
            base = next((b for b in source_file.fields
                         if b.start == f.start and b.length == f.length), None)
            if base is None:
                continue
            self._add(Edge(target=f.key, sources=[base.key], via="record-copy",
                           activity=act.name, proc=st.proc, line=st.line,
                           origin=st.origin, statement=st.text,
                           conditions=self._conds(st),
                           note="matched on IDENTICAL byte position and length between "
                                "{0} and {1}; fields that do not line up exactly are not "
                                "paired".format(src, dst)))

    def _do_close(self, st: Statement, act: Activity) -> None:
        return

    def _do_print(self, st: Statement, act: Activity) -> None:
        name = next((t.upper() for t in st.operands if is_name(t)), None)
        if name:
            self.graph.printed_by.setdefault(name, [])
            if act.name not in self.graph.printed_by[name]:
                self.graph.printed_by[name].append(act.name)

    def _do_display(self, st: Statement, act: Activity) -> None:
        toks = list(st.operands)
        target_file = None
        if toks and is_name(toks[0]) and toks[0].upper() in self.program.files:
            target_file = toks[0].upper()
            toks = toks[1:]
            self.graph.access.append(FileAccess(
                file=target_file, activity=act.name, io="write", how="DISPLAY",
                line=st.line, conditions=self._conds(st)))
        sources, consts = self._refs(toks, st, "DISPLAY item")
        if not sources and not consts:
            return
        key = self._sink(
            "@DISPLAY.{0}.{1}".format(target_file or "SYSPRINT", st.line), "display",
            {"file": target_file, "line": st.line, "activity": act.name})
        self._add(Edge(target=key, sources=sources, constants=consts, via="display",
                       activity=act.name, proc=st.proc, line=st.line, origin=st.origin,
                       statement=st.text, conditions=self._conds(st)))

    def _do_search(self, st: Statement, act: Activity) -> None:
        """``SEARCH table WITH arg GIVING result`` - a table lookup.

        The result comes from the table's description field(s); WHICH row is chosen comes
        from the argument. Both are dependencies and they are different in kind, so the
        argument is carried as the row's search key rather than pretended to be the value's
        source."""
        upper = [t.upper() for t in st.operands]
        table = next((t.upper() for t in st.operands if is_name(t)), None)
        arg_toks = self._between(st.operands, upper, "WITH", "GIVING")
        out_toks = self._after(st.operands, upper, "GIVING")
        fd = self.program.files.get(table or "")
        target = None
        for tok in out_toks:
            if is_name(tok):
                target = self._resolve(tok, st, "SEARCH result")
                break
        if target is None:
            return
        arg_keys, arg_consts = self._refs(arg_toks, st, "SEARCH argument")
        if fd is not None:
            self.graph.access.append(FileAccess(
                file=fd.name, activity=act.name, io="read", how="SEARCH", line=st.line,
                keys=list(arg_keys), conditions=self._conds(st)))
        if fd is None:
            self._flag(
                "line {0}: SEARCH names {1}, which this program does not declare as a "
                "TABLE file - the values it returns are not in the model".format(
                    st.line, table))
            self._add(Edge(target=target.key, sources=arg_keys, constants=arg_consts,
                           via="table-lookup", activity=act.name, proc=st.proc,
                           line=st.line, origin=st.origin, statement=st.text,
                           conditions=self._conds(st),
                           note="the table itself is not declared here, so only the "
                                "search argument is in this edge"))
            return
        described = [f.key for f in fd.fields[1:]] or [f.key for f in fd.fields]
        if len(fd.fields) < 2:
            self._flag(
                "line {0}: table {1} declares fewer than two fields, so which one SEARCH "
                "returns cannot be told apart from the argument".format(st.line, fd.name))
        self._add(Edge(target=target.key, sources=described + arg_keys,
                       constants=arg_consts, via="table-lookup", activity=act.name,
                       proc=st.proc, line=st.line, origin=st.origin, statement=st.text,
                       conditions=self._conds(st),
                       note="the value comes from {0}'s description field(s); the "
                            "argument decides which row".format(fd.name)))

    def _do_sql(self, st: Statement, act: Activity) -> None:
        """Embedded SQL. Table names are extracted; COLUMN-level lineage is not modelled,
        and the host fields say so rather than reporting an empty origin."""
        toks = list(st.operands)
        upper = [t.upper() for t in toks]
        tables: List[str] = []
        for kw in ("FROM", "INTO", "UPDATE", "JOIN"):
            for i, t in enumerate(upper):
                if t == kw and i + 1 < len(toks) \
                        and not toks[i + 1].startswith(":") \
                        and _SQL_NAME.match(toks[i + 1]):
                    tables.append(toks[i + 1].upper())
        hosts = [t.lstrip(":") for t in toks if t.startswith(":")]
        row = {"activity": act.name, "line": st.line, "statement": st.text,
               "tables": sorted(set(tables)), "hostFields": hosts}
        self.graph.sql.append(row)
        for host in hosts:
            fld = self._resolve(host, st, "SQL host field")
            if fld is None:
                continue
            self.graph.opaque.setdefault(fld.key, []).append(
                "set by SQL at line {0} from {1} - column-level lineage inside the "
                "statement is not modelled".format(
                    st.line, ", ".join(row["tables"]) or "an unnamed table"))
        self._flag(
            "line {0}: an SQL statement is present. Table names are recorded; the "
            "COLUMN-to-host-field mapping inside it is NOT modelled, so host fields it "
            "sets show their origin as the table, not as a column".format(st.line))

    def _do_call(self, st: Statement, act: Activity) -> None:
        toks = list(st.operands)
        name = next((t.upper() for t in toks if is_name(t)), None)
        if not name:
            return
        upper = [t.upper() for t in toks]
        arg_toks = self._after(toks, upper, "USING") if "USING" in upper else toks[1:]
        args, _ = self._refs(arg_toks, st, "CALL argument")
        self.graph.calls.append(CallSite(program=name, activity=act.name, line=st.line,
                                         arguments=args,
                                         conditions=self._conds(st)))
        for key in args:
            self.graph.opaque.setdefault(key, []).append(
                "passed to CALL {0} at line {1}, which may change it".format(
                    name, st.line))

    def _do_link(self, st: Statement, act: Activity) -> None:
        self._do_call(st, act)

    def _do_perform(self, st: Statement, act: Activity) -> None:
        return          # the procedure's own statements are already walked

    # ---- report --------------------------------------------------------
    def _report(self, act: Activity, rpt: ReportDef) -> None:
        def link(key: str, kind: str, detail: dict, field_name: str,
                 note: Optional[str] = None) -> None:
            fld = self._resolve(field_name, None, "report " + kind)
            if fld is None:
                return
            self._sink(key, kind, detail)
            self._add(Edge(target=key, sources=[fld.key], via=kind, activity=act.name,
                           line=rpt.line, statement="REPORT {0}".format(rpt.name),
                           note=note))

        for entry in rpt.sequence:
            link("@{0}.SEQUENCE.{1}".format(rpt.name, entry["field"]),
                 "report-sequence",
                 {"report": rpt.name, "field": entry["field"],
                  "descending": entry["descending"]},
                 entry["field"],
                 "SEQUENCE decides the ORDER of the report, not the value of any column")
        for entry in rpt.control:
            if entry["field"] == "FINAL":
                continue
            link("@{0}.CONTROL.{1}".format(rpt.name, entry["field"]),
                 "report-control",
                 {"report": rpt.name, "field": entry["field"],
                  "options": entry["options"]},
                 entry["field"],
                 "a CONTROL break groups the report and resets its totals; a change in "
                 "this field is what ends a group")
        for name in rpt.sums:
            link("@{0}.SUM.{1}".format(rpt.name, name), "report-sum",
                 {"report": rpt.name, "field": name}, name,
                 "accumulated across every record the report receives")
        for title in rpt.titles:
            for i, item in enumerate(title["items"], start=1):
                if item["kind"] == "field":
                    link("@{0}.TITLE{1}.{2}".format(rpt.name, title["index"], i),
                         "report-title",
                         {"report": rpt.name, "title": title["index"], "column": i},
                         item["field"])
        for rline in rpt.lines:
            column = 0
            for item in rline.items:
                if item["kind"] != "field":
                    continue
                column += 1
                link("@{0}.LINE{1}.{2}".format(rpt.name, rline.index, column),
                     "report-detail",
                     {"report": rpt.name, "line": rline.index, "column": column,
                      "heading": rpt.headings.get(item["field"])},
                     item["field"])

    # ---- token slicing -------------------------------------------------
    @staticmethod
    def _between(toks: Sequence[str], upper: Sequence[str], start: str,
                 end: str) -> List[str]:
        if start not in upper:
            return []
        i = list(upper).index(start) + 1
        j = list(upper).index(end) if end in upper else len(toks)
        return list(toks[i:max(i, j)])

    @staticmethod
    def _after(toks: Sequence[str], upper: Sequence[str], word: str) -> List[str]:
        if word not in upper:
            return []
        return list(toks[list(upper).index(word) + 1:])


def overlapping(fd: FileDef, f: Field) -> List[str]:
    """Other fields of ``fd`` whose bytes intersect ``f``'s, nearest first."""
    if f.start is None or f.end is None:
        return []
    out = []
    for other in fd.fields:
        if other is f or other.start is None or other.end is None:
            continue
        if other.start <= f.end and f.start <= other.end:
            out.append(other.name)
    return sorted(set(out))


# --------------------------------------------------------------------------- #
# end-to-end flow
# --------------------------------------------------------------------------- #

@dataclass
class Path:
    """One route from an origin to a sink, in the order the data travels."""
    origin: Optional[str]                            # field key, or None for constants-only
    hops: List[dict] = dc_field(default_factory=list)
    constants: List[str] = dc_field(default_factory=list)
    influenced_by: List[str] = dc_field(default_factory=list)
    truncated: bool = False
    cyclic: bool = False
    opaque: List[str] = dc_field(default_factory=list)


class FlowWalker:
    """Walks the edge graph backwards from a sink to the inputs it derives from.

    The walk stops at an *origin*: a field of a file the program READS (the program's own
    boundary - anything further back is another program's business), or a field nothing in
    this program writes (which is itself worth reporting: an unwritten working-storage
    field reaching an output is a bug or an unmodelled verb).
    """

    def __init__(self, graph: LineageGraph, *, max_paths: int = MAX_PATHS,
                 max_depth: int = MAX_DEPTH):
        self.graph = graph
        self.incoming = graph.incoming()
        self.max_paths = max_paths
        self.max_depth = max_depth
        self.io = graph.file_io()
        self.by_key: Dict[str, Field] = graph.field_index()
        # Every sink is walked at least twice - once for the field-level flow and once for
        # the file-level aggregation - and the walk is the expensive part. Nothing mutates
        # a returned path, so one walk per key is enough.
        self._cache: Dict[str, List["Path"]] = {}
        #: The keys whose walk stopped at ``max_paths`` with edges or sources still unwalked,
        #: so the paths returned for them may not reach every end.
        self.cut: Set[str] = set()
        self._cutting = False

    def is_origin(self, key: str) -> bool:
        """A field the walk stops at.

        The boundary is a file this program only READS: what put the bytes there is another
        program's business. A file it reads AND writes is not a boundary - a sort work file
        is read by the report and written by the sort, and stopping there would end every
        chain one hop short of the input it actually came from. Anything nothing in this
        program writes is also an origin, which is worth reporting in its own right: an
        unwritten working-storage field reaching an output is a bug, or an unmodelled verb.
        """
        fld = self.by_key.get(key)
        if fld is None:
            return False
        if fld.owner and self.io.get(fld.owner) == "read":
            return True
        return key not in self.incoming

    def walk(self, sink: str) -> List[Path]:
        cached = self._cache.get(sink)
        if cached is None:
            cached = []
            self._cutting = False
            self._walk(sink, [], set(), cached, [])
            self._cache[sink] = cached
            if self._cutting:
                self.cut.add(sink)
        return cached

    def _walk(self, key: str, hops: List[dict], on_path: Set[str],
              out: List[Path], influences: List[str]) -> None:
        if len(out) >= self.max_paths:
            self._cutting = True
            return
        edges = self.incoming.get(key, [])
        if not edges:
            out.append(self._path(key, hops, influences, [], cyclic=False))
            return
        for ei, edge in enumerate(edges):
            hop = {"target": edge.target, "via": edge.via, "activity": edge.activity,
                   "line": edge.line, "statement": edge.statement}
            for k, v in (("proc", edge.proc), ("origin", edge.origin),
                         ("operators", edge.operators or None), ("note", edge.note),
                         ("accumulates", edge.accumulates or None)):
                if v:
                    hop[k] = v
            if edge.conditions:
                hop["conditions"] = edge.conditions
            new_influences = influences + [
                f for c in edge.conditions for f in c.get("fields", [])]
            here = [hop] + hops
            if not edge.sources:
                out.append(self._path(None, here, new_influences, edge.constants))
                continue
            for si, src in enumerate(edge.sources):
                # `key` itself counts as on-path: an accumulator (``N = N + 1``) is its own
                # source, and detecting that only on the next recursion would report the
                # same statement twice in the chain.
                if src == key or src in on_path:
                    p = self._path(src, here, new_influences, edge.constants, cyclic=True)
                    out.append(p)
                    continue
                if len(here) >= self.max_depth:
                    p = self._path(src, here, new_influences, edge.constants)
                    p.truncated = True
                    out.append(p)
                    continue
                if self.is_origin(src):
                    out.append(self._path(src, here, new_influences, edge.constants))
                    continue
                self._walk(src, here, on_path | {key}, out, new_influences)
                if len(out) >= self.max_paths:
                    if si + 1 < len(edge.sources) or ei + 1 < len(edges):
                        self._cutting = True
                    return

    def _path(self, origin: Optional[str], hops: List[dict], influences: List[str],
              constants: List[str], cyclic: bool = False) -> Path:
        opaque: List[str] = []
        for hop in hops:
            for reason in self.graph.opaque.get(hop["target"], []):
                if reason not in opaque:
                    opaque.append(reason)
        if origin:
            opaque.extend(r for r in self.graph.opaque.get(origin, [])
                          if r not in opaque)
        return Path(origin=origin, hops=list(hops),
                    constants=[literal_value(c) for c in constants],
                    influenced_by=_dedupe(influences), cyclic=cyclic, opaque=opaque)


def _dedupe(items: Iterable[str]) -> List[str]:
    seen: Dict[str, None] = {}
    for i in items:
        seen.setdefault(i, None)
    return list(seen)

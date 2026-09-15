"""Two views over a parsed Easytrieve program, plus the join that resolves its ddnames.

  * ``build_eztrieve_lineage(program)`` - the record layouts (every field with its byte
    range), the statement-level field edges, the **end-to-end field flow** from an input
    file's bytes to an output file's bytes or a report column, and the file-to-file
    dataflow those edges add up to.

  * ``build_eztrieve_artifacts(program)`` - the dependency manifest in the SAME shape as
    the COBOL and JCL manifests: one row per related artifact - files (by ddname), macro
    members, called programs, file exits, SQL tables - each tagged ``dependency`` runtime /
    compile-time, with the identity and resolution honesty the siblings already carry.

  * ``bind_jcl_ddnames(manifest, lineage)`` - the join. An Easytrieve ``FILE PERSNL`` names
    a **ddname**, and only the JCL says which dataset that is. Given a JCL lineage view
    (a plain dict, so this package imports nothing from the JCL one) the file rows gain
    their datasets and the identity chain closes.

Both views are pure reads; they invent nothing. What the parser or the lineage walk could
not resolve is already on the program or the graph and is carried through here rather than
papered over.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from mainframe_artifacts.dependents import output_rows
from mainframe_artifacts.synonyms import FROM_MAP, SynonymLookup

from . import VIEW_SCHEMA_VERSION
from .lineage import FlowWalker, LineageGraph, Path, build_graph, overlapping
from .model import FORMATS, STORAGE_FILE, Field, FileDef, Program

#: Which JCL ddnames a run of Easytrieve needs that the PROGRAM never names, so a reader
#: is not left wondering why they are absent from the manifest.
_RUNTIME_DDNAMES = ("SYSIN (the program source itself)", "SYSPRINT", "SYSSNAP")


# --------------------------------------------------------------------------- #
# shared shaping
# --------------------------------------------------------------------------- #

def _field_row(fld: Field, fd: Optional[FileDef]) -> dict:
    row: dict = {"field": fld.name}
    if fld.bytes_text:
        row["bytes"] = fld.bytes_text
        row["start"] = fld.start
    if fld.length is not None:
        row["length"] = fld.length
    if fld.fmt:
        row["format"] = fld.fmt
        row["formatMeans"] = FORMATS.get(fld.fmt, "not a format this tool knows")
    if fld.decimals is not None:
        row["decimals"] = fld.decimals
    if fld.storage != STORAGE_FILE:
        row["storage"] = fld.storage
    if fld.redefines:
        row["redefines"] = fld.redefines
    if fld.occurs:
        row["occurs"] = fld.occurs
    if fld.index:
        row["index"] = list(fld.index)
    if fld.value is not None:
        row["value"] = fld.value
    if fld.mask:
        row["mask"] = fld.mask
    if fld.heading:
        row["heading"] = list(fld.heading)
    if fd is not None:
        overlaps = overlapping(fd, fld)
        if overlaps:
            row["overlaps"] = overlaps
    if fld.origin:
        row["definedInMacro"] = fld.origin
    row["line"] = fld.line
    if fld.flags:
        row["flags"] = list(fld.flags)
    return row


def _field_descriptor(key: str, graph: LineageGraph) -> dict:
    """A field key -> the reader-facing description of that field, bytes included."""
    f = graph.field_index().get(key)
    if f is None:
        return {"field": key}
    d: dict = {"field": f.name}
    if f.owner:
        d["file"] = f.owner
    else:
        d["storage"] = f.storage
    if f.bytes_text:
        d["bytes"] = f.bytes_text
    if f.length is not None:
        d["length"] = f.length
    if f.fmt:
        d["format"] = f.fmt
    if f.decimals is not None:
        d["decimals"] = f.decimals
    return d


def _conditions_text(conditions: Sequence[dict]) -> List[dict]:
    out = []
    for c in conditions:
        row = {"kind": c.get("kind"), "test": c.get("test"),
               "negated": bool(c.get("negated"))}
        if c.get("afterFailing"):
            row["afterFailing"] = list(c["afterFailing"])
        if c.get("fields"):
            row["fields"] = list(c["fields"])
        out.append(row)
    return out


# --------------------------------------------------------------------------- #
# lineage
# --------------------------------------------------------------------------- #

def build_eztrieve_lineage(program: Program, *, graph: Optional[LineageGraph] = None
                           ) -> dict:
    """Record layouts, field-to-field edges, end-to-end field flow and file dataflow."""
    graph = graph or build_graph(program)
    io = graph.file_io()

    files_out: List[dict] = []
    for name in sorted(program.files):
        fd = program.files[name]
        row: dict = {"file": name, "io": io.get(name, "unknown")}
        for key, val in (("organization", fd.organization), ("device", fd.device),
                         ("recfm", fd.recfm), ("recordLength", fd.record_length),
                         ("blockSize", fd.block_size)):
            if val:
                row[key] = val
        if fd.usage:
            row["usage"] = list(fd.usage)
        if fd.key_fields:
            row["keyFields"] = list(fd.key_fields)
        if fd.exit_program:
            row["exit"] = fd.exit_program
        if fd.copied_from:
            row["copiedFrom"] = fd.copied_from
        if fd.instream:
            row["instream"] = True
            row["instreamRows"] = len(fd.table_data)
        if fd.virtual:
            row["virtual"] = True
        if fd.origin:
            row["declaredInMacro"] = fd.origin
        declared = max((f.end or 0) for f in fd.fields) if fd.fields else 0
        if declared:
            row["declaredThroughByte"] = declared
        row["record"] = [_field_row(f, fd) for f in fd.fields]
        row["line"] = fd.line
        if fd.flags:
            row["flags"] = list(fd.flags)
        files_out.append(row)

    working = [_field_row(f, None) for f in program.working]

    activities: List[dict] = []
    for act in program.activities:
        arow: dict = {"activity": act.name, "kind": act.kind, "line": act.line}
        if act.inputs:
            arow["inputs"] = [
                {"file": i.get("file"), **({"key": i["key"]} if i.get("key") else {}),
                 **({"null": True} if i.get("null") else {})}
                for i in act.inputs]
        if act.sort_to:
            arow["sortTo"] = act.sort_to
            arow["sortKeys"] = list(act.sort_keys)
        for key, val in (("startProc", act.start_proc), ("finishProc", act.finish_proc),
                         ("beforeProc", act.before_proc)):
            if val:
                arow[key] = val
        if act.procs or (act.report and act.report.procs):
            arow["procedures"] = [p.name for p in act.procs] + \
                                 [p.name for p in (act.report.procs if act.report else [])]
        arow["statements"] = len(act.all_statements())
        if act.report is not None:
            arow["report"] = act.report.name
        if act.origin:
            arow["definedInMacro"] = act.origin
        if act.flags:
            arow["flags"] = list(act.flags)
        activities.append(arow)

    edges_out: List[dict] = []
    field_keys = graph.field_index()
    for e in graph.edges:
        row: dict = {"target": (_field_descriptor(e.target, graph)
                                if e.target in field_keys else {"sink": e.target}),
                     "targetKey": e.target,
                     "via": e.via}
        if e.sources:
            row["sources"] = [_field_descriptor(s, graph) for s in e.sources]
        if e.constants:
            row["constants"] = list(e.constants)
        if e.operators:
            row["operators"] = list(e.operators)
        if e.accumulates:
            row["accumulates"] = True
        row["activity"] = e.activity
        if e.proc:
            row["procedure"] = e.proc
        row["line"] = e.line
        if e.origin:
            row["definedInMacro"] = e.origin
        if e.statement:
            row["statement"] = e.statement
        if e.conditions:
            row["conditions"] = _conditions_text(e.conditions)
        if e.note:
            row["note"] = e.note
        edges_out.append(row)

    # One walker for both passes: the backward walk is the expensive part and it caches
    # per sink, so the file-level aggregation costs nothing beyond the field-level one.
    walker = FlowWalker(graph)
    flows = _field_flow(graph, io, walker)
    file_flow = _file_flow(graph, io, walker)

    reports_out: List[dict] = []
    for act in program.activities:
        rpt = act.report
        if rpt is None:
            continue
        rrow: dict = {"report": rpt.name, "activity": act.name,
                      "printedBy": sorted(graph.printed_by.get(rpt.name, []))}
        if rpt.summary:
            rrow["summary"] = True
        for key, val in (("printer", rpt.printer), ("sumFile", rpt.sumfile)):
            if val:
                rrow[key] = val
        # The layout operands as coded, in a fixed order. No print position is published:
        # it also needs each column's edited width (type, decimals, mask), its heading
        # width, centring and site-option defaults, none of which is settled here.
        for key in ("linesize", "space", "spread", "nospread", "noadjust"):
            if key in rpt.layout:
                rrow[key] = rpt.layout[key]
        if rpt.sequence:
            rrow["sequence"] = list(rpt.sequence)
        if rpt.control:
            rrow["control"] = list(rpt.control)
        if rpt.sums:
            rrow["sums"] = list(rpt.sums)
        if rpt.titles:
            rrow["titles"] = list(rpt.titles)
        if rpt.headings:
            rrow["headings"] = {k: list(v) for k, v in sorted(rpt.headings.items())}
        rrow["lines"] = [{"index": ln.index, "items": ln.items} for ln in rpt.lines]
        if not rrow["printedBy"]:
            rrow["note"] = ("no activity PRINTs this report - it produces no output unless "
                            "something outside this program does")
        reports_out.append(rrow)

    access_out = [
        {"file": a.file, "activity": a.activity, "io": a.io, "how": a.how,
         "line": a.line,
         **({"keys": a.keys} if a.keys else {}),
         **({"operation": a.operation} if a.operation else {}),
         **({"conditions": _conditions_text(a.conditions)} if a.conditions else {})}
        for a in graph.access]

    return {
        "format": "eztrieve-dependencies-lineage",
        "formatVersion": VIEW_SCHEMA_VERSION,
        "program": program.name,
        "source": program.source_name,
        "note": (
            "Field-level lineage for one Easytrieve program. 'files' is the declared record "
            "layout: Easytrieve gives every field an explicit byte position and length, so "
            "each row's 'bytes' is a fact about the record, not a computation. "
            "'fieldLineage' is one row per statement that made a field depend on other "
            "fields - assignment, MOVE, MOVE LIKE, table lookup, sort copy, an explicit "
            "redefinition, or a report reference - carrying the IF / CASE / DO conditions "
            "that gate it. 'fieldFlow' is the end-to-end answer no single statement holds: "
            "for each sink (a field of a file this program writes, a report column, a "
            "DISPLAY) the chain back through working storage to the INPUT-file bytes it "
            "derives from. 'influencedBy' on a path is kept SEPARATE from its value "
            "sources: a field assigned a literal inside an IF has no value source but is "
            "still decided by the field the IF tests. 'fileFlow' is the same edges "
            "aggregated to ddname level. Nothing is invented - a reference this tool could "
            "not resolve is in 'unresolved', and everything it declined to model is in "
            "'flags'. A FILE name IS a ddname: this view says which bytes move where, and "
            "the JCL says which dataset each ddname is - bind_jcl_ddnames closes that."
        ),
        "files": files_out,
        "workingStorage": working,
        "activities": activities,
        "fileFlow": file_flow,
        "fieldLineage": edges_out,
        "fieldFlow": flows,
        "reports": reports_out,
        "fileAccess": access_out,
        "calls": [{"program": c.program, "activity": c.activity, "line": c.line,
                   "arguments": [_field_descriptor(a, graph) for a in c.arguments],
                   **({"conditions": _conditions_text(c.conditions)}
                      if c.conditions else {})}
                  for c in graph.calls],
        "sql": list(graph.sql),
        "unresolved": list(graph.unresolved),
        "flags": list(program.flags) + [f for f in graph.flags if f not in program.flags],
    }


def _sinks(graph: LineageGraph, io: Dict[str, str]) -> List[Tuple[str, dict]]:
    """Every node worth walking back from, in a stable order: the fields of the files this
    program writes, then the report columns and DISPLAY lines."""
    out: List[Tuple[str, dict]] = []
    for name in sorted(graph.program.files):
        if io.get(name) not in ("write", "read-write", "update"):
            continue
        for fld in graph.program.files[name].fields:
            out.append((fld.key, {"kind": "file-field", "file": name,
                                  **_field_descriptor(fld.key, graph)}))
    for key in sorted(graph.sinks):
        sink = graph.sinks[key]
        out.append((key, {"kind": sink.kind, **sink.detail}))
    return out


def _influence_rows(keys: Sequence[str], graph: LineageGraph, walker: FlowWalker,
                    cache: Dict[str, List[str]]) -> List[dict]:
    """The gating fields, each traced back to the inputs it itself derives from.

    ``PX-GRADE`` is only ever assigned a literal, so it has no value origin - but which
    literal is chosen is decided by ``ANNUAL-PAY``, which came from ``GROSS``. Naming only
    the immediate gate would stop one hop short of the fact a reader is after, so each
    influencing field carries ``tracesTo``."""
    out: List[dict] = []
    for key in keys:
        row = dict(_field_descriptor(key, graph))
        if key not in cache:
            if walker.is_origin(key):
                cache[key] = []
            else:
                seen: List[str] = []
                for path in walker.walk(key):
                    if path.origin and path.origin != key and path.origin not in seen:
                        seen.append(path.origin)
                cache[key] = seen
        if cache[key]:
            row["tracesTo"] = [_field_descriptor(k, graph) for k in cache[key]]
        out.append(row)
    return out


def _field_flow(graph: LineageGraph, io: Dict[str, str],
                walker: Optional[FlowWalker] = None) -> List[dict]:
    """For each sink, the chains back to the input-file fields it derives from."""
    walker = walker or FlowWalker(graph)
    incoming = graph.incoming()
    influence_cache: Dict[str, List[str]] = {}
    # The references each statement dropped because they resolved to no field, keyed the
    # way a hop names its statement. An edge built from one has no source and no constant
    # for it, so a sink ending there is NOT "built from constants".
    dropped: Dict[Tuple[str, int, str], List[str]] = {}
    for u in graph.unresolved:
        if "line" in u:
            refs = dropped.setdefault((u["activity"], u["line"], u["statement"]), [])
            if u["reference"] not in refs:
                refs.append(u["reference"])
    rows: List[dict] = []
    for key, descriptor in _sinks(graph, io):
        if key not in incoming:
            continue
        paths = walker.walk(key)
        origins: List[dict] = []
        constants: List[str] = []
        influenced: List[str] = []
        notes: List[str] = []
        for path in paths:
            for c in path.constants:
                if c not in constants:
                    constants.append(c)
            for f in path.influenced_by:
                if f not in influenced:
                    influenced.append(f)
            if path.origin is None:
                continue
            row: dict = {"origin": _field_descriptor(path.origin, graph),
                         "originKey": path.origin,
                         "path": path.hops}
            if path.constants:
                row["constants"] = list(path.constants)
            if path.influenced_by:
                row["influencedBy"] = list(path.influenced_by)
            if path.opaque:
                row["opaque"] = list(path.opaque)
            if path.cyclic:
                row["cyclic"] = True
                row["note"] = ("this path revisits a field it already passed through - an "
                               "accumulator or a loop; the chain is reported once, not "
                               "unrolled")
            if path.truncated:
                row["truncated"] = True
                notes.append("a path was longer than the walk's depth bound and was cut")
            origins.append(row)
        row = {"sink": descriptor, "sinkKey": key, "origins": origins}
        if constants:
            row["constants"] = constants
        if influenced:
            row["influencedBy"] = _influence_rows(influenced, graph, walker,
                                                  influence_cache)
        if not origins:
            _no_origin_reason(row, paths, influenced, dropped,
                              walker.max_paths if key in walker.cut else None)
            row["note"] = ("nothing in this program traces back to an input file: this "
                           "sink is built entirely from constants, from the conditions "
                           "listed in 'influencedBy', or from a statement this tool does "
                           "not model (see 'flags')")
        elif notes:
            row["note"] = "; ".join(sorted(set(notes)))
        rows.append(row)
    return rows


def _no_origin_reason(row: dict, paths: Sequence[Path], influenced: List[str],
                      dropped: Dict[Tuple[str, int, str], List[str]],
                      path_limit: Optional[int]) -> None:
    """Which of the three causes the no-origin note names, as a closed value.

    Every path of a sink with no origins ends at an edge that has no field source, so the
    edges at those ends are the whole story. ``unmodelled`` when this tool cannot vouch
    for it - an end dropped a reference it could not resolve, or recorded neither a field
    nor a constant, or the walk stopped at its path limit (``path_limit``) with ends unseen;
    otherwise ``conditions`` when a tested field decides which constant is chosen, and
    ``constants`` when nothing does. ``unmodelledAt`` names the statements behind an
    ``unmodelled`` answer, so the category comes with a lead."""
    at: List[dict] = []
    for path in paths:
        end = path.hops[0]
        refs = dropped.get((end["activity"], end["line"], end["statement"]), [])
        if refs or not path.constants:
            site = {"activity": end["activity"], "line": end["line"],
                    "statement": end["statement"]}
            if refs:
                site["unresolved"] = list(refs)
            if site not in at:
                at.append(site)
    if at or path_limit is not None:
        row["noOriginReason"] = "unmodelled"
        if at:
            row["unmodelledAt"] = at
        if path_limit is not None:
            row["pathLimit"] = path_limit
    elif influenced:
        row["noOriginReason"] = "conditions"
    else:
        row["noOriginReason"] = "constants"


def _file_flow(graph: LineageGraph, io: Dict[str, str],
               walker: Optional[FlowWalker] = None) -> List[dict]:
    """The field edges aggregated to ddname level: which file's bytes reach which other's."""
    owners = {key: f.owner for key, f in graph.field_index().items()}
    walker = walker or FlowWalker(graph)
    pairs: Dict[Tuple[str, str], dict] = {}
    for key, _ in _sinks(graph, io):
        target_file = owners.get(key)
        if not target_file:
            continue
        for path in walker.walk(key):
            src_file = owners.get(path.origin or "")
            if not src_file or src_file == target_file:
                continue
            rec = pairs.setdefault((src_file, target_file), {
                "from": src_file, "to": target_file, "fields": 0, "activities": []})
            rec["fields"] += 1
            for hop in path.hops:
                if hop["activity"] and hop["activity"] not in rec["activities"]:
                    rec["activities"].append(hop["activity"])
    out = []
    for (src, dst), rec in sorted(pairs.items()):
        rec["activities"] = sorted(rec["activities"])
        out.append(rec)
    return out


# --------------------------------------------------------------------------- #
# artifacts
# --------------------------------------------------------------------------- #

_CLASS_ORDER = {"file": 0, "db2-table": 1, "program": 2, "macro": 3}


def build_eztrieve_artifacts(program: Program, *,
                             graph: Optional[LineageGraph] = None,
                             synonyms: Optional[SynonymLookup] = None) -> dict:
    """The related-artifact manifest, mirroring the COBOL and JCL manifests.

    ``synonyms`` is the Db2 catalog's SYNONYM/ALIAS knowledge, supplied as input
    (``mainframe_artifacts.synonyms.SynonymLookup``: a map, a host resolver, or both).
    Every ``db2-table`` row is asked of it - a table name is this view's whole
    statement about the table, so every one is the point of need - and a row written
    under a synonym gains ``baseTable``. The name as written stays the artifact.
    """
    graph = graph or build_graph(program)
    io = graph.file_io()

    artifacts: List[dict] = []
    excluded: List[dict] = []

    touched: Dict[str, List[dict]] = {}
    for a in graph.access:
        touched.setdefault(a.file, []).append(
            {"activity": a.activity, "how": a.how, "line": a.line,
             **({"operation": a.operation} if a.operation else {}),
             **({"conditional": True} if a.conditions else {})})

    for name in sorted(program.files):
        fd = program.files[name]
        if fd.virtual:
            excluded.append({"name": name, "kind": "virtual-file",
                             "reason": "VIRTUAL - an in-storage work area, not a dataset "
                                       "and not a ddname the JCL allocates"})
            continue
        if fd.instream:
            excluded.append({"name": name, "kind": "instream-table",
                             "reason": "TABLE INSTREAM - its {0} row(s) are in this "
                                       "source member, so there is nothing to "
                                       "retrieve".format(len(fd.table_data))})
            continue
        row: dict = {
            "artifact": name,
            "kind": "file",
            "dependency": "runtime",
            "io": io.get(name, "unknown"),
            # The join to the JCL. This is the whole reason the manifest carries a ddname
            # in the same field name the COBOL manifest uses: an Easytrieve FILE name IS
            # a ddname, so the same binder closes both.
            "ddname": name,
            "identity": "program-local",
            "resolvedBy": "JCL DD statement",
            "needs": ("the JCL DD statement that binds this ddname to a dataset - the "
                      "program names only the ddname, so the DSN is not knowable from "
                      "this source alone"),
            "fields": len(fd.fields),
            "touchedBy": touched.get(name, []),
        }
        for key, val in (("organization", fd.organization), ("device", fd.device),
                         ("recfm", fd.recfm), ("recordLength", fd.record_length)):
            if val:
                row[key] = val
        if fd.usage:
            row["usage"] = list(fd.usage)
        if io.get(name) == "unknown":
            row["directionUnknown"] = True
            row["note"] = ("declared but never read or written by any statement or "
                           "activity header this tool models - the ddname must still be "
                           "allocated")
        if fd.origin:
            row["declaredInMacro"] = fd.origin
        artifacts.append(row)

    # SQL tables named in embedded statements.
    tables: Dict[str, List[dict]] = {}
    for row in graph.sql:
        for table in row["tables"]:
            tables.setdefault(table, []).append(
                {"activity": row["activity"], "line": row["line"]})
    for name in sorted(program.files):
        fd = program.files[name]
        if fd.organization != "SQL" or not fd.sql_text:
            continue
        # An SQL-organized FILE carries its own query. Only the FROM/JOIN names are taken -
        # standard SQL, not an Easytrieve-specific reading - so a declaration whose exact
        # dialect this tool does not know still yields the tables it depends on.
        words = fd.sql_text.replace(",", " ").replace("(", " ").replace(")", " ").split()
        for i, word in enumerate(words[:-1]):
            if word.upper() in ("FROM", "JOIN"):
                tables.setdefault(words[i + 1].upper(), []).append(
                    {"file": name, "line": fd.line})
    for table in sorted(tables):
        row = {
            "artifact": table, "kind": "db2-table", "dependency": "runtime",
            "identity": "global", "resolvedBy": "the Db2 catalog (DDL / DCLGEN)",
            "needs": ("the table's DDL or DCLGEN for its columns; this tool records that "
                      "the statement names the table, not which columns it touches"),
            "referencedBy": tables[table],
        }
        hit = synonyms(table) if synonyms is not None else None
        if hit is not None:
            # A synonym's base is what the DDL declares and what cross-program identity
            # joins on; which door said so is provenance a reader may need.
            base, door = hit
            row["baseTable"] = base
            row["resolvedVia"] = "synonym map" if door == FROM_MAP else "catalog resolver"
        artifacts.append(row)
    catalog_flags = ([("synonym resolver failed mid-run ({0}); synonyms it did not "
                       "reach stay unresolved - fix the resolver and re-run").format(
                           synonyms.disabled_reason)]
                     if synonyms is not None and synonyms.disabled_reason else [])

    # Called programs and file exits.
    programs: Dict[str, dict] = {}
    for call in graph.calls:
        prog = programs.setdefault(call.program, {
            "artifact": call.program, "kind": "program", "dependency": "runtime",
            "identity": "global", "role": "called",
            "resolvedBy": "binder / link-edit control (STEPLIB / JOBLIB / LINKLIST)",
            "needs": ("the load library that provides this module. Its arguments are "
                      "recorded as POSSIBLY MODIFIED: this tool cannot see inside it"),
            "calledFrom": []})
        prog["calledFrom"].append({"activity": call.activity, "line": call.line})
    for name in sorted(program.files):
        fd = program.files[name]
        if not fd.exit_program:
            continue
        prog = programs.setdefault(fd.exit_program, {
            "artifact": fd.exit_program, "kind": "program", "dependency": "runtime",
            "identity": "global", "role": "file exit",
            "resolvedBy": "binder / link-edit control (STEPLIB / JOBLIB / LINKLIST)",
            "needs": ("the load library that provides this module. A file EXIT sees every "
                      "record on its way in or out, so it can change any field of {0} - "
                      "none of which is in the field lineage".format(name)),
            "exitFor": []})
        prog.setdefault("exitFor", []).append(name)
    for name in sorted(programs):
        artifacts.append(programs[name])

    # Macro members - compile-time, exactly like a copybook or a cataloged PROC.
    macros: Dict[str, dict] = {}
    for use in program.macros:
        row = macros.setdefault(use.name, {
            "artifact": use.name, "kind": "macro", "dependency": "compile-time",
            "identity": "program-local",
            "status": "expanded" if use.resolved else "unresolved",
            "resolvedBy": "the macro library (the MACRO / PANDD / SYSLIB concatenation)",
            "needs": ("the library that holds this macro member; its text is part of the "
                      "effective program, so a record layout or a whole activity can live "
                      "inside it. SYSLIB-order-style ambiguity applies"),
            "usedAt": []})
        entry = {"line": use.line}
        if use.arguments:
            entry["arguments"] = list(use.arguments)
        if use.in_macro:
            entry["insideMacro"] = use.in_macro
        row["usedAt"].append(entry)
        if not use.resolved:
            row["status"] = "unresolved"
    for name in sorted(macros):
        artifacts.append(macros[name])

    # Reports and other non-artifacts, named with the reason rather than dropped.
    for act in program.activities:
        if act.report is None:
            continue
        rpt = act.report
        excluded.append({
            "name": rpt.name, "kind": "report",
            "reason": ("a report is produced by this program, not retrieved from "
                       "anywhere" + (
                           "; it is written to ddname {0}, which IS a file row".format(
                               rpt.printer) if rpt.printer else
                           "; with no PRINTER operand it goes to the run's SYSPRINT"))})

    artifacts.sort(key=lambda r: (_CLASS_ORDER.get(r["kind"], 9), r["artifact"]))

    return {
        "format": "eztrieve-dependencies-artifacts",
        "formatVersion": VIEW_SCHEMA_VERSION,
        "program": program.name,
        "source": program.source_name,
        "note": (
            "One row per artifact this Easytrieve program is related to - the same shape "
            "as the COBOL and JCL artifact manifests. A 'file' row is a ddname, NOT a "
            "dataset: Easytrieve names only the ddname, so its identity is program-local "
            "and it stays unresolved until a JCL DD statement binds it (bind_jcl_ddnames, "
            "or the CLI's --bind-jcl). 'macro' rows are compile-time, like a copybook or a "
            "cataloged PROC: a macro's text is part of the effective program and can carry "
            "a whole record layout or a whole activity, so an unresolved one means the "
            "model is SHORT, not merely undocumented. 'program' rows are CALL targets and "
            "file EXITs - a file exit can change any field of the file it is attached to, "
            "and none of that is in the field lineage. Reports, VIRTUAL work areas and "
            "instream tables are excluded WITH the reason, because they are produced or "
            "carried here rather than retrieved. This program's own run also needs the "
            "ddnames it never names: " + ", ".join(_RUNTIME_DDNAMES) + " - they are the "
            "JCL's business and are deliberately not invented here. Nothing is guessed: "
            "unresolved macros and references are in 'flags'."
        ),
        "artifacts": artifacts,
        "excluded": excluded,
        "flags": (list(program.flags) + [f for f in graph.flags if f not in program.flags]
                  + catalog_flags),
    }


# --------------------------------------------------------------------------- #
# the join: resolve this program's ddnames against a JCL model
# --------------------------------------------------------------------------- #

#: The ddname a step's Easytrieve SOURCE arrives on. EZTPA00 reads the program it is to
#: run from SYSIN, which is what makes the SYSIN member - not the step's ``EXEC PGM=`` -
#: the thing that identifies which program a step runs.
SOURCE_DDNAMES = ("SYSIN",)


def jcl_steps_running(program_name: str, jcl_lineage: dict) -> List[str]:
    """The JCL steps that run THIS Easytrieve program, found by its source member.

    A compiled Easytrieve program is EXECed by name, but the ordinary case is
    ``EXEC PGM=EZTPA00`` with ``//SYSIN DD DSN=SRC.LIB(PAYROLL)`` - so the step's program
    name is the Easytrieve *interpreter*, and what identifies WHICH program runs is the
    member on its SYSIN. Matching on that is the difference between binding this program's
    ddnames and binding every Easytrieve step's in the job.

    Read from the LINEAGE view's per-step DD list rather than from the artifacts manifest's
    control-card rows: the manifest keys those rows on the DSN, so two members of the same
    source library collapse into one row that names both steps - which is exactly the case
    this function has to tell apart.
    """
    want = str(program_name).upper()
    steps: List[str] = []
    for srow in jcl_lineage.get("steps", []) or []:
        step = srow.get("step")
        if not step:
            continue
        for dd in list(srow.get("inputs") or []) + list(srow.get("outputs") or []):
            if str(dd.get("ddname", "")).upper() not in SOURCE_DDNAMES:
                continue
            member = dd.get("member")
            if member is not None:
                match = str(member).upper() == want
            else:
                # A sequential source dataset rather than a library member: the last
                # qualifier is the closest thing to a member name it has.
                dataset = str(dd.get("dataset") or "")
                match = bool(dataset) and dataset.rsplit(".", 1)[-1].upper() == want
            if match and step not in steps:
                steps.append(step)
            break
    return steps


def bind_jcl_ddnames(manifest: dict, jcl_lineage: dict, *,
                     steps: Sequence[str] = ()) -> dict:
    """Resolve an Easytrieve manifest's ddnames against a JCL job's DD bindings.

    ``jcl_lineage`` is the ``jcl-dependencies-lineage`` dict - a plain dict, which is
    precisely what keeps this package from importing the JCL one. The step is found from
    the SYSIN member that names this program (see :func:`jcl_steps_running`) unless
    ``steps`` names it outright.

    Honesty rules, the same three the JCL side applies to a COBOL manifest: a ddname bound
    to DIFFERENT datasets across the supplied steps lists ``datasetCandidates`` rather than
    picking one; an unmatched ddname is left exactly as it was, still needing JCL; and when
    the step could not be identified the binding is made on ddname alone and SAYS SO,
    because a job that runs three Easytrieve programs has three different meanings for
    ``SYSIN`` and possibly for every other ddname too. Returns a new manifest; the input is
    not mutated.
    """
    import copy

    out = copy.deepcopy(manifest)
    flags: List[str] = out.setdefault("flags", [])
    program = str(out.get("program", "")).upper()
    bindings = list(jcl_lineage.get("ddBindings", []) or [])

    chosen = [s.upper() for s in steps]
    basis = "steps named by the caller"
    if not chosen:
        direct = [b["step"] for b in bindings
                  if str(b.get("program", "")).upper() == program]
        if direct:
            chosen = sorted(set(direct))
            basis = ("the JCL step(s) whose EXEC PGM= is this program - it runs as a "
                     "compiled load module")
    if not chosen:
        found = jcl_steps_running(program, jcl_lineage)
        if found:
            chosen = [s.upper() for s in found]
            basis = "the JCL step(s) whose SYSIN member names this program"
    if not chosen:
        basis = "ddname alone - no step could be identified as running this program"
        flags.append(
            "JCL binding was made on ddname alone: no step in {0} could be identified as "
            "running {1} (its EXEC PGM= is the Easytrieve interpreter, and no step's SYSIN "
            "member matched the program name). If the job runs more than one Easytrieve "
            "program, a ddname may have been bound from the wrong step - name the step "
            "explicitly to remove the doubt.".format(
                jcl_lineage.get("source", "the JCL"), program))

    job = jcl_lineage.get("job") or jcl_lineage.get("source") or "<jcl>"
    by_ddname: Dict[str, List[dict]] = {}
    for b in bindings:
        if chosen and str(b.get("step", "")).upper() not in chosen:
            continue
        entry = {"job": job, "step": b.get("step"), "dataset": b.get("dataset"),
                 "io": b.get("io")}
        for key in ("generation", "member", "conditions"):
            if b.get(key):
                entry[key] = b[key]
        by_ddname.setdefault(str(b.get("ddname", "")).upper(), []).append(entry)

    matched = 0
    for row in out.get("artifacts", []) or []:
        if row.get("kind") != "file" or not row.get("ddname"):
            continue
        found = by_ddname.get(str(row["ddname"]).upper())
        if not found:
            continue
        matched += 1
        row["boundBy"] = found
        datasets = sorted({e["dataset"] for e in found if e.get("dataset")})
        if len(datasets) == 1:
            row["dataset"] = datasets[0]
            row["resolvedBy"] = "JCL DD statement: " + ", ".join(
                sorted({"{0}.{1}".format(e["job"], e["step"]) for e in found}))
            row["identity"] = "global"
            # The chain is closed: the ddname now has its DSN. (Whether the record layout
            # this program declares matches that dataset is a different question, and one
            # this tool deliberately does not assert.)
            row.pop("needs", None)
        elif datasets:
            row["datasetCandidates"] = datasets
            flags.append(
                "file {0} (ddname {1}): bound to {2} different datasets across the "
                "supplied JCL - the same program runs against different data in different "
                "steps; 'boundBy' says which step uses which. Not collapsed.".format(
                    row["artifact"], row["ddname"], len(datasets)))

    out["jclBinding"] = {"job": job, "source": jcl_lineage.get("source"),
                         "steps": chosen, "basis": basis, "boundFiles": matched}
    if matched:
        out["note"] = out.get("note", "") + (
            " File rows carrying 'dataset'/'boundBy' were resolved against the supplied "
            "JCL: the ddname -> DSN binding is closed, and 'boundBy' names the step that "
            "closed it (with that step's run conditions where the JCL is conditional). "
            "The record layout this program declares for a bound dataset is NOT checked "
            "against it - that would need the dataset's own definition.")
    return out


# --------------------------------------------------------------------------- #
# dependents - the reverse direction, which only a host index holds
# --------------------------------------------------------------------------- #

FORMAT_DEPENDENTS = "eztrieve-dependencies-dependents"

_DEPENDENTS_NOTE = (
    "What the ESTATE says depends on what this program PROVIDES - the reverse of every "
    "other view here, and the half this source cannot contain. Two things can be "
    "depended on: the program itself, which a job runs by naming this member as "
    "EZTPA00's SYSIN, and the data it WRITES, which downstream work reads. Written data "
    "is asked about as the DATASET a JCL DD statement binds its ddname to, never as the "
    "ddname itself: a ddname is program-local, so the same spelling in another program "
    "means something unrelated and no estate-wide index can be keyed on one. A ddname "
    "bound to several datasets is asked about once per dataset, never collapsed. With no "
    "JCL supplied (bind_jcl_ddnames, or the CLI's --bind-jcl) a written ddname is "
    "reported in 'unanswered' as unanswerable by THIS package - 'asked' is false and "
    "'needs' says what would close it - rather than asked about under a name that exists "
    "nowhere outside this source. Supplied by the host through --dependents-map or "
    "--dependents-resolver and reported as given; 'suppliedBy' says which door answered. "
    "'matchStrength' is the host's own field, never folded into prose, and a capped "
    "answer carries 'truncated' with the true 'total'. 'unanswered' is the honest half: "
    "absent from these lists means nobody said, never that nothing depends on the name."
)

#: What an unbound ddname still needs - the words the artifacts view already uses for the
#: same gap on the same row (see the 'file' row's own 'needs').
_NEEDS_JCL = ("the JCL DD statement that binds this ddname to a dataset - the program "
              "names only the ddname, so the DSN is not knowable from this source alone")

#: Why a bare ddname is never handed to a dependents lookup. A host asked one has two
#: choices and both are bad: match the spelling estate-wide, which mints a dependency
#: between every program that happens to use the same ddname, or refuse. A wrong edge in
#: the reverse direction is worse than a missing one, so the ask is not made at all.
_DDNAME_IS_LOCAL = (
    "a ddname is program-local, not an identity the estate holds: the same spelling in "
    "another program means something unrelated, so there is no index that could be asked "
    "about this name. Bind it to a dataset (bind_jcl_ddnames, or the CLI's --bind-jcl) "
    "and the reverse direction is asked about the DSN instead")


def _bound_file_rows(bound: Optional[dict]) -> Dict[str, dict]:
    """A bound manifest's file rows, keyed by ddname. Empty when nothing was bound."""
    rows: Dict[str, dict] = {}
    for row in (bound or {}).get("artifacts", []) or []:
        if row.get("kind") == "file" and row.get("ddname"):
            rows[str(row["ddname"]).upper()] = row
    return rows


def _bound_sites(row: dict, dataset: str) -> List[str]:
    """``JOB.STEP`` for every binding on ``row`` that named ``dataset``."""
    return sorted({"{0}.{1}".format(e.get("job"), e.get("step"))
                   for e in row.get("boundBy", []) or []
                   if e.get("dataset") == dataset})


def _provides(program: Program, graph: LineageGraph,
              bound: Optional[dict] = None) -> List[dict]:
    """What another artifact can depend on: this program, and the DATASETS it writes.

    Files it only READS are left out deliberately: they are what this program depends
    on, and the reverse question about them belongs to whoever writes them.

    A written file is named here by its ddname, which is program-local, so it is asked
    about as the dataset the JCL binds that ddname to - ``bound`` is this program's
    artifact manifest after :func:`bind_jcl_ddnames`, and the join, the step
    identification and the three honesty rules are all that function's, not repeated
    here. Without a binding there is no name to ask about, and the row says so
    (``asked`` false, with what would close it) rather than being asked about bare.
    """
    rows: List[dict] = []
    if program.name:
        rows.append({"name": program.name, "kind": "program",
                     "provides": "the program itself, run as EZTPA00's SYSIN member"})
    io = graph.file_io()
    bound_rows = _bound_file_rows(bound)
    asks: List[dict] = []
    unaskable: List[dict] = []
    for name in sorted(program.files):
        fd = program.files[name]
        if fd.virtual or fd.instream:
            continue                     # in-storage or in-source: nothing to depend on
        # This package's own io vocabulary - read / write / read-write - not the JCL
        # one. A file it only READS is what this program depends on, and the reverse
        # question about that belongs to whoever writes it.
        if io.get(name) not in ("write", "read-write"):
            continue
        bound_row = bound_rows.get(name.upper(), {})
        datasets = ([bound_row["dataset"]] if bound_row.get("dataset")
                    else list(bound_row.get("datasetCandidates") or []))
        if not datasets:
            unaskable.append({"name": name, "kind": "file", "asked": False,
                              "reason": _DDNAME_IS_LOCAL, "needs": _NEEDS_JCL})
            continue
        for dataset in datasets:
            sites = _bound_sites(bound_row, dataset)
            ask = {"name": dataset, "kind": "dataset",
                   "provides": "written as ddname {0}, bound to this dataset by {1}".format(
                       name, ", ".join(sites) or "the supplied JCL"),
                   "ddname": name}
            if len(datasets) > 1:
                # The ddname binds to different data in different steps. Each dataset is
                # asked about on its own; collapsing them to one here would undo the
                # honesty rule the binding just applied.
                ask["datasetCandidates"] = list(datasets)
            asks.append(ask)
    asks.sort(key=lambda r: (r["name"], r["ddname"]))
    return rows + asks + unaskable


def build_eztrieve_dependents(program: Program, lookup, *,
                              graph: Optional[LineageGraph] = None,
                              bound: Optional[dict] = None) -> Optional[dict]:
    """What depends on what this program provides, or ``None`` if nobody was asked.

    ``None`` rather than an empty view: an empty answer reads as "nothing in the estate
    depends on this program", which a run that opened no door cannot claim.

    ``bound`` is this program's artifact manifest after :func:`bind_jcl_ddnames` - what
    turns a program-local ddname into a dataset an estate index can be asked about.
    Without one the program itself is still asked about, and every ddname it writes is
    reported unanswerable with the reason: that half of the reverse direction needs the
    JCL, and saying so is the honest answer.
    """
    if lookup is None or not lookup.supplied:
        return None
    graph = graph or build_graph(program)

    rows = _provides(program, graph, bound)
    provided: List[dict] = []
    unanswered: List[dict] = []
    for row in rows:
        if row.get("asked") is False:
            # This package cannot form the ask and says so. Not the same statement as a
            # host that WAS asked and does not cover the name, and not the same as one
            # that broke: three answers, and collapsing any two of them is the bug the
            # dependents contract exists to prevent.
            unanswered.append(row)
            continue
        answer = lookup(row["name"], row["kind"])
        if answer is None:
            entry = {"name": row["name"], "kind": row["kind"]}
            if row.get("ddname"):
                entry["ddname"] = row["ddname"]
            entry["reason"] = (
                "the lookup failed earlier in this run and was not asked again"
                if lookup.disabled_reason else "the lookup does not cover this name")
            unanswered.append(entry)
            continue
        out = {"name": row["name"], "kind": row["kind"], "provides": row["provides"]}
        for key in ("ddname", "datasetCandidates"):
            if row.get(key) is not None:
                out[key] = row[key]
        out["dependents"] = output_rows(answer.rows)
        out["count"] = len(answer.rows)
        out["suppliedBy"] = answer.door
        if answer.truncated:
            out["truncated"] = True
            if answer.total is not None:
                out["total"] = answer.total
        provided.append(out)

    flags = []
    if lookup.disabled_reason:
        flags.append(
            "dependents lookup failed mid-run ({0}); names it did not reach stay "
            "unanswered - fix the lookup and re-run".format(lookup.disabled_reason))
    if lookup.map_warning:
        flags.append(
            "part of the dependents map could not be read ({0}); the entries it did read "
            "answered normally".format(lookup.map_warning))
    binding = dict((bound or {}).get("jclBinding") or {})
    if bound is not None and not binding.get("steps"):
        # Read from the binding's own STEPS rather than from its prose: an empty list is
        # the fact that no step could be identified as running this program.
        flags.append(
            "the JCL binding was made on ddname alone: no step in {0} could be identified "
            "as running {1}, so a dataset asked about here may have been bound from a "
            "different Easytrieve step of the same job - name the step to remove the "
            "doubt".format(binding.get("source") or binding.get("job") or
                           "the supplied JCL", program.name))
    candidates: Dict[str, List[str]] = {}
    for row in rows:
        if row.get("datasetCandidates"):
            candidates.setdefault(row["ddname"], row["datasetCandidates"])
    for ddname in sorted(candidates):
        flags.append(
            "ddname {0} binds to {1} different datasets across the supplied JCL ({2}): "
            "each was asked about separately and none of them is presented as THE "
            "dataset this program writes".format(
                ddname, len(candidates[ddname]), ", ".join(candidates[ddname])))

    view = {
        "format": FORMAT_DEPENDENTS,
        "formatVersion": VIEW_SCHEMA_VERSION,
        "program": program.name,
        "source": program.source_name,
        "note": _DEPENDENTS_NOTE,
        "suppliedBy": lookup.describe(),
    }
    if binding:
        # Where the datasets came from: job, steps and the basis the binding was made on.
        view["jclBinding"] = binding
    view["provides"] = provided
    view["unanswered"] = unanswered
    view["flags"] = flags
    return view

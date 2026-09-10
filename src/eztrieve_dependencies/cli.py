"""Command-line entry point: an Easytrieve program -> its field lineage and dependencies."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional

from mainframe_artifacts.artifact_service import decode_member, load_fetcher
from mainframe_artifacts.bundle import open_bundle
from mainframe_artifacts.cliargs import (add_logging_args, add_output_args,
                                         add_dependents_args, add_retrieval_args,
                                         add_synonym_args, dependents_lookup,
                                         jobs as _jobs, synonym_lookup)
from mainframe_artifacts.errors import CobolXstateError
from mainframe_artifacts.logging_setup import PACKAGE_LOGGER as CORE_LOGGER
from mainframe_artifacts.logging_setup import configure_logging
from mainframe_artifacts.output import make_run_dir, run_dir, write_json
from mainframe_artifacts.profiling import StageTimer
from mainframe_artifacts.report import report_stages

from . import PACKAGE_LOGGER
from .api import analyze, gather
from .detect import classify
from .lexer import RIGHT_MARGIN
from .views import bind_jcl_ddnames

# Explicit name, NOT __name__: this module is also run as
# `python -m eztrieve_dependencies.cli`, where __name__ == "__main__" would put the logger
# outside the package hierarchy and out of configure_logging's reach (so INFO/progress
# would be silently dropped).
_log = logging.getLogger("eztrieve_dependencies.cli")

# The dependents view is not a --target choice: it is not a view you ask for, it is an
# answer you were given, so it is written exactly when a lookup supplied one.
_SUFFIXES = (".ezt.artifacts.json", ".ezt.lineage.json", ".ezt.dependents.json",
             ".ezt.prefetch.json", ".ezt.fetch.json")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="eztrieve-dependencies",
        description="Parse a CA-Easytrieve program and emit its record layouts, its "
                    "field-to-field lineage (end to end, from an input file's bytes to an "
                    "output file's bytes or a report column), and the manifest of "
                    "everything it depends on - following %%MACRO members so the fields "
                    "and activities inside them are in the model.",
    )
    p.add_argument("source", help="path to an Easytrieve program ('-' for stdin)")
    p.add_argument("--target", choices=["both", "artifacts", "lineage"], default="both",
                   help="which views to write (default: both). lineage = record layouts, "
                        "field edges and end-to-end field flow; artifacts = the manifest "
                        "of files, macros, called programs and tables.")
    p.add_argument("-I", "--path", "--macro-path", dest="path", action="append",
                   default=[], metavar="DIR",
                   help="directory to search for %%MACRO members before asking the estate "
                        "(repeatable)")
    p.add_argument("--macro-ext", dest="macro_ext", action="append", default=[],
                   metavar="EXT",
                   help="extra file extension to try when looking for a macro on the "
                        "search path, e.g. --macro-ext .lib (repeatable). The Easytrieve "
                        "defaults (.mac/.ezm/.ezt/...) are always tried as well.")
    p.add_argument("--right-margin", type=int, default=RIGHT_MARGIN, metavar="COL",
                   help="the last column the compiler scans (default: %(default)s). "
                        "73-80 is the sequence-number area and is ignored, which is what "
                        "the compiler does; pass 0 for a member that is not in fixed "
                        "80-byte format and whose lines run past column 72.")
    p.add_argument("--program-name", metavar="NAME",
                   help="the program's identity (default: the source member name). "
                        "Easytrieve has no PROGRAM-ID, and this name is what the JCL join "
                        "matches against a step's SYSIN member.")
    p.add_argument("--bind-jcl", metavar="FILE",
                   help="a jcl-dependencies LINEAGE view (*.jcl.lineage.json). Its "
                        "ddBindings resolve this program's file ddnames to real datasets, "
                        "closing the one thing an Easytrieve source cannot say. WHICH step "
                        "runs this program is found from the SYSIN member that names it. "
                        "Implies --target artifacts.")
    p.add_argument("--bind-step", action="append", default=[], metavar="STEP",
                   help="bind only from this JCL step (repeatable). Use it when a job runs "
                        "several Easytrieve programs and the SYSIN member cannot identify "
                        "which step is this one.")
    p.add_argument("--max-rounds", type=int, default=12, metavar="N",
                   help="how deep to follow %%MACRO nesting when closing over the program "
                        "(default: 12). The closure is bounded because a member set deeper "
                        "than this is more likely a resolver loop than a real program; "
                        "hitting the bound is REPORTED, never silently treated as a "
                        "complete closure.")
    add_retrieval_args(p)
    # Db2 catalog knowledge: a 'db2-table' row written under a SYNONYM/ALIAS gains its
    # base table, so cross-program identity can land on the name the DDL declares.
    add_synonym_args(p)
    add_dependents_args(p)
    add_output_args(p, outdir_help=(
        "directory for output (default: ./out). EVERY file this run produces goes here, "
        "exactly as given with nothing appended - both views, both retrieval reports, and "
        "the members retrieved from the estate (under deps/). Created with parents if it "
        "does not exist."))
    add_logging_args(p)
    return p


def _service(args, source_name: str):
    """The estate artifact service for this run, and why it is missing if it is.

    Never fatal. A run without the service still parses whatever is on the local search
    path and still writes its reports - they simply say, per member, that nothing was ever
    looked for."""
    fetcher, why = load_fetcher(args.copybook_fetcher)
    if fetcher is None:
        _log.warning("[{0}] WARNING: {1}".format(source_name, why))
    return fetcher, why


def _load_jcl(path_text: str) -> dict:
    """The JCL lineage view: the ddBindings, and the per-step DD list the SYSIN member is
    identified from. Both live in that one view, so nothing else has to be found."""
    path = Path(path_text)
    lineage = json.loads(path.read_text(encoding="utf-8"))
    if lineage.get("format") != "jcl-dependencies-lineage":
        raise CobolXstateError(
            "--bind-jcl {0}: this is not a jcl-dependencies LINEAGE view (its 'format' is "
            "{1!r}). The binding reads 'ddBindings' and the per-step DD list, which only "
            "that view carries - point it at the *.jcl.lineage.json a jcl-dependencies run "
            "wrote.".format(path, lineage.get("format")))
    return lineage


def run(argv: Optional[List[str]] = None, timing_sink=None) -> int:
    """Parse args, configure logging, and dispatch, behind the top-level error boundary."""
    args = build_parser().parse_args(argv)
    # BOTH roots: retrieval logs from mainframe_artifacts.*, everything else from
    # eztrieve_dependencies.*. A root nobody configures propagates to the root logger, or
    # prints WARNING+ via logging's lastResort - which would end -qq's silence.
    configure_logging(verbose=args.verbose or (1 if args.debug else 0), quiet=args.quiet,
                      loggers=(CORE_LOGGER, PACKAGE_LOGGER))
    try:
        return _run(args, timing_sink=timing_sink)
    except CobolXstateError as exc:
        _log.error("%s", exc)
        return 1
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        _log.error("interrupted")
        return 130
    except Exception:
        if args.debug:
            raise
        _log.critical("internal error while processing %r - re-run with --debug for the "
                      "full traceback", args.source)
        _log.debug("internal error traceback", exc_info=True)
        return 1


def _run(args, timing_sink=None) -> int:
    paths = list(args.path)
    if args.source == "-":
        source = sys.stdin.read()
        source_name = "<stdin>"
        default_stem = None
    else:
        path = Path(args.source)
        if not path.exists():
            _log.error("error: no such file: {0}".format(path))
            return 2
        source = decode_member(path.read_bytes())
        source_name = path.name
        default_stem = path.stem
        # A macro member most often sits beside the program that uses it.
        paths.append(str(path.parent))

    ok, why = classify(source_name, source)
    if not ok:
        _log.warning("[{0}] WARNING: {1}".format(source_name, why))

    timer = StageTimer(_log, args.timing, source_name, sink=timing_sink)

    if args.gather_only and args.from_bundle:
        _log.error("error: --gather-only writes a bundle and --from-bundle reads one; "
                   "they cannot both apply to a single run")
        return 2

    bundle = None
    if args.from_bundle:
        try:
            bundle = open_bundle(args.from_bundle)
        except CobolXstateError as exc:
            _log.error("error: {0}".format(exc))
            return 2

    jcl_lineage = None
    if args.bind_jcl:
        jcl_path = Path(args.bind_jcl)
        if not jcl_path.is_file():
            _log.error("error: --bind-jcl {0}: no such file".format(jcl_path))
            return 2
        try:
            jcl_lineage = _load_jcl(args.bind_jcl)
        except ValueError as exc:
            _log.error("error: --bind-jcl {0} is not readable JSON ({1})".format(
                jcl_path, exc))
            return 2

    lookup, why_synonyms = synonym_lookup(args)
    if why_synonyms:
        _log.error("error: {0}".format(why_synonyms))
        return 2

    reverse, why_dependents = dependents_lookup(args)
    if why_dependents:
        _log.error("error: {0}".format(why_dependents))
        return 2

    fetcher, why_service = (None, None) if bundle is not None \
        else _service(args, source_name)

    out_dir = run_dir(args.outdir)
    err = make_run_dir(out_dir)
    if err:
        _log.error("error: {0}".format(err))
        return 2
    deps = str(out_dir / "deps")

    if args.gather_only:
        gathered = gather(source, source_name=source_name, fetcher=fetcher, paths=paths,
                          dest=args.gather_only, unavailable=why_service,
                          margin=args.right_margin, exts=tuple(args.macro_ext),
                          max_rounds=args.max_rounds, jobs=_jobs(args))
        _log.info("[{0}] wrote estate bundle {1}".format(source_name, gathered))
        _log.info("[{0}] model from it with: --from-bundle {1}".format(
            source_name, args.gather_only))
        timer.report()
        return 0

    analysis = analyze(source, source_name=source_name, bundle=bundle, fetcher=fetcher,
                       retrieve=not args.no_fetch, paths=paths, dest=deps,
                       unavailable=why_service, program_name=args.program_name,
                       margin=args.right_margin, exts=tuple(args.macro_ext),
                       max_rounds=args.max_rounds, jobs=_jobs(args), timer=timer,
                       synonyms=lookup.mapping if lookup is not None else None,
                       synonym_resolver=lookup.resolver if lookup is not None else None,
                       dependents=reverse.mapping if reverse is not None else None,
                       dependents_resolver=(reverse.resolver if reverse is not None
                                            else None))
    program = analysis.program
    base = default_stem or program.name or "program"

    wanted = set({"both": ("artifacts", "lineage")}.get(args.target, (args.target,)))
    if jcl_lineage is not None and "artifacts" not in wanted:
        _log.info("[{0}] --bind-jcl binds the ARTIFACTS view, so it is written too".format(
            source_name))
        wanted.add("artifacts")

    artifacts = analysis.artifacts() if "artifacts" in wanted else None
    if artifacts is not None and jcl_lineage is not None:
        with timer.stage("bind-jcl"):
            artifacts = bind_jcl_ddnames(artifacts, jcl_lineage,
                                         steps=tuple(args.bind_step))
        binding = artifacts["jclBinding"]
        _log.info("[{0}] bound {1} ddname(s) from {2} via {3}".format(
            source_name, binding["boundFiles"], binding.get("source") or args.bind_jcl,
            binding["basis"]))

    written = {
        ".ezt.artifacts.json": artifacts,
        ".ezt.lineage.json": analysis.lineage() if "lineage" in wanted else None,
        # None when no door was opened, and the loop below writes nothing for a None.
        ".ezt.dependents.json": analysis.dependents(),
        ".ezt.prefetch.json": analysis.prefetch.report(),
        ".ezt.fetch.json": analysis.fetch,
    }
    for suffix in _SUFFIXES:
        obj = written.get(suffix)
        if obj is None:
            continue
        target = out_dir / "{0}{1}".format(base, suffix)
        write_json(target, obj, args.indent)
        _log.info("[{0}] wrote {1}".format(source_name, target))
    report_stages(_log, source_name, analysis.prefetch, analysis.fetch)

    if args.summary:
        lineage = analysis.lineage()
        fields = sum(len(f["record"]) for f in lineage["files"])
        resolved = sum(1 for r in lineage["fieldFlow"] if r["origins"])
        _log.info("[{0}] {1} file(s), {2} record field(s), {3} working-storage field(s), "
                  "{4} activity(ies), {5} field edge(s), {6}/{7} sink(s) traced to an "
                  "input, {8} unresolved reference(s), {9} flag(s)".format(
                      program.name, len(lineage["files"]), fields,
                      len(lineage["workingStorage"]), len(lineage["activities"]),
                      len(lineage["fieldLineage"]), resolved, len(lineage["fieldFlow"]),
                      len(lineage["unresolved"]), len(lineage["flags"])))
        for edge in lineage["fileFlow"]:
            _log.info("  FLOW {0} -> {1} ({2} field(s))".format(
                edge["from"], edge["to"], edge["fields"]))
        for flag in lineage["flags"]:
            _log.info("  FLAG {0}".format(flag))

    timer.report()
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()

"""The Easytrieve front-end as a library: analyze a program, get its field lineage and its
dependencies.

The same shape as the COBOL and JCL sides' ``api`` modules, and for the same reason:
driving this from another Python program should be the code path the command line takes,
not a second one that drifts from it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Sequence

from mainframe_artifacts.bundle import EstateBundle, recording_fetcher, write_bundle
from mainframe_artifacts.fetch import fetch_dependencies
from mainframe_artifacts.prefetch import PrefetchResult
from mainframe_artifacts.profiling import StageTimer
from mainframe_artifacts.synonyms import SynonymLookup

from .lexer import RIGHT_MARGIN
from .lineage import LineageGraph, build_graph
from .model import Program
from .parser import parse_eztrieve
from .prefetch import prefetch_eztrieve
from .views import bind_jcl_ddnames, build_eztrieve_artifacts, build_eztrieve_lineage

_log = logging.getLogger(__name__)


@dataclass
class ProgramAnalysis:
    """One analyzed Easytrieve program, and the views projected from it."""

    program: Program
    prefetch: PrefetchResult
    source_name: str = "<eztrieve>"
    fetch: Optional[dict] = None
    #: Db2 SYNONYM/ALIAS knowledge (a map, a host resolver, or both) - None when the
    #: run opened neither door, and then every table is reported as written.
    synonyms: Optional[SynonymLookup] = None

    _graph: Optional[LineageGraph] = field(default=None, repr=False)
    _lineage: Optional[dict] = field(default=None, repr=False)
    _artifacts: Optional[dict] = field(default=None, repr=False)

    def graph(self) -> LineageGraph:
        """The field-dependency graph both views project. Built once."""
        if self._graph is None:
            self._graph = build_graph(self.program)
        return self._graph

    def lineage(self) -> dict:
        """Record layouts, field-to-field edges, and end-to-end field flow."""
        if self._lineage is None:
            self._lineage = build_eztrieve_lineage(self.program, graph=self.graph())
        return self._lineage

    def artifacts(self) -> dict:
        """Every file, macro, called program and table this program depends on."""
        if self._artifacts is None:
            self._artifacts = build_eztrieve_artifacts(self.program, graph=self.graph(),
                                                       synonyms=self.synonyms)
        return self._artifacts

    def bind(self, jcl_lineage: dict, *, steps: Sequence[str] = ()) -> dict:
        """Close the ddname->dataset join using a JCL job's lineage view.

        Takes a plain dict and returns one - this package never imports the JCL one.
        """
        return bind_jcl_ddnames(self.artifacts(), jcl_lineage, steps=steps)


def analyze(source: str, *, source_name: str = "<eztrieve>",
            bundle: Optional[EstateBundle] = None,
            fetcher: Optional[Any] = None,
            retrieve: bool = True,
            paths: Sequence[str] = (), dest: Optional[str] = None,
            unavailable: Optional[str] = None,
            program_name: Optional[str] = None,
            margin: int = RIGHT_MARGIN,
            exts: Sequence[str] = (),
            max_rounds: int = 12, jobs: int = 1,
            timer: Optional[StageTimer] = None,
            synonyms: Optional[Dict[str, str]] = None,
            synonym_resolver: Optional[Callable[[str], Optional[str]]] = None,
            ) -> ProgramAnalysis:
    """Retrieve, parse and model one Easytrieve program.

    ``synonyms`` (a ``{"SYNONYM": "BASE"}`` map) and ``synonym_resolver`` (a
    ``(name) -> base | None`` callable the host supplies, see
    ``mainframe_artifacts.protocol.SynonymResolver``) are the two doors Db2 catalog
    knowledge arrives by; the map answers first. With either open, a ``db2-table``
    artifact row written under a synonym also names its base table. Neither is a
    default: a table stays as written, never guessed.

    Stage 1 is not optional decoration here. An Easytrieve macro routinely carries a whole
    record layout, and sometimes a whole activity; parsed without it the files have no
    fields, so nothing that references them resolves and the field lineage is empty exactly
    where the program does its work - while still looking finished.

    The estate is reached the same four ways as on the COBOL and JCL sides: through
    ``fetcher``, not at all (``fetcher=None``), deliberately off (``retrieve=False``), or
    replayed from a gathered ``bundle``.
    """
    timer = timer or StageTimer(_log, False, source_name)

    if bundle is not None:
        fetcher = bundle.fetcher()
        unavailable = unavailable or bundle.unavailable
    elif not retrieve:
        fetcher = None
        unavailable = unavailable or ("retrieval was disabled for this run, so this "
                                      "member was never looked for")

    with timer.stage("prefetch"):
        pre = prefetch_eztrieve(source, fetcher, paths=list(paths), dest=dest,
                                source_name=source_name, unavailable=unavailable,
                                max_rounds=max_rounds, jobs=jobs, exts=exts,
                                margin=margin)
    with timer.stage("parse"):
        program = parse_eztrieve(source, resolver=pre.resolver(),
                                 source_name=source_name, program_name=program_name,
                                 margin=margin)

    lookup = (SynonymLookup(synonyms, synonym_resolver)
              if (synonyms or synonym_resolver is not None) else None)
    analysis = ProgramAnalysis(program=program, prefetch=pre, source_name=source_name,
                               synonyms=lookup)
    with timer.stage("field-lineage"):
        analysis.lineage()
    with timer.stage("artifacts"):
        art = analysis.artifacts()
    with timer.stage("fetch"):
        analysis.fetch = fetch_dependencies(art, fetcher, dest=dest,
                                            prefetched=pre.store,
                                            unavailable=unavailable, jobs=jobs)
    return analysis


def gather(source: str, *, source_name: str = "<eztrieve>",
           fetcher: Optional[Any] = None,
           paths: Sequence[str] = (), dest: str,
           unavailable: Optional[str] = None,
           margin: int = RIGHT_MARGIN, exts: Sequence[str] = (),
           max_rounds: int = 12, jobs: int = 1) -> str:
    """Run the retrieval half where the estate is reachable; return the bundle manifest."""
    recorder, answers = recording_fetcher(fetcher) if fetcher is not None else (None, [])
    analysis = analyze(source, source_name=source_name, fetcher=recorder, paths=paths,
                       dest=dest, unavailable=unavailable, margin=margin, exts=exts,
                       max_rounds=max_rounds, jobs=jobs)
    return write_bundle(dest, subject_name=source_name, subject_text=source,
                        kind="eztrieve", prefetch=analysis.prefetch, answers=answers,
                        fetch=analysis.fetch)

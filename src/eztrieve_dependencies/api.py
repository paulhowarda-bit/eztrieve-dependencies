"""The Easytrieve front-end as a library: analyze a program, get its field lineage and its
dependencies.

The same shape as the COBOL and JCL sides' ``api`` modules, and for the same reason:
driving this from another Python program should be the code path the command line takes,
not a second one that drifts from it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from mainframe_artifacts.bundle import (EstateBundle, recording_dependents_resolver,
                                        recording_fetcher, write_bundle)
from mainframe_artifacts.dependents import DependentsLookup
from mainframe_artifacts.fetch import fetch_dependencies
from mainframe_artifacts.prefetch import PrefetchResult
from mainframe_artifacts.profiling import StageTimer
from mainframe_artifacts.synonyms import SynonymLookup

from . import PRODUCER
from .lexer import RIGHT_MARGIN
from .lineage import LineageGraph, build_graph
from .model import Program
from .parser import parse_eztrieve
from .prefetch import prefetch_eztrieve
from .views import (bind_jcl_ddnames, build_eztrieve_artifacts,
                    build_eztrieve_dependents, build_eztrieve_lineage)

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

    #: What the estate says depends on what this program provides - None when the run
    #: opened neither door, and then :meth:`dependents` is None too, because "nobody told
    #: us" is not "nothing depends on this program".
    dependents_lookup: Optional[DependentsLookup] = None

    #: A JCL job's lineage view, if one was supplied: what binds this program's ddnames
    #: to real datasets. None means the ddname->dataset chain stays open, and the
    #: reverse direction on the data this program writes is reported unanswerable.
    jcl_lineage: Optional[dict] = None
    #: Which JCL steps to bind from, when the SYSIN member cannot identify one.
    bind_steps: Tuple[str, ...] = ()

    _graph: Optional[LineageGraph] = field(default=None, repr=False)
    _lineage: Optional[dict] = field(default=None, repr=False)
    _artifacts: Optional[dict] = field(default=None, repr=False)
    _bound: Optional[dict] = field(default=None, repr=False)
    _dependents: Optional[dict] = field(default=None, repr=False)

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

    def dependents(self) -> Optional[dict]:
        """What depends on this program and on the ddnames it writes, or ``None`` if
        nobody was asked.

        ``None`` rather than an empty view: "no lookup was given" and "nothing depends on
        this program" are different statements, and only the first is usually true.
        """
        if self.dependents_lookup is None or not self.dependents_lookup.supplied:
            return None
        if self._dependents is None:
            self._dependents = build_eztrieve_dependents(
                self.program, self.dependents_lookup, graph=self.graph(),
                bound=self.bound_artifacts())
        return self._dependents

    def bind(self, jcl_lineage: dict, *, steps: Sequence[str] = ()) -> dict:
        """Close the ddname->dataset join using a JCL job's lineage view.

        Takes a plain dict and returns one - this package never imports the JCL one.
        """
        return bind_jcl_ddnames(self.artifacts(), jcl_lineage, steps=steps)

    def bound_artifacts(self) -> Optional[dict]:
        """The manifest with THIS run's ``jcl_lineage`` bound into it, or ``None``.

        The same join :meth:`bind` performs, done once for the run and cached, so the
        artifacts view a caller writes and the datasets the dependents view asks about
        are the one binding rather than two that could differ.
        """
        if self.jcl_lineage is None:
            return None
        if self._bound is None:
            self._bound = self.bind(self.jcl_lineage, steps=self.bind_steps)
        return self._bound


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
            dependents: Optional[Mapping[str, Sequence[dict]]] = None,
            dependents_resolver: Optional[Callable[..., Any]] = None,
            jcl_lineage: Optional[dict] = None,
            bind_steps: Sequence[str] = (),
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

    ``jcl_lineage`` (a ``jcl-dependencies-lineage`` dict, with ``bind_steps`` when the
    SYSIN member cannot identify the step) is bound into the manifest before the reverse
    direction is asked, because that binding is what gives a written ddname a name that
    exists outside this program. Supplied here rather than only through
    :meth:`ProgramAnalysis.bind` for a reason: the dependents view is built eagerly below,
    and BUILDING it is what asks the host, so a binding that arrives afterwards is too
    late to change what was asked.

    The estate is reached the same four ways as on the COBOL and JCL sides: through
    ``fetcher``, not at all (``fetcher=None``), deliberately off (``retrieve=False``), or
    replayed from a gathered ``bundle``.
    """
    timer = timer or StageTimer(_log, False, source_name)

    if bundle is not None:
        fetcher = bundle.fetcher()
        if dependents_resolver is None and bundle.has_dependents():
            dependents_resolver = bundle.dependents()
        unavailable = unavailable or bundle.unavailable
    elif not retrieve:
        fetcher = None
        unavailable = unavailable or ("retrieval was disabled for this run, so this "
                                      "member was never looked for")

    with timer.stage("prefetch"):
        pre = prefetch_eztrieve(source, fetcher, paths=list(paths), dest=dest,
                                source_name=source_name, unavailable=unavailable,
                                max_rounds=max_rounds, jobs=jobs, exts=exts,
                                margin=margin, producer=PRODUCER)
    with timer.stage("parse"):
        program = parse_eztrieve(source, resolver=pre.resolver(),
                                 source_name=source_name, program_name=program_name,
                                 margin=margin)

    lookup = (SynonymLookup(synonyms, synonym_resolver)
              if (synonyms or synonym_resolver is not None) else None)
    reverse = (DependentsLookup(dependents, dependents_resolver)
               if (dependents or dependents_resolver is not None) else None)
    analysis = ProgramAnalysis(program=program, prefetch=pre, source_name=source_name,
                               synonyms=lookup, dependents_lookup=reverse,
                               jcl_lineage=jcl_lineage, bind_steps=tuple(bind_steps))
    with timer.stage("field-lineage"):
        analysis.lineage()
    with timer.stage("artifacts"):
        art = analysis.artifacts()
    if jcl_lineage is not None:
        # Before the asks, not after: the datasets this closes are what gets asked about.
        with timer.stage("bind-jcl"):
            analysis.bound_artifacts()
    if reverse is not None:
        # Built here rather than on demand: building it is what ASKS the host, and a
        # gather run has to make the asks in order to record them.
        with timer.stage("dependents"):
            analysis.dependents()
    with timer.stage("fetch"):
        analysis.fetch = fetch_dependencies(art, fetcher, dest=dest,
                                            prefetched=pre.store,
                                            unavailable=unavailable, jobs=jobs,
                                            producer=PRODUCER)
    return analysis


def gather(source: str, *, source_name: str = "<eztrieve>",
           fetcher: Optional[Any] = None,
           paths: Sequence[str] = (), dest: str,
           unavailable: Optional[str] = None,
           margin: int = RIGHT_MARGIN, exts: Sequence[str] = (),
           max_rounds: int = 12, jobs: int = 1,
           dependents: Optional[Mapping[str, Sequence[dict]]] = None,
           dependents_resolver: Optional[Callable[..., Any]] = None,
           jcl_lineage: Optional[dict] = None,
           bind_steps: Sequence[str] = ()) -> str:
    """Run the retrieval half where the estate is reachable; return the bundle manifest.

    A dependents lookup is gathered like the artifact service: wrapped in a recorder,
    asked exactly as a live run asks it, and its answers written into the bundle. The
    index is as unreachable from the modelling box as the estate is.

    ``jcl_lineage`` is taken here for the same "exactly as a live run asks it" reason: it
    decides WHICH names the reverse direction is asked about, so a gather run without the
    binding the modelling run will have records answers to questions that run never asks.
    """
    recorder, answers = recording_fetcher(fetcher) if fetcher is not None else (None, [])
    reverse, reverse_answers = (recording_dependents_resolver(dependents_resolver)
                                if dependents_resolver is not None else (None, []))
    analysis = analyze(source, source_name=source_name, fetcher=recorder, paths=paths,
                       dest=dest, unavailable=unavailable, margin=margin, exts=exts,
                       max_rounds=max_rounds, jobs=jobs, dependents=dependents,
                       dependents_resolver=reverse, jcl_lineage=jcl_lineage,
                       bind_steps=bind_steps)
    return write_bundle(dest, subject_name=source_name, subject_text=source,
                        kind="eztrieve", prefetch=analysis.prefetch, answers=answers,
                        fetch=analysis.fetch, dependents=reverse_answers)

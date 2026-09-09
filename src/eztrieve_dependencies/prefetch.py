"""Stage 1 for Easytrieve: close over the macro members a program needs.

**Discovered by record-and-replay, not by scanning.** Parse the program with a resolver
that fetches nothing and merely records what it was asked for, retrieve those, then
re-parse with them in hand - repeating until the parse stops asking for anything new.

A lexical scan for ``%NAME`` would have been easy and would have been wrong: a macro
invokes macros, and the invocation only becomes visible once the outer macro's text is in
hand. Replaying the parse asks exactly the right questions in exactly the right order,
because the expander already funnels every external member through one call
(``macros.MacroExpander._resolve``).

Why it matters more here than anywhere else in this family of tools: an Easytrieve macro
routinely carries a **record layout**. Without it the file has no fields, so no statement
that references them resolves, so the field lineage - the entire point of this package - is
empty exactly where the program does its work, while the output still looks finished.

That is also the contract this file depends on, and it is easy to break from the other
side: anything that memoizes resolution inside the expander, short-circuits when the
resolver returns ``None``, or adds a second resolution path will silently shorten the
closure - no error, just a program that reads as though it had fewer fields than it has.
"""

from __future__ import annotations

from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from mainframe_artifacts.prefetch import (PrefetchResult, Prefetcher,  # noqa: F401
                                          member_key)

from .lexer import RIGHT_MARGIN
from .parser import parse_eztrieve

#: Extensions tried on the local search path before the estate is asked. The shared engine
#: already tries the COBOL/JCL ones; these are the Easytrieve names a macro member is kept
#: under, and without them a macro sitting right beside the program would be reported
#: MISSING and then fetched from the estate - shadowing the local file the ``-I`` flag
#: pointed at.
MACRO_EXTS: Tuple[str, ...] = (".mac", ".MAC", ".ezm", ".EZM", ".ezt", ".EZT",
                               ".ezy", ".EZY", ".eas", ".EAS")


def prefetch_eztrieve(source: str, fetcher: Optional[Callable],
                      paths: Optional[List[str]] = None, dest: Optional[str] = None,
                      source_name: str = "<eztrieve>", max_rounds: int = 12,
                      unavailable: Optional[str] = None,
                      result: Optional[PrefetchResult] = None,
                      jobs: int = 1,
                      exts: Sequence[str] = (),
                      margin: int = RIGHT_MARGIN,
                      seen: Optional[Iterable[str]] = None,
                      producer: Optional[str] = None) -> PrefetchResult:
    """Close over the macro members a program needs, by replaying the parse until it stops
    asking for members it has not got.

    No type hint is passed: the estate service auto-detects, and its ``detected_type`` is a
    better answer than anything we could infer from a ``%NAME`` invocation.
    """
    pf = Prefetcher(fetcher, paths, dest, unavailable, result,
                    exts=tuple(exts) + MACRO_EXTS, seen=seen,
                    producer=producer)
    pf.name_source(source_name)

    for _ in range(max_rounds):
        asked: List[str] = []

        def recording(name: str, _asked=asked) -> Optional[str]:
            _asked.append(name)
            return pf.store_text(name) or pf.result.resolver()(name)

        parse_eztrieve(source, resolver=recording, source_name=source_name,
                       margin=margin)
        fresh = [n for n in asked if member_key(n) not in pf.seen]
        if not fresh:
            break
        # One round IS a level: everything the parse asked for this time round was asked
        # for before any of it came back, so it can all be retrieved together.
        pf.obtain_wave([(n, "referenced by the program (%MACRO)") for n in fresh],
                       None, jobs)
    else:
        pf.note_closure_bound(max_rounds)
    return pf.result

"""eztrieve_dependencies - recover what an Easytrieve program does to which BYTES.

Easytrieve sits exactly between the two things the sibling tools model. Like COBOL it is a
program: it assigns, branches, loops, calls and writes. Like JCL it binds a name to
something outside itself - its ``FILE PERSNL`` names a **ddname**, and only the JCL says
which dataset that is. And unlike either, it declares its record layouts positionally in
the same source file, so ``GROSS 100 4 P 2`` is bytes 100-103 of the PERSNL record with no
copybook, DCLGEN or control card to resolve first.

That is why field-level lineage is worth extracting here in particular. Both ends of every
edge already have a concrete byte range, so ``PERSNL bytes 100-103 -> PAYEXT bytes 25-30,
through ANNUAL-PAY, multiplied by 12, only when STATUS = 'A'`` is a provable statement
about the program rather than an inference across three artifacts.

This package answers that question and retrieves what it needs to answer it. It is a PEER
of ``cobol_xstate`` and ``jcl_dependencies``, not a part of either: the three share only
the estate-retrieval half (``mainframe_artifacts``), so an Easytrieve run carries neither a
COBOL modelling engine nor a JCL parser, and none of them imports another. The one place
they meet - binding this program's ddnames to real datasets - is
:func:`eztrieve_dependencies.views.bind_jcl_ddnames`, which takes a plain JCL lineage
**dict**. That is deliberate, and it is what keeps this direction of the dependency from
existing at all.

The crawl is an ASSEMBLY chain: a ``%MACRO`` can carry a whole record layout, a whole
activity, and further macros. Parsed without them the files have no fields, so nothing that
references them resolves and the lineage is empty exactly where the program does its work -
while still looking finished. So stage 1 retrieves them before the parse, by replaying the
parse until it stops asking for members it has not got.
"""

import logging as _logging

#: This package's top-level logger name. The CLI passes it - alongside mainframe-artifacts'
#: own root - to ``mainframe_artifacts.logging_setup.configure_logging``; a root nobody
#: configures either propagates to the root logger or prints via logging's lastResort.
PACKAGE_LOGGER = "eztrieve_dependencies"

_logging.getLogger(PACKAGE_LOGGER).addHandler(_logging.NullHandler())

#: The name this distribution publishes under. It names both views' ``format`` and, passed
#: down to ``mainframe_artifacts``, the two shared retrieval reports - which hardcoded
#: "cobol-xstate" for every front-end until upstream ledger batch 10, item 30c.
PRODUCER = "eztrieve-dependencies"

#: Bumped when a published view's shape changes in a way a consumer must notice. Additive
#: keys do NOT bump it; a removed or re-meaning key does.
#:
#: Starts at 3, not 1, and family-wide: cobol-xstate's lineage view had been publishing
#: ``formatVersion: 2`` on its own since conditions moved into interned pools, so 1 would
#: have taken a published number BACKWARDS - the exact silent shape change this key exists
#: to prevent. One number across the family keeps a consumer's rule the same everywhere.
VIEW_SCHEMA_VERSION = 3

from .api import ProgramAnalysis, analyze, gather                      # noqa: E402
from .detect import looks_like_easytrieve                              # noqa: E402
from .lineage import Edge, LineageGraph, build_graph                   # noqa: E402
from .model import (Activity, Field, FileDef, Procedure, Program,      # noqa: E402
                    ReportDef, Statement)
from .parser import parse_eztrieve                                     # noqa: E402
from .prefetch import prefetch_eztrieve                                # noqa: E402
from .views import (bind_jcl_ddnames, build_eztrieve_artifacts,        # noqa: E402
                    build_eztrieve_lineage, jcl_steps_running)

#: The version of the JCL-lineage contract :func:`bind_jcl_ddnames` binds against - the
#: ``ddBindings`` rows of ``jcl-dependencies-lineage``. Anything driving both packages
#: should check it, because a skewed pair fails INVISIBLY otherwise: an unbound manifest
#: looks fine, since its file rows say exactly what an unbound run's rows say.
JCL_BINDING_API_VERSION = 1

__all__ = [
    "analyze", "gather", "ProgramAnalysis",
    "parse_eztrieve", "Program", "Activity", "FileDef", "Field", "Statement",
    "Procedure", "ReportDef",
    "build_graph", "LineageGraph", "Edge",
    "build_eztrieve_lineage", "build_eztrieve_artifacts", "bind_jcl_ddnames",
    "jcl_steps_running", "prefetch_eztrieve", "looks_like_easytrieve",
    "PACKAGE_LOGGER", "JCL_BINDING_API_VERSION",
]

__version__ = "0.1.0"

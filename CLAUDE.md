# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Parses CA-Easytrieve (Easytrieve Plus) and extracts field-level dependency lineage: which
bytes of which record become which bytes of which other record, through working storage,
under which conditions, out to which ddname. See README.md for the user-facing description.

## Setup

The one dependency, `mainframe-artifacts`, ships from the **mainframe-common** repository
and is normally a sibling checkout rather than an install:

```
code/
  mainframe-common/mainframe-artifacts/    <- the dependency
  jcl-dependencies/                        <- peer
  eztrieve-dependencies/                   <- here
```

`tests/_mainframe_common.py` puts the sibling's `src` on `sys.path` when the distribution
is not installed; override the location with `MAINFRAME_COMMON_REPO`. When neither is
found, `tests/conftest.py` ignores every module except `test_sibling_distribution.py`, so
the run ends as one clean skip naming the pip command instead of a wall of collection
errors. `tools/byteproof.py` performs the same discovery.

No install is needed to run the suite — `pyproject.toml` sets `pythonpath = ["src",
"tests"]`.

## Commands

```bash
python -m pytest -q                                    # the suite
python -m pytest tests/test_lineage.py -q               # one file
python -m pytest tests/test_lineage.py::test_a_field_built_only_from_literals_still_reports_what_decides_it -q
python -m pytest -q -k "macro"                          # by name

python tools/byteproof.py --check goldens/views.sha256  # byte-stability ratchet
python tools/byteproof.py --record goldens/views.sha256 # re-record (deliberately only)
python tools/refresh_fixture.py --check                 # the cross-repo JCL fixture
python -m pyflakes src/eztrieve_dependencies/*.py tools/*.py tests/*.py
```

Running the tool itself. `pyproject.toml`'s `pythonpath` applies to **pytest only**, so a
bare `python -m eztrieve_dependencies` fails with `No module named` unless the package is
installed or the path is given explicitly:

```bash
# installed (also gives you the `eztrieve-dependencies` console script)
python -m pip install -e ../mainframe-common/mainframe-artifacts -e .

# or, without installing anything — note `;` is the separator on Windows, `:` elsewhere
export PYTHONPATH="src;tests;../mainframe-common/mainframe-artifacts/src"
```

Then (`--no-fetch` because the real estate client is not installed here; a macro member is
found automatically beside the program, `-I DIR` adds more places to look):

```bash
python -m eztrieve_dependencies examples/payroll.ezt --outdir ./out --no-fetch --summary
python -m eztrieve_dependencies examples/payroll.ezt --outdir ./out --no-fetch \
    --bind-jcl tests/fixtures/payroll.jcl.lineage.json
python -m eztrieve_dependencies examples/macroed.ezt --outdir ./out --jobs 1 \
    --fetcher fakes.estate:fetch_artifact          # needs tests/ on the path
# Db2 SYNONYM/ALIAS -> base table (mainframe_artifacts.cliargs.add_synonym_args, shared
# with cobol-xstate): a map file and/or a resolver MODULE:FUNC asked once per table
# name; a 'db2-table' row written under a synonym gains baseTable/resolvedVia. Neither
# is a default, and a resolver that raises is a flagged failed lookup.
python -m eztrieve_dependencies prog.ezt --outdir ./out --no-fetch \
    --synonym-map synonyms.json --synonym-resolver mycatalog:resolve
```

`tests/fakes/estate.py` is the deterministic stand-in for the estate service.

**Verify from a fresh clone, not only in place.** The ratchet cannot catch a
checkout-dependent difference in the directory the goldens were recorded in. Clone the repo
to a temp dir and run `pytest` plus `byteproof --check` there before trusting a change.

## Architecture

One pipeline, each stage ignorant of the next:

```
lexer.py    physical text -> logical statements   (columns 1-72, +/- continuation,
                                                   TABLE INSTREAM data kept out of code)
macros.py   %MACRO expansion via the caller's resolver
parser.py   statements -> model.py Program        (files, fields, activities, conditions)
lineage.py  Program -> LineageGraph               (edges, sinks, access, the backward walk)
views.py    LineageGraph -> the two JSON views + the JCL join
```

`api.py` wires prefetch → parse → views → fetch; `cli.py` is a thin front end over it.
Nothing below `views.py` knows about JSON, and nothing above `parser.py` decides lineage.

### Invariants that span files

**Three peer packages, no imports between them.** `cobol_xstate` says what a program does,
`jcl_dependencies` says which dataset a ddname is, this says which bytes become which. They
share only `mainframe_artifacts` and meet at plain dicts. `tests/test_boundaries.py` runs
child interpreters with those packages blocked *and* greps every module's import lines — a
single stray import would make this repo unreleasable on its own.

**The resolver is the sole route to an external member.** `prefetch.py` closes over a
program's macros by *replaying the parse* under a recording resolver until it stops asking.
That works only while `macros.MacroExpander._resolve` is the one call that reaches outside.
Memoizing resolution inside the expander, short-circuiting when the resolver returns `None`,
or adding a second resolution path **silently shortens the closure** — no error, just a
program that reads as though it had fewer fields than it has. A macro routinely carries a
whole record layout, so a short closure produces an empty lineage that still looks finished.

**Node keys.** The graph is keyed on strings, and the format is load-bearing:

| node | key |
|---|---|
| record field | `PERSNL:GROSS` (`Field.key`, owner-qualified) |
| working storage | `ANNUAL-PAY` (bare — no owner to qualify with) |
| report column | `@PAYRPT.LINE1.3`, `@PAYRPT.SUM.ANNUAL-PAY`, `.CONTROL.`, `.SEQUENCE.`, `.TITLE1.` |
| DISPLAY | `@DISPLAY.<file-or-SYSPRINT>.<line>` |

`graph.field_index()` maps field keys back to `Field`s; anything starting with `@` is in
`graph.sinks`.

**Where the backward walk stops** (`FlowWalker.is_origin`): a file this program only
*reads* is the boundary. A file it reads *and* writes is **not** — a sort work file is
written by the SORT and read by the report, and stopping there ends every chain one hop
short of the real input. Changing this rule silently truncates every flow.

**Ambiguous names are resolved by activity scope, and say so.** A sort work file declared
with `COPY OTHERFILE` carries every field name the original has, making every unqualified
reference ambiguous by the letter of the rule. `Symbols.resolve(token, scope)` reads it
against the record the activity is working on, sets `Resolution.scoped`, and
`_Builder._note_scoped` flags it. Every route to a field — statement operands *and*
condition tests — must go through `_note_scoped`, or scoped resolutions become silent.

**Conditions are dependencies, kept apart from value flow.** The parser stamps the open
`IF`/`CASE`/`DO` nesting onto each `Statement`; `_Builder._conds` resolves the tested names
to field keys; the walk collects them as `influencedBy`, separate from `sources`. A field
assigned only literals inside an `IF` has no value origin but is still decided by the field
the `IF` tests — reporting "no origin" would be true about the value and false about the
dependency.

**Deliberately not traversed, and it says so rather than doing it quietly:** positional
field overlaps (a group and its elements share bytes by design — traversing makes every
field depend on every other), `CALL` arguments beyond marking them opaque, SQL columns
inside a statement. Only an *explicit* redefinition becomes an alias edge.

**The JCL join matches the SYSIN member, not `EXEC PGM=`.** An Easytrieve step runs
`PGM=EZTPA00` — the interpreter — so the step's program name cannot identify which program
runs. `jcl_steps_running` reads the per-step DD list of a `jcl-dependencies-lineage` dict.
It deliberately does **not** use the JCL *artifacts* view, whose control-card rows are keyed
on DSN alone and so collapse two members of one library into a single row naming both steps.

## Output is the contract

Every byte of both views is output. Key order, list order, the presence of a note, the
wording of a flag — none of it is asserted by the test suite, so `tools/byteproof.py` is
what actually guards it. A refactor that should not change output must produce identical
bytes; re-record goldens only when a change is intended and reviewed.

`.gitattributes` pinning `eol=lf` is load-bearing for this, not cosmetic: a member resolved
from the local search path is read as bytes and its length goes into the retrieval report,
so a CRLF checkout shifts every one of those counts.

`tests/fixtures/payroll.jcl.lineage.json` is a committed JCL lineage view — this repo's half
of a cross-repository contract, so the tests need no JCL install.
`tools/refresh_fixture.py --check` asserts the shape the binder reads and is run by the
suite, because a drifted contract does not fail loudly: an unbound manifest looks exactly
like one nobody tried to bind.

## Honesty discipline

Nothing is guessed. Anything unresolved is surfaced rather than smoothed over — unresolved
macros, unsupplied macro parameters (left visible as `&NAME`, never blanked), unmodelled
statement verbs, unbalanced control nesting, ambiguous references, text past column 72.
When adding a feature, the question to answer is "what does this tool *not* know here, and
does the output say so?" A flag naming a gap is worth more than a plausible value.

New statement verbs go in `parser._VERBS` **and** get a `_Builder._do_<verb>` handler;
listing a verb without a handler makes it silently unmodelled with no flag.

Adding a new artifact `kind` to the manifest requires a matching entry in
`mainframe_artifacts.fetch._KIND_TYPE` (and `artifact_service.EXT_FOR_TYPE`) in the
mainframe-common repo — otherwise stage 2 reports it `skipped: no known retrieval type`,
which is false for anything stage 1 already retrieved.

# eztrieve-dependencies

Parse CA-Easytrieve (Easytrieve Plus) programs and recover **which bytes of which record
become which bytes of which other record** — through however many working-storage hops,
under whichever conditions, and out to which ddname.

Easytrieve sits exactly between the two things the sibling tools model. Like COBOL it is a
program: it assigns, branches, loops, calls and writes. Like JCL it binds a name to
something outside itself — its `FILE PERSNL` names a **ddname**, and only the JCL says
which dataset that is. And unlike either, it declares its record layouts **positionally,
in the same source file**:

```
FILE PERSNL FB(150 1800)
  GROSS  100  4 P 2
```

`GROSS` is bytes 100–103, packed, two decimals. No copybook, DCLGEN or control card has to
be resolved before that is known — which is why field-level lineage is worth extracting
here specifically. Both ends of every edge already have a concrete byte range.

## Install

```bash
pip install eztrieve-dependencies
```

It depends on `mainframe-artifacts` (the estate boundary and the two-stage dependency
retrieval, shared with the COBOL and JCL tools) and on nothing else. Pure Python standard
library, Python ≥ 3.9.

`mainframe-artifacts` ships from the
[mainframe-common](https://github.com/paulhowarda-bit/mainframe-common) repository (one
repo, several distributions; its `mainframe-artifacts/` subdirectory). Until it is on an
index, install it straight from that repo:

```bash
pip install "mainframe-artifacts @ git+https://github.com/paulhowarda-bit/mainframe-common#subdirectory=mainframe-artifacts"
```

**It depends on neither `cobol-xstate` nor `jcl-dependencies`.** The three are peers: the
COBOL says what a program does, the JCL says which dataset a ddname is, and this says which
bytes become which. `tests/test_boundaries.py` enforces that none of them is importable
from here.

## Use

```bash
eztrieve-dependencies payroll.ezt                    # 2 views + both retrieval reports -> ./out
eztrieve-dependencies payroll.ezt --target lineage   # just the field lineage
eztrieve-dependencies payroll.ezt --summary          # + a human summary on stderr

# Close the ddnames against the JCL that runs it
eztrieve-dependencies payroll.ezt --bind-jcl out/payroll.jcl.lineage.json

# The REVERSE direction - what the estate says depends on this program - arrives through
# a door and is never derived here. A third view is written only when one is open.
eztrieve-dependencies payroll.ezt --dependents-map index.json
eztrieve-dependencies payroll.ezt --dependents-resolver myindex:lookup \
                                  --bind-jcl out/payroll.jcl.lineage.json

# Gather where the estate is reachable, model where it is not
eztrieve-dependencies payroll.ezt --gather-only ./bundle
eztrieve-dependencies payroll.ezt --from-bundle ./bundle    # no network at all

# Db2 SYNONYM/ALIAS -> base table, as catalog knowledge the host supplies (never
# guessed): a map file, a callable asked per table name, or both (the map wins). A
# 'db2-table' row written under a synonym then also carries 'baseTable'.
eztrieve-dependencies prog.ezt --synonym-map synonyms.json --synonym-resolver mycatalog:resolve
```

As a library:

```python
from eztrieve_dependencies import analyze

p = analyze(open("payroll.ezt").read(), source_name="payroll.ezt", retrieve=False)
p.lineage()        # record layouts, field edges, end-to-end field flow, file dataflow
p.artifacts()      # every file (by ddname), macro, called program and table
p.bind(jcl_lineage)  # close the ddname -> dataset join from a JCL model
p.dependents()     # what the estate says depends on it - None when no door was opened
```

## What the lineage actually says

Three layers, each answering a different question.

**`fieldLineage` — statement level.** One row per assignment, `MOVE`, `MOVE LIKE`, table
lookup, sort copy, explicit redefinition or report reference: which field it wrote, which
fields it read, by what means, and under which conditions.

**`fieldFlow` — end to end.** For every *sink* — a field of a file the program writes, a
report column, a `DISPLAY` — the chain back to the input-file fields it derives from. This
is the row a modernization wants, and no single statement contains it:

```
PERSNL.GROSS  bytes 100-103, P 2
  -> ANNUAL-PAY  (assignment, × 12, only when STATUS = 'A')
  -> PAYEXT.PX-ANNUAL  bytes 25-30, P 2
```

**`fileFlow` — file level.** The same edges aggregated to ddname, so the program reads as a
dataflow the JCL can then resolve to datasets.

### Conditions are dependencies, and are kept separate from values

`PX-GRADE` is only ever assigned the literal `'H'` or `'L'`. Its *value* comes from no
field at all — but which literal is chosen depends on `ANNUAL-PAY`, which came from
`GROSS`. A lineage that reported only value flow would say `PX-GRADE` has no origin, which
is false in every sense a reader cares about. So each sink carries `influencedBy`: the
fields tested in the `IF` / `CASE` / `DO` nesting that gates it, each traced back to *its*
own origins — kept apart from the value sources rather than mixed in with them.

### What is deliberately not traversed, and says so

Fields in one Easytrieve record **overlap by design** — a group and its elements share
bytes. Treating every positional overlap as an edge would make every field of a record
depend on every other, and the output would be true and useless. So overlaps are *listed*
on each field, and only an explicit redefinition (a declaration whose location operand
names another field) becomes a traversable alias edge.

Likewise: a `CALL` may change any argument, so those fields are marked opaque and a path
through one says so. A `POINT` decides *which record* is read, not what a value is computed
from, and is recorded as its own kind of edge. `SEARCH` returns the table's description
while the argument decides the row — two dependencies, kept distinct. An `EXIT(...)` on a
file sees every record on its way past and can change any field of it, and none of that is
in the lineage — the manifest row says so.

## Following the macros, and why it is not optional

An Easytrieve `%MACRO` routinely carries a **record layout**, and sometimes a whole
activity. Parsed without it the file has no fields, so nothing that references them
resolves, and the field lineage is empty exactly where the program does its work — while
still looking like a finished answer.

So stage 1 retrieves the macros before the parse, by replaying the parse until it stops
asking for members it has not got. That closure is discovered by **record-and-replay, not
by scanning**: a macro invokes macros, and the inner invocation only becomes visible once
the outer macro's text is in hand.

## The one place it meets the JCL tool

`bind_jcl_ddnames(manifest, jcl_lineage)` resolves this program's file ddnames against a
JCL job's `ddBindings`. It takes a plain **dict** and returns one, so this package imports
nothing from the JCL side.

Finding *which* step to bind from is the interesting part. An Easytrieve step reads

```
//RUNPAY   EXEC PGM=EZTPA00
//SYSIN    DD  DSN=PROD.EZTSRC(PAYROLL),DISP=SHR
//PERSNL   DD  DSN=PROD.HR.PERSNL.MASTER,DISP=SHR
```

— so the step's `EXEC PGM=` is the Easytrieve *interpreter*, not the program. What
identifies which program runs is the **SYSIN member**, and that is what the join matches
on. A job that runs three Easytrieve programs has three different meanings for `PERSNL`;
binding on ddname alone would pick whichever came last. When no step can be identified, the
binding is still made — on ddname alone — and a flag says exactly that.

`tests/fixtures/payroll.jcl.lineage.json` is a committed JCL lineage view — this
repository's half of that contract, so its tests need no JCL install.
`tools/refresh_fixture.py --check` asserts the shape the binder reads, and the test suite
runs it, so a schema change is a red test rather than a quietly unbound manifest (which
looks *fine* — an unbound manifest says exactly what a manifest nobody tried to bind says).

`JCL_BINDING_API_VERSION` in `eztrieve_dependencies/__init__.py` is the contract version.

## The reverse direction, and why it needs that same binding

Every view above answers *what does this program name?* The opposite question — *what
depends on this program?* — is not in the source at all. It lives in an estate-wide index,
so it arrives through a door (`--dependents-map`, `--dependents-resolver`, or
`analyze(dependents=, dependents_resolver=)`) and is reported exactly as given. **Three
answers, and they are kept apart:** no door opened writes no view at all; an empty
`dependents` list means the index was asked and nothing depends on the name; `unanswered`
means neither.

Two things can be depended on: the program itself, which a job runs by naming this member
as `EZTPA00`'s SYSIN, and the data it **writes**. The second is the difficult one. This
source names only a **ddname**, which is program-local — the same spelling in another
program means something unrelated — so there is no estate-wide index that could be keyed
on it, and matching one anyway would mint a dependency between every program that happens
to use the same three letters.

So the written half is asked about as the **dataset** the JCL binds that ddname to, using
the binding above:

```bash
eztrieve-dependencies payroll.ezt --bind-jcl payroll.jcl.lineage.json \
                                  --dependents-resolver myindex:lookup
# asks about PAYROLL (program) and PROD.FIN.PAY.EXTRACT (dataset) - never about PAYEXT
```

Without a binding the ddname is reported in `unanswered` with `asked: false` and what would
close it, rather than asked about under a name that exists nowhere outside this source. A
ddname bound to different datasets in different steps is asked about once per dataset —
the `datasetCandidates` rule the binding applies is not collapsed by the view that consumes
it.

## Honest limits, all in `flags` rather than guessed

* Source is read in **columns 1–72**, as the compiler reads it. A non-sequence-number tail
  past the margin is reported, not silently dropped; `--right-margin 0` scans whole lines.
* A statement whose **verb is not modelled** is recorded with a flag naming it — an
  unmodelled verb that moves data is a hole in the lineage, and a silent one is worthless.
* A **reference declared in more than one file** is read against the record the activity is
  working on (which is what Easytrieve does, and what a `COPY`-derived sort work file makes
  unavoidable) — and every such resolution is flagged with the qualification that would
  remove the doubt.
* **Unbalanced** `IF` / `DO` / `CASE` nesting is flagged, because it makes every condition
  after it wrong.
* A **macro parameter that was not supplied** is left visible as `&NAME` and flagged, never
  blanked — a field silently declared at position `''` is a layout that is *wrong* rather
  than absent.
* **SQL** contributes its table names; the column-to-host-field mapping inside a statement
  is not modelled, and host fields it sets say so. A table written under a Db2
  SYNONYM/ALIAS is reported as written, and gains its `baseTable` only when the host
  supplies the catalog's knowledge (`--synonym-map`, `--synonym-resolver`, or
  `analyze(synonyms=, synonym_resolver=)`) — the join lives in the catalog, so it is
  never guessed here.
* `MOVE` written without `TO` takes the last operand as the target, and says it did.

## Development

```bash
# mainframe-artifacts comes from a sibling mainframe-common checkout (or the git+ line above)
python -m pip install -e ../mainframe-common/mainframe-artifacts -e .
python -m pytest -q
python tools/byteproof.py --check goldens/views.sha256   # byte-stability ratchet
python tools/refresh_fixture.py --check                  # the JCL contract fixture
```

From a bare dual-checkout — mainframe-common beside this repo, nothing installed — the
suite and the ratchet find `../mainframe-common/mainframe-artifacts` automatically
(override with `MAINFRAME_COMMON_REPO`); without either, the suite ends as one clean skip
naming the exact pip command.

Output is byte-stable and deterministic: a refactor that should not change output must
produce identical bytes, and a green test run does not prove that — the order of an
`origins` list, the presence of a note and the key order of a hop are all output, and none
of them is asserted anywhere. The ratchet hashes every view of every example. Re-record
only when an output change is intended and reviewed.

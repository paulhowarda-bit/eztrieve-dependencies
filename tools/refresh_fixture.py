#!/usr/bin/env python3
"""Regenerate the committed jcl-dependencies lineage fixture, and check its shape.

``tests/fixtures/payroll.jcl.lineage.json`` is this repository's half of a cross-repository
contract. ``bind_jcl_ddnames`` reads two things out of a JCL lineage view - the per-step DD
list (to find the step whose SYSIN names this program) and ``ddBindings`` (to get the
datasets) - and if either changes shape, the binding does not fail: it quietly binds
nothing, and an unbound manifest looks exactly like a manifest nobody tried to bind.

So the fixture is committed rather than built live (this package must not depend on the JCL
one - they are peers), and ``--check`` asserts the shape the binder actually relies on.
``tests/test_artifacts.py`` runs the same check, so drift is a red test.

    python tools/refresh_fixture.py --check
    python tools/refresh_fixture.py --record     # needs a jcl-dependencies checkout

The recording path finds jcl-dependencies as a sibling checkout (override with
JCL_DEPENDENCIES_REPO) or installed. Without either, --record says so and --check still
works, which is the point of committing the file.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import List

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"
FIXTURE = REPO / "tests" / "fixtures" / "payroll.jcl.lineage.json"
SOURCE_JCL = EXAMPLES / "payroll.jcl"

CHECKOUT = Path(os.environ.get("JCL_DEPENDENCIES_REPO",
                               REPO.parent / "jcl-dependencies"))


def check(path: Path = FIXTURE) -> List[str]:
    """The shape ``bind_jcl_ddnames`` depends on. Returns the problems, empty if sound."""
    problems: List[str] = []
    if not path.is_file():
        return ["{0} is missing - run --record".format(path)]
    try:
        view = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return ["{0} is not readable JSON: {1}".format(path, exc)]

    if view.get("format") != "jcl-dependencies-lineage":
        problems.append("format is {0!r}, expected 'jcl-dependencies-lineage'".format(
            view.get("format")))
    for key in ("job", "source", "steps", "ddBindings"):
        if key not in view:
            problems.append("the view has no {0!r} - the binder reads it".format(key))

    steps = view.get("steps") or []
    if not steps:
        problems.append("'steps' is empty - the SYSIN member is found from it")
    sysin_members = set()
    for srow in steps:
        if "step" not in srow:
            problems.append("a step row has no 'step' name")
        for dd in list(srow.get("inputs") or []) + list(srow.get("outputs") or []):
            if str(dd.get("ddname", "")).upper() == "SYSIN" and dd.get("member"):
                sysin_members.add(dd["member"])
    if not sysin_members:
        problems.append(
            "no step carries a SYSIN DD with a 'member' - that is how a step is identified "
            "as running a particular Easytrieve program, so the join would silently bind "
            "on ddname alone")

    bindings = view.get("ddBindings") or []
    if not bindings:
        problems.append("'ddBindings' is empty - it is what supplies the datasets")
    for b in bindings:
        for key in ("program", "step", "ddname", "dataset", "io"):
            if key not in b:
                problems.append("a ddBindings row has no {0!r}".format(key))
                break
    if not any(b.get("generation") for b in bindings):
        problems.append("no ddBindings row carries a 'generation' - the fixture no longer "
                        "covers the GDG case the binder carries across")
    return problems


def record() -> int:
    spec = importlib.util.find_spec("jcl_dependencies")
    if spec is None:
        src = CHECKOUT / "src"
        if not (src / "jcl_dependencies" / "__init__.py").is_file():
            print("error: jcl_dependencies is neither installed nor found in a checkout "
                  "at {0} - install it, or set JCL_DEPENDENCIES_REPO. The committed "
                  "fixture still works without it; only --record needs it.".format(
                      CHECKOUT), file=sys.stderr)
            return 2
        sys.path.insert(0, str(src))

    from jcl_dependencies.parser import parse_jcl              # noqa: E402
    from jcl_dependencies.views import build_jcl_lineage       # noqa: E402

    # No resolver: the fixture must not depend on what an estate happened to answer.
    job = parse_jcl(SOURCE_JCL.read_text(encoding="utf-8"), resolver=None,
                    source_name=SOURCE_JCL.name)
    view = build_jcl_lineage(job)
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(view, indent=2) + "\n", encoding="utf-8")
    print("recorded {0}".format(FIXTURE))
    problems = check()
    for p in problems:
        print("  WARNING {0}".format(p), file=sys.stderr)
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--record", action="store_true")
    group.add_argument("--check", action="store_true")
    args = ap.parse_args()
    if args.record:
        return record()
    problems = check()
    if problems:
        print("FIXTURE CONTRACT FAILURE: {0} problem(s)\n".format(len(problems)),
              file=sys.stderr)
        for p in problems:
            print("  {0}".format(p), file=sys.stderr)
        return 1
    print("fixture sound: {0}".format(FIXTURE))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

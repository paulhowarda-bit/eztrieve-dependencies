"""Telling an Easytrieve program from the neighbouring languages.

The estate keeps Easytrieve, COBOL and JCL in the same libraries and often under the same
naming conventions, so a front-end that assumes what it was handed produces a confident
answer about the wrong language. This says something useful instead.

Easytrieve knowledge lives here rather than in ``mainframe_artifacts`` deliberately: core
is ignorant of its consumers, and ``FILE PERSNL FB(150 1800)`` is a fact about this
language, not a shared one.
"""

from __future__ import annotations

import re
from typing import Tuple

_SUFFIXES = ("ezt", "ezy", "eas", "ezp", "easytrieve", "eztrieve", "mac", "ezm")

#: Statements that only an Easytrieve program has. Two of these, in a member that is
#: neither JCL nor COBOL, is enough - one alone is not (``FILE`` and ``JOB`` are words that
#: turn up in a lot of text).
_SIGNALS = (
    re.compile(r"^JOB\b.*\bINPUT\b", re.I),
    re.compile(r"^SORT\b.+\bTO\b.+\bUSING\b", re.I),
    re.compile(r"^REPORT\s+[A-Z@#$][A-Z0-9@#$_-]*", re.I),
    re.compile(r"^FILE\s+[A-Z@#$][A-Z0-9@#$_-]*", re.I),
    re.compile(r"^DEFINE\s+[A-Z@#$][A-Z0-9@#$_-]*\s+[WS0-9*]", re.I),
    re.compile(r"^END-(IF|DO|PROC|CASE)\b", re.I),
    re.compile(r"^[A-Z@#$][A-Z0-9@#$_-]*\.\s+PROC\b", re.I),
    re.compile(r"^PRINT\s+[A-Z@#$][A-Z0-9@#$_-]*\s*$", re.I),
    re.compile(r"^%[A-Z@#$][A-Z0-9@#$_-]*", re.I),
    re.compile(r"^(SEQUENCE|CONTROL|LINE|TITLE|HEADING)\s+", re.I),
)

_JCL = re.compile(r"^//\S*\s+(JOB|PROC|EXEC|DD)\b", re.I)
_COBOL = re.compile(r"\b(IDENTIFICATION\s+DIVISION|PROGRAM-ID)\b", re.I)


def classify(source_name: str, source: str) -> Tuple[bool, str]:
    """``(is_easytrieve, why)`` - the reason is what a warning would say."""
    suffix = str(source_name).lower().rsplit(".", 1)[-1]
    if suffix in _SUFFIXES:
        return True, "recognised by its .{0} extension".format(suffix)

    hits = 0
    for raw in source.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.startswith("*"):
            continue
        stripped = line.strip()
        if _JCL.match(stripped):
            return False, ("this looks like JCL (a //NAME JOB/PROC/EXEC/DD statement). "
                           "Use jcl-dependencies.")
        if _COBOL.search(stripped):
            return False, ("this looks like COBOL (an IDENTIFICATION DIVISION or "
                           "PROGRAM-ID). Use the COBOL tool.")
        for pattern in _SIGNALS:
            if pattern.match(stripped):
                hits += 1
                break
        if hits >= 2:
            return True, "recognised by its Easytrieve statements"
    return False, ("this does not look like Easytrieve (no FILE / DEFINE / JOB INPUT / "
                   "REPORT statements were found)")


def looks_like_easytrieve(source_name: str, source: str) -> bool:
    return classify(source_name, source)[0]

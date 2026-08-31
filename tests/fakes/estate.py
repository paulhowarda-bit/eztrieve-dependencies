"""A deterministic stand-in for the estate's artifact service (mf-fetch).

The real default client is ``mf_fetch:fetch_artifact`` - an external library
that talks to a mainframe share. Nothing here can reach it, so without a stand-in the
retrieval reports are untestable and the byte-stability ratchet could not cover them.

Answers from a fixed table, so a run is reproducible on any machine with no network. The
members cover what the Easytrieve closure actually exercises: a macro carrying a RECORD
LAYOUT (whose fields exist in no other file), a macro carrying STATEMENTS, a macro that
invokes another macro (so the closure needs a second round to find it), a name the estate
does not have, and a name whose REQUEST FAILS - which is not the same fact as absence and
must never be reported as one.
"""

from __future__ import annotations

# The record layout macroed.ezt asks for. Its fields exist in no other file, so a parse
# without it leaves EDITIN and EDITGOOD with no fields at all.
CUSTREC = (
    "MACRO 1 START\n"
    "  CU-NUMBER   &START     9 N\n"
    "  CU-NAME     *         30 A\n"
    "  CU-STATE    *          2 A\n"
    "  CU-BALANCE  *          5 P 2\n"
)

# Statements, not declarations - and it invokes a further macro, so the closure cannot
# reach STDLIMIT until CUSTEDIT itself has been retrieved.
CUSTEDIT = (
    "MACRO\n"
    "  IF CU-NAME = ' '\n"
    "     WS-ERRORS = WS-ERRORS + 1\n"
    "  END-IF\n"
    "%STDLIMIT\n"
)

STDLIMIT = (
    "MACRO\n"
    "  IF CU-BALANCE LT 0\n"
    "     WS-ERRORS = WS-ERRORS + 1\n"
    "  END-IF\n"
)

# What the shipped examples use directly.
EDITCHK = (
    "MACRO\n"
    "  IF CU-NAME = ' '\n"
    "     WS-ERRORS = WS-ERRORS + 1\n"
    "  END-IF\n"
    "  IF CU-BALANCE LT 0\n"
    "     WS-ERRORS = WS-ERRORS + 1\n"
    "  END-IF\n"
)

TABLE = {
    "CUSTREC": CUSTREC,
    "CUSTEDIT": CUSTEDIT,
    "STDLIMIT": STDLIMIT,
    "EDITCHK": EDITCHK,
}


class EstateRequestFailed(RuntimeError):
    """The request itself failed - credentials, connectivity, a service fault."""


def fetch_artifact(name, type=None, copy=None):        # noqa: A002 - the wire keyword
    """The mf-fetch calling convention: ``f(name, type=..., copy=...)``."""
    key = str(name).strip().strip("'\"").upper()
    if "(" in key:                                     # SRC.LIB(CUSTREC) -> the member
        key = key.split("(", 1)[1].rstrip(")")
    if key == "BOOM":
        raise EstateRequestFailed("estate share unreachable (simulated)")
    text = TABLE.get(key)
    if text is None:
        return {"artifact_name": key, "found": False}
    return {"artifact_name": key, "found": True, "text": text,
            "detected_type": "macro",
            "source_location": "PROD.EZTMACS({0})".format(key)}

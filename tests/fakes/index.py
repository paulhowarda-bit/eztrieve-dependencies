"""A fixed estate INDEX for the reverse direction, reachable as a --dependents-resolver.

``fakes.estate`` answers "what is this member?"; this answers the other direction, "what
depends on this name?". The CLI's door takes MODULE:FUNC, so exercising it end to end
needs a resolver that can be IMPORTED by name rather than passed in as a callable - which
is the whole point of the flag, and the reason a bundle has to record what it answered.

Deliberately narrow: it covers the program PAYROLL and the DATASET the payroll JCL binds
PAYEXT to, and nothing else. A name it does not cover returns None - NOT ANSWERED - which
is what keeps the three answers distinguishable in a test.
"""

#: Keyed (name, kind) exactly as the lookup asks: a ddname is never a key here, because a
#: ddname is not a name an estate index can hold.
INDEX = {
    ("PAYROLL", "program"): [
        {"name": "PAYRUN", "kind": "JOB", "via": "EXEC PGM=EZTPA00 SYSIN",
         "match_strength": "qualified", "detail": "step RUNPAY names PAYROLL as SYSIN"},
    ],
    ("PROD.FIN.PAY.EXTRACT", "dataset"): [
        {"name": "PAYPOST", "kind": "JOB", "via": "DD DSN=... DISP=SHR",
         "match_strength": "qualified"},
    ],
}


def dependents(name, kind=None):
    """What the index says depends on ``name``, or None for a name it does not cover."""
    return INDEX.get((name, kind))

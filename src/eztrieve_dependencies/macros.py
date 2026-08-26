"""Easytrieve macro expansion - the one place this package reaches outside the source file.

A ``%CUSTREC 1`` in the middle of a library section is not decoration. The macro it names
carries the FIELD DECLARATIONS of a record: parsed without it, the file has no fields, so
no statement referencing them resolves, so the program's lineage is not merely incomplete -
it is empty in exactly the places a modernization cares about, and it looks finished.
A macro can equally carry whole statements, whole FILE declarations, and further macros.

So this module does the same job ``jcl_dependencies.parser`` does for cataloged PROCs and
INCLUDE members, and follows the same two rules:

* **It does not fetch.** The caller supplies ``resolver(name) -> text | None``. What the
  resolver cannot return is FLAGGED, never guessed - a program with an invented record
  layout is worse than one that says which layout it is missing.
* **It is the sole route to an external member.** Everything reaches the estate through one
  call, which is what lets :mod:`eztrieve_dependencies.prefetch` close over a program by
  replaying the parse until it stops asking. Memoising resolution here, or adding a second
  path, would silently shorten that closure.

**Parameter binding.** A macro member opens with ``MACRO [n] [name ...]``: the optional
leading integer is how many of the named parameters are positional, and the rest are
keyword parameters supplied as ``NAME value`` pairs at the call. With no integer, every
declared parameter is positional. References are ``&NAME`` or ``&NAME.`` (the dot is a
terminator and is consumed), exactly as in JCL. A parameter the call does not supply is
left visible as ``&NAME`` and flagged - never blanked, because a field silently declared at
position ``''`` is a record layout that is wrong rather than absent.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from .lexer import LogicalLine, is_literal, logical_lines, tokenize
from .model import MacroUse

logger = logging.getLogger(__name__)

Resolver = Callable[[str], Optional[str]]

#: ``%NAME`` - a macro invocation. It occupies the whole statement.
_INVOKE = re.compile(r"^%\s*([A-Z@#$][A-Z0-9@#$_-]*)\s*(.*)$", re.I)

#: ``&NAME`` / ``&NAME.`` - a macro parameter reference.
_PARAM = re.compile(r"&([A-Z0-9@#$_]+)\.?", re.I)

#: How deep macro nesting may go before we call it a loop rather than a program.
MAX_DEPTH = 10


def substitute(text: str, values: Dict[str, str]) -> Tuple[str, List[str]]:
    """Replace ``&NAME`` / ``&NAME.`` from ``values``; return (text, unresolved names).

    An unresolved parameter is LEFT VISIBLE and reported. Blanking it would produce a field
    declaration that parses and is wrong, which is the one outcome worth more than a
    missing one."""
    unresolved: List[str] = []

    def repl(m: "re.Match") -> str:
        name = m.group(1).upper()
        if name in values:
            return values[name]
        if name not in unresolved:
            unresolved.append(name)
        return m.group(0)

    return _PARAM.sub(repl, text), unresolved


def parse_macro_header(lines: Sequence[LogicalLine]) -> Tuple[List[str], int, int]:
    """Read a macro member's ``MACRO`` statement.

    Returns ``(parameter names, positional count, index of the first body line)``. A member
    with no ``MACRO`` statement is a parameterless macro whose whole text is the body -
    which is legal and common for a macro that only carries fixed declarations.
    """
    for idx, ln in enumerate(lines):
        toks = ln.tokens()
        if not toks:
            continue
        if toks[0].upper() != "MACRO":
            return [], 0, 0
        rest = toks[1:]
        positional = None
        if rest and rest[0].isdigit():
            positional = int(rest[0])
            rest = rest[1:]
        names = [t.upper() for t in rest if t not in ("(", ")")]
        return names, (len(names) if positional is None else positional), idx + 1
    return [], 0, 0


def bind_arguments(names: Sequence[str], positional: int,
                   args: Sequence[str]) -> Tuple[Dict[str, str], List[str]]:
    """Bind a call's operands to the macro's parameters.

    The first ``positional`` parameters take the first operands in order; whatever is left
    is read as ``KEYWORD value`` pairs against the remaining parameter names. An operand
    that matches no parameter is reported rather than dropped."""
    values: Dict[str, str] = {}
    problems: List[str] = []
    pos_names = list(names[:positional])
    kw_names = {n for n in names[positional:]}

    i = 0
    for name in pos_names:
        if i >= len(args):
            break
        values[name] = args[i]
        i += 1
    while i < len(args):
        key = args[i].upper()
        if key in kw_names and i + 1 < len(args):
            values[key] = args[i + 1]
            i += 2
            continue
        problems.append(args[i])
        i += 1
    return values, problems


def _quote_strip(token: str) -> str:
    """A macro argument is substituted as TEXT. ``'PROD'`` supplies ``PROD``; the quotes are
    call syntax, and keeping them would put them into a field name or a byte position."""
    if is_literal(token):
        body = token[1:] if token[:1].upper() in ("X", "P", "B", "C", "Z") else token
        return body[1:-1].replace("''", "'")
    return token


class MacroExpander:
    """Expands ``%NAME`` invocations in place, recursively, recording what it could not do.

    One instance per parse. ``uses`` is the record of every invocation seen - resolved or
    not - which the artifact manifest turns into compile-time dependency rows.
    """

    def __init__(self, resolver: Optional[Resolver] = None, *,
                 margin: int = 72, max_depth: int = MAX_DEPTH):
        self.resolver = resolver
        self.margin = margin
        self.max_depth = max_depth
        self.uses: List[MacroUse] = []
        self.flags: List[str] = []
        self._expanding: Set[str] = set()

    # -- resolver plumbing --------------------------------------------------
    def _resolve(self, name: str) -> Optional[str]:
        if self.resolver is None:
            self.flags.append(
                "macro {0}: no resolver supplied - its declarations and statements are "
                "not in the model".format(name))
            return None
        try:
            got = self.resolver(name)
        except Exception as exc:            # a bad resolver must not crash the parse
            self.flags.append("macro {0}: resolver raised {1!r}".format(name, exc))
            logger.debug("Easytrieve macro resolver raised for %r", name, exc_info=True)
            return None
        if got is None:
            self.flags.append(
                "macro {0}: resolver returned nothing - its declarations and statements "
                "are not in the model".format(name))
        return got

    # -- expansion ----------------------------------------------------------
    def expand(self, lines: Sequence[LogicalLine]) -> List[LogicalLine]:
        """Return ``lines`` with every macro invocation replaced by its body."""
        return self._expand(lines, depth=0, in_macro=None)

    def _expand(self, lines: Sequence[LogicalLine], depth: int,
                in_macro: Optional[str]) -> List[LogicalLine]:
        out: List[LogicalLine] = []
        for ln in lines:
            if ln.is_data:
                out.append(ln)
                continue
            m = _INVOKE.match(ln.text.strip())
            if not m:
                out.append(ln)
                continue
            name = m.group(1).upper()
            args = [_quote_strip(t) for t in tokenize(m.group(2))
                    if t not in ("(", ")")]
            use = MacroUse(name=name, line=ln.line, arguments=list(args), depth=depth,
                           in_macro=in_macro)
            self.uses.append(use)
            body = self._body_for(use, ln, args, depth, in_macro)
            out.extend(body)
        return out

    def _body_for(self, use: MacroUse, ln: LogicalLine, args: List[str],
                  depth: int, in_macro: Optional[str]) -> List[LogicalLine]:
        name = use.name
        if depth >= self.max_depth:
            self.flags.append(
                "macro {0} at line {1}: nesting deeper than {2} levels - not expanded, so "
                "anything it declares is not in the model".format(
                    name, ln.line, self.max_depth))
            return []
        if name in self._expanding:
            self.flags.append(
                "macro {0} at line {1}: recursive invocation - not expanded".format(
                    name, ln.line))
            return []

        text = self._resolve(name)
        if text is None:
            return []
        use.resolved = True

        member, member_flags = logical_lines(text, margin=self.margin)
        self.flags.extend(f for f in member_flags if f not in self.flags)
        names, positional, first = parse_macro_header(member)
        values, leftover = bind_arguments(names, positional, args)
        if leftover:
            self.flags.append(
                "macro {0} at line {1}: operand(s) {2} match no declared parameter "
                "({3}) - they were not substituted".format(
                    name, ln.line, ", ".join(leftover),
                    ", ".join(names) if names else "the member declares none"))

        body: List[LogicalLine] = []
        for src in member[first:]:
            if src.is_data:
                body.append(LogicalLine(text=src.text, line=src.line, raw=src.raw,
                                        origin=name, depth=depth + 1))
                continue
            sub, unresolved = substitute(src.text, values)
            for u in unresolved:
                msg = ("macro {0} at line {1}: parameter &{2} was not supplied - left "
                       "visible in the expansion, not guessed".format(name, ln.line, u))
                if msg not in self.flags:
                    self.flags.append(msg)
            body.append(LogicalLine(text=sub, line=src.line, raw=src.raw,
                                    origin=name, depth=depth + 1))

        self._expanding.add(name)
        try:
            return self._expand(body, depth + 1, name)
        finally:
            self._expanding.discard(name)

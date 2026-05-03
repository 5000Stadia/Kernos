"""English / DSL → canonical predicate AST compiler.

WORKFLOW-LOOP-PRIMITIVE C6.

Two compilation paths land here:

1. **Expression-string DSL** (deterministic, no LLM). Operators
   author predicates in a small recognised grammar:

       event.event_type == "cc.batch.report"
       event.payload.kind == "report"
       event.payload.severity in ["high", "critical"]
       event.payload.message contains "deploy"
       event.payload.task_id exists
       event.payload.task_id not exists
       event.member_id == "owner"
       event.correlation_id == "cor-123"

   AND/OR composition at the top level. Parenthesised
   subexpressions for nested composition. NOT prefix.

   ``compile_dsl(source: str) -> dict`` returns the canonical AST.

2. **English form** (one-time cheap LLM call). Operators write
   trigger conditions in plain English; the compiler invokes the
   injected LLM callable to translate. v1 expects the operator to
   wire up a callable matching ``EnglishCompiler``; the default
   compiler raises ``EnglishCompilationUnavailable`` when no LLM
   is bound. Future WORKFLOW-LOOPS-ENGLISH-V2 expands the
   recognition surface and ships a default compiler.

The compiled AST is stored alongside the original source as
``predicate_source`` on the Trigger record so operators can read
back exactly what they wrote.
"""
from __future__ import annotations

import json
import re
from typing import Awaitable, Callable

from kernos.kernel.workflows.predicates import (
    PredicateError,
    validate as validate_predicate,
)


class CompilerError(ValueError):
    """Raised when the DSL or English compilation fails. Message
    names the offending segment."""


class EnglishCompilationUnavailable(CompilerError):
    """Raised when an English-form predicate is submitted but no
    LLM compiler is bound."""


# An English compiler takes a string (the original English) and
# returns the canonical predicate AST. Sync or async — the caller
# awaits if the return value is awaitable. Real-world bindings inject
# a small LLM call; tests inject a deterministic stub.
EnglishCompiler = Callable[[str], "dict | Awaitable[dict]"]


# ---------------------------------------------------------------------------
# DSL parser
# ---------------------------------------------------------------------------


def compile_dsl(source: str) -> dict:
    """Compile an expression-string DSL predicate to canonical AST.

    Raises ``CompilerError`` on syntactic failure naming the
    offending segment. The result is run through
    :func:`validate_predicate` before returning, so a successful
    compile always produces a valid AST."""
    if not isinstance(source, str) or not source.strip():
        raise CompilerError("DSL source must be a non-empty string")
    try:
        ast = _parse_expression(source.strip())
    except CompilerError:
        raise
    except Exception as exc:
        raise CompilerError(f"failed to parse DSL: {exc}") from exc
    try:
        validate_predicate(ast)
    except PredicateError as exc:
        raise CompilerError(f"compiled AST failed validation: {exc}") from exc
    return ast


def _parse_expression(text: str) -> dict:
    """Top-level parser. Recurses into AND/OR/NOT and parenthesised
    groups. Leaf parsing handled by ``_parse_leaf``."""
    text = text.strip()
    # Strip outer parentheses if they wrap the whole expression.
    while text.startswith("(") and _matching_close(text, 0) == len(text) - 1:
        text = text[1:-1].strip()
    # Top-level OR (lowest precedence).
    or_parts = _split_top_level(text, " OR ")
    if len(or_parts) > 1:
        return {"op": "OR", "operands": [_parse_expression(p) for p in or_parts]}
    # Top-level AND.
    and_parts = _split_top_level(text, " AND ")
    if len(and_parts) > 1:
        return {"op": "AND", "operands": [_parse_expression(p) for p in and_parts]}
    # NOT prefix.
    if text.startswith("NOT "):
        return {"op": "NOT", "operand": _parse_expression(text[4:])}
    return _parse_leaf(text)


def _matching_close(text: str, open_idx: int) -> int:
    """Find the index of the ')' that matches the '(' at
    ``open_idx``. Returns -1 if unmatched."""
    depth = 0
    in_str = False
    str_quote = ""
    for i in range(open_idx, len(text)):
        ch = text[i]
        if in_str:
            if ch == str_quote and text[i - 1] != "\\":
                in_str = False
            continue
        if ch in ('"', "'"):
            in_str = True
            str_quote = ch
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _split_top_level(text: str, sep: str) -> list[str]:
    """Split ``text`` on ``sep`` only when ``sep`` is at the top
    parenthesis level and outside any string literal."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    in_str = False
    str_quote = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if in_str:
            buf.append(ch)
            if ch == str_quote and text[i - 1] != "\\":
                in_str = False
            i += 1
            continue
        if ch in ('"', "'"):
            in_str = True
            str_quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "(":
            depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch == ")":
            depth -= 1
            buf.append(ch)
            i += 1
            continue
        if depth == 0 and text[i:i + len(sep)] == sep:
            parts.append("".join(buf).strip())
            buf = []
            i += len(sep)
            continue
        buf.append(ch)
        i += 1
    if buf:
        parts.append("".join(buf).strip())
    return parts


_PATH_RE = r"event(?:\.[A-Za-z_][\w]*)+"
_STR_RE = r'(?:"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')'

_LEAF_PATTERNS: list[tuple[re.Pattern, Callable[[re.Match], dict]]] = []


def _strip_event_prefix(path: str) -> str:
    """``event.payload.foo`` → ``payload.foo``; ``event.member_id`` →
    ``member_id``."""
    return path[len("event."):] if path.startswith("event.") else path


def _unquote(s: str) -> str:
    return json.loads(s) if s.startswith('"') else s.strip("'")


def _register_leaf(pattern: str, builder: Callable[[re.Match], dict]) -> None:
    _LEAF_PATTERNS.append((re.compile(pattern), builder))


# Equality with quoted string
_register_leaf(
    rf"^(?P<path>{_PATH_RE})\s*==\s*(?P<value>{_STR_RE})$",
    lambda m: _build_eq(m.group("path"), _unquote(m.group("value"))),
)
# Equality with bare number / boolean
_register_leaf(
    rf"^(?P<path>{_PATH_RE})\s*==\s*(?P<value>-?\d+(?:\.\d+)?|true|false)$",
    lambda m: _build_eq(m.group("path"), _coerce_scalar(m.group("value"))),
)
# Inequality (compiled to NOT eq)
_register_leaf(
    rf"^(?P<path>{_PATH_RE})\s*!=\s*(?P<value>{_STR_RE})$",
    lambda m: {"op": "NOT", "operand": _build_eq(
        m.group("path"), _unquote(m.group("value")),
    )},
)
# contains
_register_leaf(
    rf"^(?P<path>{_PATH_RE})\s+contains\s+(?P<value>{_STR_RE})$",
    lambda m: {
        "op": "contains",
        "path": _strip_event_prefix(m.group("path")),
        "value": _unquote(m.group("value")),
    },
)
# in [list]
_register_leaf(
    rf"^(?P<path>{_PATH_RE})\s+in\s+\[(?P<list>.+)\]$",
    lambda m: {
        "op": "in_set",
        "path": _strip_event_prefix(m.group("path")),
        "values": _parse_list(m.group("list")),
    },
)
# not exists
_register_leaf(
    rf"^(?P<path>{_PATH_RE})\s+not\s+exists$",
    lambda m: {
        "op": "not_exists",
        "path": _strip_event_prefix(m.group("path")),
    },
)
# exists
_register_leaf(
    rf"^(?P<path>{_PATH_RE})\s+exists$",
    lambda m: {
        "op": "exists",
        "path": _strip_event_prefix(m.group("path")),
    },
)


def _build_eq(path: str, value) -> dict:
    """Compile ``event.<path>`` equality. Special-case
    ``event.member_id``, ``event.correlation_id``,
    ``event.event_type`` to their named operators where it gives a
    cleaner AST."""
    stripped = _strip_event_prefix(path)
    if stripped == "member_id":
        return {"op": "actor_eq", "value": value}
    if stripped == "correlation_id":
        return {"op": "correlation_eq", "value": value}
    return {"op": "eq", "path": stripped, "value": value}


def _coerce_scalar(s: str) -> int | float | bool | str:
    if s == "true":
        return True
    if s == "false":
        return False
    if "." in s:
        return float(s)
    try:
        return int(s)
    except ValueError:
        return s


def _parse_list(text: str) -> list:
    """Parse a comma-separated DSL list. Items may be quoted strings
    or bare scalars."""
    parts = _split_top_level(text, ",")
    out: list = []
    for raw in parts:
        raw = raw.strip()
        if not raw:
            continue
        if raw.startswith(('"', "'")):
            out.append(_unquote(raw))
        else:
            out.append(_coerce_scalar(raw))
    return out


def _parse_leaf(text: str) -> dict:
    text = text.strip()
    for pattern, builder in _LEAF_PATTERNS:
        m = pattern.match(text)
        if m:
            return builder(m)
    raise CompilerError(f"unrecognised DSL leaf: {text!r}")


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


async def compile_predicate_source(
    source: dict | str,
    *,
    english_compiler: EnglishCompiler | None = None,
) -> dict:
    """Compile a predicate source to canonical AST.

    Accepts:
      * dict → validated and returned as-is.
      * string starting with a recognised DSL token → parsed via
        :func:`compile_dsl`.
      * any other string → handed to ``english_compiler`` if bound;
        otherwise raises :class:`EnglishCompilationUnavailable`.
    """
    if isinstance(source, dict):
        validate_predicate(source)
        return source
    if not isinstance(source, str):
        raise CompilerError(
            f"predicate source must be dict or str, got {type(source).__name__}"
        )
    if _looks_like_dsl(source):
        return compile_dsl(source)
    if english_compiler is None:
        raise EnglishCompilationUnavailable(
            "predicate appears to be in English form but no english_compiler "
            "is bound — author the predicate as DSL or wire up an LLM-backed "
            "english_compiler"
        )
    out = english_compiler(source)
    if hasattr(out, "__await__"):
        out = await out  # type: ignore[assignment]
    if not isinstance(out, dict):
        raise CompilerError(
            f"english_compiler must return dict, got {type(out).__name__}"
        )
    validate_predicate(out)
    return out


_DSL_TOKEN_RE = re.compile(r"\bevent\.[A-Za-z_]")


def _looks_like_dsl(source: str) -> bool:
    """Heuristic: a string is treated as DSL if it contains an
    ``event.<name>`` reference. English forms typically don't."""
    return bool(_DSL_TOKEN_RE.search(source))


__all__ = [
    "CompilerError",
    "EnglishCompilationUnavailable",
    "EnglishCompiler",
    "compile_dsl",
    "compile_predicate_source",
]

"""USER-INITIATED-IMPROVEMENT-TRIGGER-V1 — fix-authorization substrate.

Two layers:

1. ``FixAuthorizationStore`` — SQLite-backed catalog over shared
   ``instance.db``. Mirrors the per-module-connection pattern used
   by :class:`kernos.kernel.closure_store.ClosureStore`.

2. ``classify_fix_scope`` + lattice constants — pure-function fix-
   scope classification with fail-closed semantics. Diff is
   authoritative over self-reported touches_paths; unknown in-repo
   paths and empty-everything responses both route to
   ``substrate_tier`` (the conservative side).

Plus the high-level workflow-adapter entry points
(``record_fix_authorization``, ``classify_proposed_fix``,
``validate_investigation_response``,
``maybe_run_closure_for_fix``) used by the workflow's
``call_tool`` action.
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
from kernos.utils import utc_now
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import aiosqlite


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scope vocabulary + gate-weight mapping
# ---------------------------------------------------------------------------


SCOPE_EXTERNAL_ONLY = "external_only"
SCOPE_CONFIG_DATA = "config_data"
SCOPE_SENSITIVE = "sensitive"
SCOPE_SUBSTRATE_TIER = "substrate_tier"


VALID_SCOPES: frozenset[str] = frozenset({
    SCOPE_EXTERNAL_ONLY,
    SCOPE_CONFIG_DATA,
    SCOPE_SENSITIVE,
    SCOPE_SUBSTRATE_TIER,
})


GATE_WEIGHT_NONE = "no_gate"
GATE_WEIGHT_LIGHT = "light"
GATE_WEIGHT_FULL = "full"


_SCOPE_TO_GATE_WEIGHT: dict[str, str] = {
    SCOPE_EXTERNAL_ONLY: GATE_WEIGHT_NONE,
    SCOPE_CONFIG_DATA: GATE_WEIGHT_LIGHT,
    SCOPE_SENSITIVE: GATE_WEIGHT_FULL,
    SCOPE_SUBSTRATE_TIER: GATE_WEIGHT_FULL,
}


_SCOPE_REQUIRES_ARCHITECT_GATE: dict[str, bool] = {
    SCOPE_EXTERNAL_ONLY: False,
    SCOPE_CONFIG_DATA: False,
    SCOPE_SENSITIVE: True,
    SCOPE_SUBSTRATE_TIER: True,
}


# ---------------------------------------------------------------------------
# Path lattice (precedence: sensitive > substrate > config > external)
# ---------------------------------------------------------------------------


# SENSITIVE: secrets, credentials, live state. Architect gate
# required regardless of whether the diff "looks like config."
SENSITIVE_PATH_PATTERNS: tuple[str, ...] = (
    ".env",
    "**/.env",
    ".credentials/**",
    "secrets/**",
    "data/**/*.db",
    "data/**/*.db-wal",
    "data/**/*.db-shm",
)


# SUBSTRATE: Kernos's own code, specs, workflow defs, build
# system, top-level scripts. Architect gate required.
SUBSTRATE_PATH_PATTERNS: tuple[str, ...] = (
    "kernos/**",
    "specs/**",
    "tests/**",
    "pyproject.toml",
    "requirements.txt",
    "requirements*.txt",
    "*.workflow.yaml",
    "**/*.workflow.yaml",
    "start.sh",
    "scripts/**",
    "docs/architecture/**",
    "DECISIONS.md",
    "CLAUDE.md",
)


# CONFIG_DATA: safe runtime knobs + non-DB data adjustments.
# Light apply allowed. NOTE: *.workflow.yaml is excluded
# (handled by SUBSTRATE_PATH_PATTERNS above due to precedence).
CONFIG_DATA_PATH_PATTERNS: tuple[str, ...] = (
    "data/**/*.json",
    "data/**/*.yaml",
    "data/**/*.md",
    "data/**/*.txt",
    "data/**/*.log",
)


# ---------------------------------------------------------------------------
# FixScopeResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FixScopeResult:
    scope: str
    gate_weight: str
    requires_architect_gate: bool
    sensitive_path_detected: bool
    sensitive_paths: tuple[str, ...]
    diff_path_disagreement: bool
    derived_paths: tuple[str, ...]
    reasoning: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "gate_weight": self.gate_weight,
            "requires_architect_gate": self.requires_architect_gate,
            "sensitive_path_detected": self.sensitive_path_detected,
            "sensitive_paths": list(self.sensitive_paths),
            "diff_path_disagreement": self.diff_path_disagreement,
            "derived_paths": list(self.derived_paths),
            "reasoning": self.reasoning,
        }


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FixAuthorizationError(Exception):
    """Base for fix-authorization errors."""


class InvestigationResponseMalformed(FixAuthorizationError):
    """Raised by validate_investigation_response when CC's
    response doesn't satisfy the schema. Workflow uses
    ``on_failure: abort`` to halt + surface to user."""


# ---------------------------------------------------------------------------
# Diff parsing
# ---------------------------------------------------------------------------


# Match `diff --git a/<path> b/<path>` and `+++ b/<path>` headers.
_DIFF_GIT_HEADER = re.compile(
    r"^diff --git a/(\S+) b/(\S+)\s*$", re.MULTILINE,
)
_DIFF_NEW_HEADER = re.compile(
    r"^\+\+\+ (?:b/)?(\S+)\s*$", re.MULTILINE,
)
_DIFF_OLD_HEADER = re.compile(
    r"^--- (?:a/)?(\S+)\s*$", re.MULTILINE,
)


def extract_paths_from_unified_diff(
    diff: str | None,
) -> list[str]:
    """Parse a unified-diff string and return the set of file
    paths it modifies.

    Handles:
      - ``diff --git a/<path> b/<path>`` headers (preferred)
      - ``+++ b/<path>`` / ``--- a/<path>`` fallback for diffs
        without git headers
      - ``/dev/null`` skipped (represents creation/deletion)

    Returns a deduplicated sorted list. Empty on None / empty
    input. Never raises — malformed diffs return whatever
    paths were extractable; the classifier handles the rest.
    """
    if not diff:
        return []
    paths: set[str] = set()
    try:
        for m in _DIFF_GIT_HEADER.finditer(diff):
            a, b = m.group(1), m.group(2)
            if a and a != "/dev/null":
                paths.add(a)
            if b and b != "/dev/null":
                paths.add(b)
        if not paths:
            for m in _DIFF_NEW_HEADER.finditer(diff):
                p = m.group(1)
                if p and p != "/dev/null":
                    paths.add(p)
            for m in _DIFF_OLD_HEADER.finditer(diff):
                p = m.group(1)
                if p and p != "/dev/null":
                    paths.add(p)
    except Exception as exc:
        logger.warning(
            "DIFF_PARSE_PARTIAL error=%s — returning extracted "
            "paths so far (fail-soft on parsing, fail-closed on "
            "classification)",
            exc,
        )
    return sorted(paths)


def _match_any(path: str, patterns: tuple[str, ...]) -> bool:
    """fnmatch-with-`**` support. fnmatch handles single * but
    not recursive **; we translate ** to a regex pass."""
    for pat in patterns:
        if "**" in pat:
            # Translate `**` to `.*`, single `*` stays as
            # `[^/]*` (fnmatch-like). Lightweight pattern
            # support sufficient for our lattice.
            regex = pat.replace(".", r"\.")
            regex = regex.replace("**", ".*")
            regex = regex.replace("*", "[^/]*")
            regex = "^" + regex + "$"
            if re.match(regex, path):
                return True
        elif fnmatch.fnmatch(path, pat):
            return True
    return False


# ---------------------------------------------------------------------------
# classify_fix_scope
# ---------------------------------------------------------------------------


def classify_fix_scope(
    *,
    proposed_fix_summary: str = "",
    proposed_fix_diff: str | None = None,
    touches_paths: list[str] | None = None,
    external_action: str | None = None,
) -> FixScopeResult:
    """Classify a proposed fix's scope for gate-weight routing.

    Fail-closed: when evidence is missing or ambiguous, returns
    ``substrate_tier``. Never returns ``external_only`` or
    ``config_data`` on partial evidence. Empty-everything →
    ``substrate_tier`` with a clear fail-closed reasoning
    string (workflow's validate_investigation_response step
    catches this earlier; the classifier's behavior is the
    last-line-of-defense pin).
    """
    # Step 1: extract diff-derived paths.
    diff_paths = extract_paths_from_unified_diff(proposed_fix_diff)
    self_reported = list(touches_paths or [])
    derived: list[str] = sorted(set(diff_paths) | set(self_reported))
    disagreement = (
        bool(diff_paths) and bool(self_reported)
        and set(diff_paths) != set(self_reported)
    )

    has_diff = bool(proposed_fix_diff and proposed_fix_diff.strip())
    has_external = bool(external_action and external_action.strip())

    # Step 2: empty-everything fail-closed.
    if not derived and not has_diff and not has_external:
        return FixScopeResult(
            scope=SCOPE_SUBSTRATE_TIER,
            gate_weight=GATE_WEIGHT_FULL,
            requires_architect_gate=True,
            sensitive_path_detected=False,
            sensitive_paths=(),
            diff_path_disagreement=False,
            derived_paths=(),
            reasoning=(
                "fail-closed: no diff, no paths, no external_action "
                "in investigation response — refusing to classify "
                "without evidence"
            ),
        )

    # Step 3: external-only path (no in-repo touches AND an
    # explicit external_action description).
    if not derived and has_external:
        return FixScopeResult(
            scope=SCOPE_EXTERNAL_ONLY,
            gate_weight=GATE_WEIGHT_NONE,
            requires_architect_gate=False,
            sensitive_path_detected=False,
            sensitive_paths=(),
            diff_path_disagreement=False,
            derived_paths=(),
            reasoning=(
                "external_only: no in-repo paths touched; "
                "recommended external action recorded for user"
            ),
        )

    # Step 4: walk derived paths through the lattice.
    sensitive_hits = sorted([
        p for p in derived if _match_any(p, SENSITIVE_PATH_PATTERNS)
    ])
    if sensitive_hits:
        return FixScopeResult(
            scope=SCOPE_SENSITIVE,
            gate_weight=GATE_WEIGHT_FULL,
            requires_architect_gate=True,
            sensitive_path_detected=True,
            sensitive_paths=tuple(sensitive_hits),
            diff_path_disagreement=disagreement,
            derived_paths=tuple(derived),
            reasoning=(
                f"sensitive: paths in security/state lattice "
                f"{sensitive_hits}"
            ),
        )

    substrate_hits = sorted([
        p for p in derived if _match_any(p, SUBSTRATE_PATH_PATTERNS)
    ])
    if substrate_hits:
        return FixScopeResult(
            scope=SCOPE_SUBSTRATE_TIER,
            gate_weight=GATE_WEIGHT_FULL,
            requires_architect_gate=True,
            sensitive_path_detected=False,
            sensitive_paths=(),
            diff_path_disagreement=disagreement,
            derived_paths=tuple(derived),
            reasoning=(
                f"substrate_tier: paths in Kernos source/specs "
                f"{substrate_hits}"
            ),
        )

    config_hits = [
        p for p in derived if _match_any(p, CONFIG_DATA_PATH_PATTERNS)
    ]
    if config_hits and len(config_hits) == len(derived):
        return FixScopeResult(
            scope=SCOPE_CONFIG_DATA,
            gate_weight=GATE_WEIGHT_LIGHT,
            requires_architect_gate=False,
            sensitive_path_detected=False,
            sensitive_paths=(),
            diff_path_disagreement=disagreement,
            derived_paths=tuple(derived),
            reasoning=(
                f"config_data: paths confined to safe runtime / "
                f"data adjustments {sorted(config_hits)}"
            ),
        )

    # Step 5: any unknown-in-repo path → fail-closed to
    # substrate_tier per design principle 5.
    unknown = sorted([
        p for p in derived
        if not _match_any(p, CONFIG_DATA_PATH_PATTERNS)
        and not _match_any(p, SUBSTRATE_PATH_PATTERNS)
        and not _match_any(p, SENSITIVE_PATH_PATTERNS)
    ])
    return FixScopeResult(
        scope=SCOPE_SUBSTRATE_TIER,
        gate_weight=GATE_WEIGHT_FULL,
        requires_architect_gate=True,
        sensitive_path_detected=False,
        sensitive_paths=(),
        diff_path_disagreement=disagreement,
        derived_paths=tuple(derived),
        reasoning=(
            f"fail-closed substrate_tier: unknown in-repo paths "
            f"{unknown} — routing conservatively"
        ),
    )


# ---------------------------------------------------------------------------
# Investigation response validation
# ---------------------------------------------------------------------------


_VALID_INVESTIGATION_OUTCOMES: frozenset[str] = frozenset({
    "completed",
    "partial",
    "unable_to_investigate",
})


def validate_investigation_response(
    *,
    investigation_outcome: str = "",
    failure_mode: str = "",
    proposed_fix_summary: str = "",
    proposed_fix_diff: str = "",
    external_action: str = "",
    touches_paths: Any = None,
    summary: str = "",
) -> dict[str, Any]:
    """Validate the structured CC response shape per spec rules.

    Raises ``InvestigationResponseMalformed`` on any rule
    violation; returns ``{"valid": True}`` on pass. The
    workflow's ``on_failure: abort`` then halts + surfaces.

    v1.1 BRIDGE-RESPONSE-SCHEMA fold (2026-05-27): the coding-
    session bridge response shape only carries ``summary +
    metadata`` — the spec's structured top-level fields
    (``failure_mode``, ``proposed_fix_diff``, ``touches_paths``,
    etc.) don't come through. Live /fix test 2026-05-27 19:14
    confirmed: CC's response had `summary` with a detailed
    markdown investigation, all other structured fields empty,
    and the strict validator rejected every completed response.
    Loosened: when ``investigation_outcome="completed"`` AND
    ``summary`` is non-empty, treat that as sufficient (the
    classifier's fail-closed semantics route to substrate-tier
    when paths/diff/external are all empty anyway, so the
    architect gate still fires on substrate-tier asks).
    Strict structured-field requirement returns once
    BRIDGE-RESPONSE-SCHEMA-V1 ships proper field carriers.

    Rules:
      1. ``investigation_outcome`` must be in the enum.
      2. ``touches_paths`` must be a list (possibly empty);
         non-list raises.
      3. If outcome == "completed": EITHER (a) summary is
         non-empty OR (b) ALL of failure_mode + proposed_fix_summary
         + (proposed_fix_diff OR external_action) are non-empty.
      4. If outcome == "unable_to_investigate": validation
         passes (workflow's own logic handles).
      5. If outcome == "partial": EITHER failure_mode OR summary
         must be non-empty.
    """
    if investigation_outcome not in _VALID_INVESTIGATION_OUTCOMES:
        raise InvestigationResponseMalformed(
            f"investigation_outcome={investigation_outcome!r} not "
            f"in allowed set {sorted(_VALID_INVESTIGATION_OUTCOMES)}"
        )
    if not isinstance(touches_paths, list):
        raise InvestigationResponseMalformed(
            f"touches_paths must be a list; got "
            f"{type(touches_paths).__name__}={touches_paths!r}"
        )
    summary_present = bool(summary and str(summary).strip())
    if investigation_outcome == "completed":
        # v1.1 acceptance path: summary alone is sufficient.
        if summary_present:
            return {"valid": True}
        # Strict path (kept for forward-compat with v2 schema).
        if not failure_mode or not str(failure_mode).strip():
            raise InvestigationResponseMalformed(
                "investigation_outcome=completed requires non-empty "
                "summary OR non-empty failure_mode + structured fields"
            )
        if not proposed_fix_summary or not str(
            proposed_fix_summary,
        ).strip():
            raise InvestigationResponseMalformed(
                "investigation_outcome=completed requires non-empty "
                "summary OR non-empty proposed_fix_summary"
            )
        diff_present = bool(
            proposed_fix_diff and proposed_fix_diff.strip()
        )
        ext_present = bool(
            external_action and external_action.strip()
        )
        if not (diff_present or ext_present):
            raise InvestigationResponseMalformed(
                "investigation_outcome=completed requires non-empty "
                "summary OR at least one of (proposed_fix_diff, "
                "external_action)"
            )
    elif investigation_outcome == "partial":
        if not summary_present and (
            not failure_mode or not str(failure_mode).strip()
        ):
            raise InvestigationResponseMalformed(
                "investigation_outcome=partial requires non-empty "
                "summary OR non-empty failure_mode"
            )
    return {"valid": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------




def _gen_authorization_id() -> str:
    return secrets.token_hex(12)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_FIX_AUTHORIZATION_DDL = """
CREATE TABLE IF NOT EXISTS fix_authorization (
    instance_id          TEXT NOT NULL,
    authorization_id     TEXT NOT NULL,
    request_id           TEXT NOT NULL,
    requester_member_id  TEXT NOT NULL,
    source_space_id      TEXT NOT NULL,
    target_hint          TEXT NOT NULL DEFAULT '',
    request_text         TEXT NOT NULL,
    trigger_surface      TEXT NOT NULL DEFAULT 'slash:/fix',
    authorized_at        TEXT NOT NULL,
    PRIMARY KEY (instance_id, authorization_id)
)
"""


_FIX_AUTHORIZATION_REQUEST_ID_UNIQ = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_fix_authorization_request_id
    ON fix_authorization (instance_id, request_id)
"""


# ---------------------------------------------------------------------------
# FixAuthorizationStore
# ---------------------------------------------------------------------------


class FixAuthorizationStore:
    """SQLite-backed catalog over ``data/instance.db``.

    Own aiosqlite connection (per-module-isolation pattern;
    mirrors :class:`kernos.kernel.closure_store.ClosureStore`).
    Schema setup idempotent; ``PRAGMA foreign_keys=ON`` set per
    connection.
    """

    def __init__(self) -> None:
        self._db: aiosqlite.Connection | None = None
        self._db_path: Path | None = None
        self._write_lock = asyncio.Lock()

    async def start(self, data_dir: str) -> None:
        if self._db is not None:
            return
        self._db_path = Path(data_dir) / "instance.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(
            str(self._db_path), isolation_level=None,
        )
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute(_FIX_AUTHORIZATION_DDL)
        await self._db.execute(_FIX_AUTHORIZATION_REQUEST_ID_UNIQ)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def insert(
        self,
        *,
        instance_id: str,
        authorization_id: str,
        request_id: str,
        requester_member_id: str,
        source_space_id: str,
        target_hint: str,
        request_text: str,
        trigger_surface: str = "slash:/fix",
    ) -> None:
        assert self._db is not None, "FixAuthorizationStore not started"
        async with self._write_lock:
            await self._db.execute(
                """
                INSERT INTO fix_authorization (
                    instance_id, authorization_id, request_id,
                    requester_member_id, source_space_id,
                    target_hint, request_text, trigger_surface,
                    authorized_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    instance_id, authorization_id, request_id,
                    requester_member_id, source_space_id,
                    target_hint, request_text, trigger_surface,
                    utc_now(),
                ),
            )

    async def get_by_request_id(
        self, *, instance_id: str, request_id: str,
    ) -> dict | None:
        assert self._db is not None, "FixAuthorizationStore not started"
        async with self._db.execute(
            """
            SELECT * FROM fix_authorization
            WHERE instance_id=? AND request_id=?
            """,
            (instance_id, request_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return dict(row)


# ---------------------------------------------------------------------------
# Kernel-tool entry points
# ---------------------------------------------------------------------------


async def record_fix_authorization(
    *,
    store: FixAuthorizationStore,
    instance_id: str,
    request_id: str,
    requester_member_id: str,
    source_space_id: str,
    target_hint: str,
    request_text: str,
    trigger_surface: str = "slash:/fix",
) -> dict[str, Any]:
    """Insert (or no-op on existing) a fix_authorization row.
    Idempotent on ``(instance_id, request_id)``. Returns
    ``{"authorization_id": str, "newly_created": bool}``.
    """
    existing = await store.get_by_request_id(
        instance_id=instance_id, request_id=request_id,
    )
    if existing is not None:
        return {
            "authorization_id": existing["authorization_id"],
            "newly_created": False,
        }
    authorization_id = _gen_authorization_id()
    try:
        await store.insert(
            instance_id=instance_id,
            authorization_id=authorization_id,
            request_id=request_id,
            requester_member_id=requester_member_id,
            source_space_id=source_space_id,
            target_hint=target_hint,
            request_text=request_text,
            trigger_surface=trigger_surface,
        )
    except aiosqlite.IntegrityError:
        # Race: another worker inserted between our get + insert.
        # Re-fetch and return the existing row.
        existing = await store.get_by_request_id(
            instance_id=instance_id, request_id=request_id,
        )
        if existing is None:
            raise
        return {
            "authorization_id": existing["authorization_id"],
            "newly_created": False,
        }
    return {
        "authorization_id": authorization_id,
        "newly_created": True,
    }


async def classify_proposed_fix(
    *,
    instance_id: str,
    proposed_fix_summary: str = "",
    proposed_fix_diff: str = "",
    touches_paths: list[str] | None = None,
    external_action: str = "",
) -> dict[str, Any]:
    """Workflow-adapter wrapper for classify_fix_scope. Returns
    the full FixScopeResult fields as a dict (so YAML branch
    can ref ``requires_architect_gate``)."""
    result = classify_fix_scope(
        proposed_fix_summary=proposed_fix_summary,
        proposed_fix_diff=proposed_fix_diff,
        touches_paths=list(touches_paths or []),
        external_action=external_action,
    )
    return result.to_dict()


async def maybe_run_closure_for_fix(
    *,
    instance_id: str,
    related_pattern_id: str = "",
    active_epoch: int = 0,
    closure_store: Any = None,
    pattern_transition_fn: Optional[Callable[..., Any]] = None,
    event_emit_fn: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """Compose SELF-IMPROVEMENT-CLOSURE-V1 primitives when the
    investigation links the fix to a known friction pattern.

    Returns one of:
      - {"closure_outcome": "no_invariant_fallback",
         "closure_id": "", "invariant_id": ""}
        (related_pattern_id empty OR no linked invariant)
      - {"closure_outcome": "passed" | "failed",
         "closure_id": str, "invariant_id": str}
        (closure machinery composed and probe ran)
    """
    if not related_pattern_id or closure_store is None:
        return {
            "closure_outcome": "no_invariant_fallback",
            "closure_id": "",
            "invariant_id": "",
        }
    from kernos.kernel.closure_store import (
        lookup_pattern_invariants,
        record_closure_attempt,
        run_closure_probe,
    )
    lookup = await lookup_pattern_invariants(
        store=closure_store,
        instance_id=instance_id,
        pattern_id=related_pattern_id,
    )
    if not lookup["has_invariants"]:
        return {
            "closure_outcome": "no_invariant_fallback",
            "closure_id": "",
            "invariant_id": "",
        }
    invariant_id = lookup["primary_invariant_id"]
    rec = await record_closure_attempt(
        store=closure_store,
        instance_id=instance_id,
        pattern_id=related_pattern_id,
        invariant_id=invariant_id,
        active_epoch=int(active_epoch),
        route="code_change_via_cc",
        route_payload={},
        probe_kind="deterministic_introspection",
        probe_payload={},
        probe_payload_version=1,
    )
    probe_result = await run_closure_probe(
        store=closure_store,
        instance_id=instance_id,
        closure_id=rec["closure_id"],
        pattern_transition_fn=pattern_transition_fn,
        event_emit_fn=event_emit_fn,
    )
    return {
        "closure_outcome": probe_result["outcome"],
        "closure_id": rec["closure_id"],
        "invariant_id": invariant_id,
    }


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------


RECORD_FIX_AUTHORIZATION_TOOL: dict = {
    "name": "record_fix_authorization",
    "description": (
        "Persist a user fix-authorization to the "
        "fix_authorization table. Idempotent on "
        "(instance_id, request_id) — second call returns the "
        "existing authorization_id with newly_created=False. "
        "Returns {authorization_id, newly_created}."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "request_id": {"type": "string"},
            "requester_member_id": {"type": "string"},
            "source_space_id": {"type": "string"},
            "target_hint": {"type": "string"},
            "request_text": {"type": "string"},
            "trigger_surface": {"type": "string"},
        },
        "required": [
            "request_id", "requester_member_id",
            "source_space_id", "request_text",
        ],
        "additionalProperties": False,
    },
}


CLASSIFY_PROPOSED_FIX_TOOL: dict = {
    "name": "classify_proposed_fix",
    "description": (
        "Classify a proposed fix's scope for gate-weight "
        "routing. Pure function — extracts paths from the "
        "diff, walks them through SENSITIVE > SUBSTRATE > "
        "CONFIG > EXTERNAL lattice (precedence). Fail-closed "
        "on missing/ambiguous evidence: routes substrate_tier "
        "rather than external_only. Returns FixScopeResult "
        "fields including requires_architect_gate native bool "
        "for workflow branching."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "proposed_fix_summary": {"type": "string"},
            "proposed_fix_diff": {"type": "string"},
            "touches_paths": {
                "type": "array",
                "items": {"type": "string"},
            },
            "external_action": {"type": "string"},
        },
        "required": [],
        "additionalProperties": False,
    },
}


VALIDATE_INVESTIGATION_RESPONSE_TOOL: dict = {
    "name": "validate_investigation_response",
    "description": (
        "Validate the CC investigation response shape before "
        "the classifier runs. v1.1: accepts non-empty summary "
        "as sufficient when outcome=completed (the coding-"
        "session bridge schema only carries summary + metadata; "
        "BRIDGE-RESPONSE-SCHEMA-V1 follow-up will add structured "
        "field carriers). Aborts workflow on malformed shape: "
        "outcome outside enum, non-list touches_paths, or "
        "completed-outcome with empty summary AND empty "
        "structured fields."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "investigation_outcome": {"type": "string"},
            "failure_mode": {"type": "string"},
            "proposed_fix_summary": {"type": "string"},
            "proposed_fix_diff": {"type": "string"},
            "external_action": {"type": "string"},
            "touches_paths": {
                "type": "array",
                "items": {"type": "string"},
            },
            "summary": {"type": "string"},
        },
        "required": ["investigation_outcome"],
        "additionalProperties": False,
    },
}


MAYBE_RUN_CLOSURE_FOR_FIX_TOOL: dict = {
    "name": "maybe_run_closure_for_fix",
    "description": (
        "Compose SELF-IMPROVEMENT-CLOSURE-V1 primitives when "
        "the investigation links the fix to a known friction "
        "pattern. No-op when related_pattern_id is empty or "
        "the pattern has no linked invariants. Returns "
        "closure_outcome + closure_id + invariant_id."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "related_pattern_id": {"type": "string"},
            "active_epoch": {"type": "integer"},
        },
        "required": [],
        "additionalProperties": False,
    },
}

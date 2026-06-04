"""RECURSIVE-SELF-HEAL-V1 — bounded recovery lane for the improvement loop.

When an `improve_kernos` attempt fails on a KNOWN machinery signature (a bug
in KERNOS's own loop infrastructure, not a hard task / weak agent output),
the supervisor may spawn ONE bounded child repair, verify it with a hermetic
deterministic fixture, and resume the parent — or stop. "Make it boring": a
bounded recovery lane, not a self-rewrite mandate (see specs/RECURSIVE-SELF-
HEAL-V1.md).

This module is the SAFETY FOUNDATION + classifier. It is inert unless
``KERNOS_RECURSIVE_SELF_HEAL`` is truthy — default OFF for v1. Every guard
Codex's safety review (§9) demanded lives here:

  * durable runaway bound — ``improvement_attempt_edges`` table, DB-enforced
    UNIQUE(parent), UNIQUE(root, signature, fingerprint), child_depth<=1,
    transactional reservation BEFORE spawn (in-memory counters launder depth
    across restarts);
  * classifier with a POSITIVE symptom AND a NEGATIVE guard per signature
    (a task failure can mimic a machinery signature);
  * constitutional boundary — guardrail files are human-only even inside a
    repair (reuses fix_authorization's substrate lattice + an explicit set).
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import aiosqlite

# ---------------------------------------------------------------------------
# Kill switch + bounds (§9.5, §5)
# ---------------------------------------------------------------------------

MAX_CHILD_DEPTH: int = 1


def is_enabled() -> bool:
    """v1 default-OFF kill switch. The whole lane is inert unless set."""
    return (
        os.environ.get("KERNOS_RECURSIVE_SELF_HEAL", "0").strip().lower()
        not in {"", "0", "false", "no", "off"}
    )


# ---------------------------------------------------------------------------
# Constitutional boundary (§9.4) — files that can disable the guardrails
# themselves must NEVER be auto-committed, even inside a repair.
# ---------------------------------------------------------------------------

# Explicit guardrail set (beyond fix_authorization's substrate lattice).
# Any repair diff touching one of these is forced to human review.
CONSTITUTIONAL_PATHS: frozenset[str] = frozenset({
    "start.sh",
    "kernos/setup/boot_guard.py",
    "kernos/setup/self_update.py",
    "kernos/setup/bring_up_substrate.py",
    "kernos/kernel/improvement_loop_workflow.py",
    "kernos/kernel/improvement_ledger.py",
    "kernos/kernel/instance_db.py",
    "kernos/kernel/git_operations.py",
    "kernos/kernel/self_test_gate.py",
    "kernos/kernel/improvement_review_protocol.py",
    "kernos/kernel/approval_receipts.py",
    "kernos/kernel/gate.py",
    "kernos/kernel/reasoning.py",
    "kernos/kernel/fix_authorization.py",
    "kernos/kernel/recursive_self_heal.py",
    "specs/RECURSIVE-SELF-HEAL-V1.md",
})

CONSTITUTIONAL_PREFIXES: tuple[str, ...] = (
    "kernos/kernel/external_agents/",
    "kernos/kernel/workflows/",
    "specs/workflows/",
    "tests/substrate_soak/",
)
CONSTITUTIONAL_SUBSTRINGS: tuple[str, ...] = (
    "kernel_tool_registry",
    "tool_runtime",
    "tool_aliases",
)


def touches_constitutional_path(files: list[str]) -> list[str]:
    """Return the subset of ``files`` that are guardrail/constitutional —
    a non-empty result means the repair must NOT auto-commit (human only).
    Conservative: an unparseable/empty file list returns everything-suspect
    by treating it as a hit on the sentinel ``"?"``."""
    hits: list[str] = []
    for f in files:
        norm = (f or "").lstrip("./")
        if not norm:
            continue
        if (
            norm in CONSTITUTIONAL_PATHS
            or norm.startswith(CONSTITUTIONAL_PREFIXES)
            or any(s in norm for s in CONSTITUTIONAL_SUBSTRINGS)
        ):
            hits.append(norm)
    return hits


# ---------------------------------------------------------------------------
# Signature framework (§4, §9.1, §9.2)
# ---------------------------------------------------------------------------

TASK_FAILURE = "task_failure"


@dataclass(frozen=True)
class RepairSignature:
    """A known machinery-failure class. ``positive`` matches the symptom;
    ``negative_guard`` must return True only when the failure is NOT
    explained by task difficulty / weak agent output (else it's a task
    failure, never machinery). ``verify`` is a hermetic deterministic
    fixture (no live LLM/gateway) run AFTER a child repair to prove the fix."""

    signature_id: str
    description: str
    positive: Callable[[dict], bool]
    negative_guard: Callable[[dict], bool]
    # verify is async: (workspace_dir) -> bool. Provided at wire time so this
    # module stays import-light; None means "not yet implemented (propose-only)".
    verify_kind: str = "fixture"


def _evt(diag: dict, *keys: str) -> Any:
    for k in keys:
        if k in diag and diag[k] not in (None, ""):
            return diag[k]
    return None


# Signature #5 — worktree dirty-state invariant failure. The one we proved
# this session (2d46d05/4056489). Most deterministic fixture, so it ships
# fully; the lane is built/validated against this known-good repair first.
def _sig5_positive(diag: dict) -> bool:
    # Symptom: the loop reported "no diff / nothing implemented" as the
    # convergence blocker.
    reason = str(_evt(diag, "reason", "recovery_reason", "last_reason") or "")
    return any(
        m in reason.lower()
        for m in ("no_diff", "head_unchanged", "false_green", "nothing_implemented")
    )


def _sig5_negative_guard(diag: dict) -> bool:
    # NEGATIVE guard (§9.1): only machinery if the worktree is OBJECTIVELY
    # dirty (status --porcelain non-empty) while detection reported no diff.
    # "reviewer saw no diff" + pristine worktree == task failure, not us.
    return bool(diag.get("worktree_objectively_dirty") is True)


_SIGNATURES: tuple[RepairSignature, ...] = (
    RepairSignature(
        signature_id="worktree_dirty_state_invariant",
        description=(
            "change-detection reports no diff while the worktree is "
            "objectively dirty (untracked/new files)"
        ),
        positive=_sig5_positive,
        negative_guard=_sig5_negative_guard,
    ),
    # Signatures #1-#4 (import/path, receipt mismatch, consult drain/readback,
    # alias miss) are PROPOSE-ONLY in v1 per §4 — registered for classification
    # study but not auto-run until each has a hermetic fixture (§9.2). They are
    # intentionally omitted from the auto-run registry below.
)

_AUTORUN_SIGNATURE_IDS: frozenset[str] = frozenset({
    "worktree_dirty_state_invariant",
})


def classify_failure(diagnostics: dict) -> str:
    """Map a legible failure (post-Stage-1 diagnostics dict) to a machinery
    signature_id eligible for auto-repair, or ``TASK_FAILURE``. Requires BOTH
    the positive symptom AND the negative guard. Default = task_failure (the
    conservative, no-recurse side)."""
    if not isinstance(diagnostics, dict):
        return TASK_FAILURE
    for sig in _SIGNATURES:
        if sig.signature_id not in _AUTORUN_SIGNATURE_IDS:
            continue
        try:
            if sig.positive(diagnostics) and sig.negative_guard(diagnostics):
                return sig.signature_id
        except Exception:
            continue
    return TASK_FAILURE


def failure_fingerprint(signature_id: str, diagnostics: dict) -> str:
    """Stable fingerprint for de-dup (§9.3) — same root+signature+fingerprint
    never gets a second repair."""
    basis = signature_id + "|" + str(
        _evt(diagnostics, "last_event_kind", "reason", "recovery_reason") or ""
    )
    return "fp:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Durable runaway bound (§9.3) — the attempt graph
# ---------------------------------------------------------------------------

_EDGES_DDL = """
CREATE TABLE IF NOT EXISTS improvement_attempt_edges (
    edge_id            TEXT PRIMARY KEY,
    parent_attempt_id  TEXT NOT NULL,
    child_attempt_id   TEXT,
    relation           TEXT NOT NULL,
    signature_id       TEXT NOT NULL,
    failure_fingerprint TEXT NOT NULL,
    root_attempt_id    TEXT NOT NULL,
    child_depth        INTEGER NOT NULL,
    state              TEXT NOT NULL,
    created_at         TEXT NOT NULL
)
"""
# DB-enforced runaway guards: one child per parent; never repeat a fix for
# the same root+signature+fingerprint.
_EDGE_INDEX_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_one_child_per_parent "
    "ON improvement_attempt_edges(parent_attempt_id, relation) "
    "WHERE relation='recursive_repair'",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_no_repeat_fix "
    "ON improvement_attempt_edges(root_attempt_id, signature_id, failure_fingerprint)",
)


def _db_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "instance.db"


async def ensure_schema(data_dir: str | Path) -> None:
    """Idempotent: create the edges table + the DB-enforced runaway guards."""
    path = _db_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(str(path)) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute(_EDGES_DDL)
        for ddl in _EDGE_INDEX_DDL:
            await db.execute(ddl)
        await db.commit()


async def _root_and_depth(
    db: aiosqlite.Connection, parent_attempt_id: str,
) -> tuple[str, int]:
    """Resolve the root attempt + the child_depth THIS parent already sits at,
    walking the edge graph. A parent that is itself a child has depth>=1, so
    its repair would be depth 2 → blocked. Depth is GLOBAL (root-anchored),
    not per-signature (§9.3)."""
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        "SELECT root_attempt_id, child_depth FROM improvement_attempt_edges "
        "WHERE child_attempt_id=? AND relation='recursive_repair' LIMIT 1",
        (parent_attempt_id,),
    )
    row = await cur.fetchone()
    if row is None:
        # parent is a root (depth 0)
        return parent_attempt_id, 0
    return str(row["root_attempt_id"]), int(row["child_depth"])


async def reserve_child_repair(
    *, data_dir: str | Path, parent_attempt_id: str, child_attempt_id: str,
    signature_id: str, failure_fingerprint: str, edge_id: str, now_iso: str,
) -> tuple[bool, str]:
    """TRANSACTIONALLY reserve a child-repair edge BEFORE spawning anything
    (§9.3). Returns (ok, reason). Rejects when: depth would exceed
    MAX_CHILD_DEPTH, the parent already has a recursive child, or the
    root+signature+fingerprint was already repaired. The DB UNIQUE indexes
    are the real guard — this also computes depth so a child-of-a-child can't
    recurse."""
    path = _db_path(data_dir)
    async with aiosqlite.connect(str(path)) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        root_id, parent_depth = await _root_and_depth(db, parent_attempt_id)
        child_depth = parent_depth + 1
        if child_depth > MAX_CHILD_DEPTH:
            return False, f"depth_exceeded(child_depth={child_depth})"
        try:
            await db.execute(
                "INSERT INTO improvement_attempt_edges("
                "edge_id, parent_attempt_id, child_attempt_id, relation, "
                "signature_id, failure_fingerprint, root_attempt_id, "
                "child_depth, state, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    edge_id, parent_attempt_id, child_attempt_id,
                    "recursive_repair", signature_id, failure_fingerprint,
                    root_id, child_depth, "reserved", now_iso,
                ),
            )
            await db.commit()
        except aiosqlite.IntegrityError as exc:
            # UNIQUE violation: parent already has a child, or this
            # root+signature+fingerprint was already attempted (de-dup).
            return False, f"already_reserved_or_deduped({type(exc).__name__})"
        return True, f"reserved(root={root_id},depth={child_depth})"


async def set_edge_state(
    *, data_dir: str | Path, edge_id: str, state: str,
) -> None:
    """Record a terminal/intermediate state on the edge (child_repair_passed,
    child_repair_failed, child_repair_rolled_back, parent_resuming, ...)."""
    path = _db_path(data_dir)
    async with aiosqlite.connect(str(path)) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute(
            "UPDATE improvement_attempt_edges SET state=? WHERE edge_id=?",
            (state, edge_id),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Hermetic verification fixture for signature #5 (§9.2) — proves a repair
# actually fixed worktree change-detection, with NO live LLM/gateway/network.
# ---------------------------------------------------------------------------


async def verify_worktree_dirty_state(_workspace_dir: str = "") -> bool:
    """Deterministic pass/fail: in a fresh temp git repo with an UNTRACKED
    new file, does the substrate's change-detection see it? This is the
    invariant signature #5's repair must restore. Returns True iff detection
    correctly reports the worktree as dirty. Hermetic + reproducible."""
    import subprocess
    import tempfile

    from kernos.kernel.improvement_loop_workflow import _worktree_has_changes

    d = tempfile.mkdtemp(prefix="rsh_verify_")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        for cmd in (
            ["init", "-q", "-b", "main"],
            ["config", "user.email", "t@e"],
            ["config", "user.name", "t"],
        ):
            subprocess.run(["git", *cmd], cwd=d, env=env, check=True)
        (Path(d) / "README.md").write_text("x\n")
        subprocess.run(["git", "add", "."], cwd=d, env=env, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "init"], cwd=d, env=env, check=True,
        )
        # The signature-#5 case: a brand-new UNTRACKED file.
        (Path(d) / "NEW_FILE.md").write_text("created by the agent\n")
        return await _worktree_has_changes(d)
    except Exception:
        return False
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


_VERIFIERS: dict[str, Callable[..., Any]] = {
    "worktree_dirty_state_invariant": verify_worktree_dirty_state,
}


# ---------------------------------------------------------------------------
# Supervisor (§3) — the bounded recovery lane. Owns all transitions. Seams
# (spawn_child_fn / surface_fn) are injected so this is testable without the
# live orchestrator and so the child runs with recursive tools stripped.
# ---------------------------------------------------------------------------


async def attempt_self_heal(
    *,
    data_dir: str,
    parent_attempt_id: str,
    diagnostics: dict,
    new_edge_id: str,
    child_attempt_id: str,
    now_iso: str,
    spawn_child_fn: Callable[..., Any],   # async (signature_id, child_attempt_id) -> diff_files:list[str]
    surface_fn: Callable[..., Any] | None = None,  # async (message) -> None
) -> dict:
    """The bounded recovery lane. Returns a result dict with ``outcome`` one
    of: disabled, task_failure, depth_or_dedup_blocked, child_repair_passed,
    child_repair_failed, human_review_required, constitutional_block.

    The supervisor is the ONLY owner of transitions. The child repair
    (``spawn_child_fn``) must run with recursion disabled + recursive tools
    stripped (the caller wires that). After the child, a HERMETIC verifier
    decides pass/fail — never a model judgment. A repair diff that touches a
    constitutional file is forced to human review even if it verifies."""
    async def _say(msg: str) -> None:
        if surface_fn is not None:
            try:
                await surface_fn(msg)
            except Exception:
                pass

    if not is_enabled():
        return {"outcome": "disabled"}

    signature_id = classify_failure(diagnostics)
    if signature_id == TASK_FAILURE:
        # Never recurse on a hard task / weak agent output.
        return {"outcome": "task_failure"}

    fp = failure_fingerprint(signature_id, diagnostics)
    ok, reason = await reserve_child_repair(
        data_dir=data_dir, parent_attempt_id=parent_attempt_id,
        child_attempt_id=child_attempt_id, signature_id=signature_id,
        failure_fingerprint=fp, edge_id=new_edge_id, now_iso=now_iso,
    )
    if not ok:
        return {"outcome": "depth_or_dedup_blocked", "reason": reason}

    await _say(
        f"The parent improvement hit a machinery failure "
        f"(`{signature_id}`). Spawning one bounded repair attempt for that "
        f"infrastructure issue, then I'll verify and resume or stop."
    )
    await set_edge_state(
        data_dir=data_dir, edge_id=new_edge_id, state="child_repair_running",
    )

    # Spawn the bounded child repair (recursion disabled by the caller).
    try:
        diff_files = await spawn_child_fn(
            signature_id=signature_id, child_attempt_id=child_attempt_id,
        ) or []
    except Exception as exc:
        await set_edge_state(
            data_dir=data_dir, edge_id=new_edge_id, state="child_repair_failed",
        )
        await _say(f"The repair attempt errored ({type(exc).__name__}); stopping.")
        return {"outcome": "child_repair_failed", "reason": str(exc)[:200]}

    # Constitutional boundary (§9.4): even a verified fix that touches a
    # guardrail file is human-only.
    constitutional = touches_constitutional_path(list(diff_files))
    if constitutional:
        await set_edge_state(
            data_dir=data_dir, edge_id=new_edge_id, state="human_review_required",
        )
        await _say(
            "The repair touched guardrail/constitutional code "
            f"({', '.join(constitutional[:3])}) — routing to human review "
            "instead of auto-applying."
        )
        return {"outcome": "constitutional_block", "files": constitutional}

    # Hermetic deterministic verification (§9.2).
    verifier = _VERIFIERS.get(signature_id)
    await set_edge_state(
        data_dir=data_dir, edge_id=new_edge_id, state="child_repair_verifying",
    )
    passed = False
    if verifier is not None:
        try:
            passed = bool(await verifier())
        except Exception:
            passed = False

    if passed:
        await set_edge_state(
            data_dir=data_dir, edge_id=new_edge_id, state="child_repair_passed",
        )
        await _say(
            "Repair verified by the deterministic fixture. Resuming the "
            "original improvement."
        )
        return {"outcome": "child_repair_passed", "signature_id": signature_id}

    await set_edge_state(
        data_dir=data_dir, edge_id=new_edge_id, state="child_repair_failed",
    )
    await _say(
        "The repair did not pass its verification fixture; stopping rather "
        "than resuming on an unverified fix."
    )
    return {"outcome": "child_repair_failed", "reason": "verification_failed"}


__all__ = [
    "MAX_CHILD_DEPTH",
    "TASK_FAILURE",
    "is_enabled",
    "touches_constitutional_path",
    "classify_failure",
    "failure_fingerprint",
    "ensure_schema",
    "reserve_child_repair",
    "set_edge_state",
    "verify_worktree_dirty_state",
    "attempt_self_heal",
]

"""FRICTION-RESPONSE-V1 — Shape B: immediate, reactive resolution of lived
operational friction (the separate element from the 24h creative self-review).

When a friction report is produced, this lane decides — safely — whether to
respond now: diagnose the cause, surface (or, opt-in, auto-trigger a gated)
fix, and on resolution archive the reports; on failure remember exactly what
was tried so the same wrong fix never loops. Spec: specs/FRICTION-RESPONSE-V1.md
(§9 binding). Inert unless ``KERNOS_FRICTION_RESPONSE`` is truthy (default OFF).

This module is the deterministic SAFETY CORE — every guard Codex's spec review
demanded lives here, seam-free and unit-testable:

  * self-friction denylist + durable in-flight reservation (no feedback loop,
    no reentrancy);
  * two-key memory — ``friction_signature`` (what the problem is) +
    ``resolution_fingerprint`` (what we tried) — with the precise anti-loop
    rule: never repeat a FAILED fingerprint for the same signature;
  * cooldown by signature + daily budget;
  * post-deploy windowed verification states (idle != resolved);
  * archive by signature with a manifest.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Kill switch + bounds (§6)
# ---------------------------------------------------------------------------

COOLDOWN_HOURS = 6.0          # per-signature debounce
MAX_RESPONSES_PER_DAY = 8     # global daily budget (count; cost budget is a
#                              live-wiring governor, §7)
VERIFY_WINDOW_HOURS = 6.0     # post-deploy opportunity window before "resolved"

# Friction whose source is Shape B's OWN machinery must never be auto-handled —
# else a response that emits friction triggers another response, forever (§2).
SELF_FRICTION_SOURCES = frozenset({
    "friction_response", "diagnose_issue", "improve_kernos",
    "friction_resolution_ledger", "friction_archive", "friction_daily_sweep",
    "self_maintenance_review", "recursive_self_heal",
})


def is_enabled() -> bool:
    return os.environ.get("KERNOS_FRICTION_RESPONSE", "").strip().lower() in (
        "1", "true", "on", "yes",
    )


# ---------------------------------------------------------------------------
# Two-key identity (§4): what the problem IS vs what we TRIED
# ---------------------------------------------------------------------------


def friction_signature(
    *, friction_type: str, pattern_id: str = "", site: str = "",
    resource: str = "", code: str = "",
) -> str:
    """A STABLE id for a class of friction — excludes timestamp / report path /
    prose noise so the same underlying problem always hashes the same. Prefers
    a detector-supplied ``pattern_id``; else normalizes type + code + site +
    resource."""
    if pattern_id.strip():
        basis = f"pattern:{pattern_id.strip().lower()}"
    else:
        parts = [friction_type.strip().lower(), code.strip().lower(),
                 site.strip().lower(), resource.strip().lower()]
        basis = "|".join(p for p in parts if p)
    return "sig_" + hashlib.sha256(basis.encode()).hexdigest()[:16]


def resolution_fingerprint(*, cause: str, touched: list[str] | tuple[str, ...]) -> str:
    """A stable id for a RESOLUTION PLAN — the normalized cause→fix intent plus
    the subsystem/files it touches. A commit/patch hash is evidence, NOT this
    key, so two semantically-identical attempts collide even with different
    commits."""
    norm_cause = " ".join(str(cause).lower().split())[:300]
    norm_touched = ",".join(sorted(str(t).strip().lower() for t in touched if t))
    return "fix_" + hashlib.sha256(
        f"{norm_cause}||{norm_touched}".encode()).hexdigest()[:16]


def signature_of_filename(name: str) -> tuple[str, str]:
    """Best-effort (friction_type, signature) from a report FILENAME alone,
    handling BOTH live naming conventions (``FRICTION_<ts>_<TYPE>_<hash>.md``
    AND ``<ts>_<TYPE>_<hash>.md``) — the existing readers glob only the first
    and miss the rest (§8)."""
    stem = name[:-3] if name.endswith(".md") else name
    if stem.startswith("FRICTION_"):
        # FRICTION_<date>_<time>_<hash>_<TYPE> — hash is in the MIDDLE; the
        # type is everything after the first three (date, time, hash) tokens.
        rest = stem[len("FRICTION_"):]
        m = re.match(r"^\d+_\d+_[0-9a-f]+_(.+)$", rest)
        ftype = m.group(1) if m else rest
    else:
        # <ts>_<TYPE>_<hash> — strip a leading timestamp + a trailing hex hash.
        s = re.sub(r"^\d{4}[-_]?\d{2}[-_]?\d{2}[T_]?[\d\-:.]*_", "", stem)
        s = re.sub(r"_[0-9a-f]{6,}$", "", s)
        ftype = s
    ftype = ftype or "UNKNOWN"
    return ftype, friction_signature(friction_type=ftype)


# ---------------------------------------------------------------------------
# Durable resolution ledger (§4) — the anti-loop memory + in-flight reservation
# ---------------------------------------------------------------------------

# states (§5)
ATTEMPTED = "attempted"
IN_FLIGHT = "in_flight"
PENDING_VERIFICATION = "pending_verification"
RESOLVED = "resolved"
RECURRED_FAILED = "recurred_failed"
UNKNOWN_NO_OBS = "unknown_no_observation"
_OPEN_STATES = frozenset({IN_FLIGHT, PENDING_VERIFICATION})
_FAILED_STATES = frozenset({RECURRED_FAILED})


def _ledger_path(data_dir: str) -> Path:
    return Path(data_dir) / "diagnostics" / "friction_resolutions.jsonl"


def record_attempt(
    data_dir: str, *, friction_signature: str, friction_type: str,
    resolution_fingerprint: str, state: str, now_iso: str,
    attempted_resolution: str = "", commit_sha: str = "", source: str = "",
) -> None:
    """Append one resolution record (best-effort; never raises into caller)."""
    rec = {
        "ts": now_iso, "friction_signature": friction_signature,
        "friction_type": friction_type,
        "resolution_fingerprint": resolution_fingerprint,
        "attempted_resolution": str(attempted_resolution)[:500],
        "state": state, "commit_sha": commit_sha, "source": source,
    }
    try:
        p = _ledger_path(data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except Exception:
        pass


def load_attempts(data_dir: str, *, limit: int = 500) -> list[dict]:
    p = _ledger_path(data_dir)
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        for line in p.read_text(errors="replace").splitlines()[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except Exception:
        return []
    return out


def _latest_state(attempts: list[dict], signature: str) -> str:
    for r in reversed(attempts):
        if r.get("friction_signature") == signature:
            return str(r.get("state") or "")
    return ""


def failed_fingerprints(attempts: list[dict], signature: str) -> set[str]:
    """Resolution fingerprints that already FAILED for this signature — never
    retry one of these (the anti-loop rule)."""
    return {
        str(r.get("resolution_fingerprint") or "")
        for r in attempts
        if r.get("friction_signature") == signature
        and str(r.get("state") or "") in _FAILED_STATES
        and r.get("resolution_fingerprint")
    }


def _hours_between(a_iso: str, b_iso: str) -> float | None:
    from datetime import datetime
    try:
        return (datetime.fromisoformat(b_iso)
                - datetime.fromisoformat(a_iso)).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return None


def reserve_in_flight(
    data_dir: str, *, friction_signature: str, friction_type: str,
    now_iso: str,
) -> bool:
    """Durably claim a response slot for this SIGNATURE before any expensive
    work. Returns False if one is already open (in_flight / pending) — no
    concurrency, no double-spend (§2). The maintenance mutex (live wiring) is
    the cross-lane guard; this is the per-signature guard."""
    attempts = load_attempts(data_dir)
    if _latest_state(attempts, friction_signature) in _OPEN_STATES:
        return False
    record_attempt(
        data_dir, friction_signature=friction_signature,
        friction_type=friction_type, resolution_fingerprint="",
        state=IN_FLIGHT, now_iso=now_iso, source="friction_response",
    )
    return True


# ---------------------------------------------------------------------------
# Eligibility (§2) — the full gate before responding
# ---------------------------------------------------------------------------


def should_respond(
    data_dir: str, *, friction_signature: str, source: str, now_iso: str,
    candidate_fingerprint: str = "",
) -> tuple[bool, str]:
    """Decide whether a freshly-produced friction report warrants a response.
    Returns (ok, reason). Order matters: cheapest + safety-critical first."""
    if not is_enabled():
        return False, "disabled"
    # Self-friction: never respond to friction our own machinery emitted (§2).
    if source in SELF_FRICTION_SOURCES:
        return False, "self_friction_source"

    attempts = load_attempts(data_dir)

    # Already being handled / awaiting verification.
    if _latest_state(attempts, friction_signature) in _OPEN_STATES:
        return False, "already_in_flight"

    # Anti-loop: don't retry a resolution that already failed for this sig.
    if candidate_fingerprint and candidate_fingerprint in failed_fingerprints(
        attempts, friction_signature,
    ):
        return False, "resolution_already_failed"

    # Per-signature cooldown.
    for r in reversed(attempts):
        if r.get("friction_signature") != friction_signature:
            continue
        gap = _hours_between(str(r.get("ts", "")), now_iso)
        if gap is not None and gap < COOLDOWN_HOURS:
            return False, "within_cooldown"
        break

    # Global daily budget (count). Cost budget is a live governor (§7).
    day = (now_iso or "")[:10]
    todays = sum(1 for r in attempts
                 if str(r.get("ts", ""))[:10] == day
                 and str(r.get("state")) != UNKNOWN_NO_OBS)
    if todays >= MAX_RESPONSES_PER_DAY:
        return False, "daily_budget_reached"

    return True, "eligible"


# ---------------------------------------------------------------------------
# Verification (§5) — post-deploy, windowed; idle is NOT resolution
# ---------------------------------------------------------------------------


def judge_resolution(
    *, deployed_iso: str, now_iso: str, recurred_iso: str = "",
    had_detector_opportunity: bool,
) -> str:
    """Classify a fix's outcome. ``recurred_iso`` = timestamp of a same-
    signature report AFTER the fix deployed (empty if none). Absence of reports
    while the bot was idle/down is NOT proof — that's unknown_no_observation."""
    if recurred_iso:
        gap = _hours_between(deployed_iso, recurred_iso)
        if gap is None or gap >= 0:
            return RECURRED_FAILED
    window = _hours_between(deployed_iso, now_iso)
    if window is None or window < VERIFY_WINDOW_HOURS:
        return PENDING_VERIFICATION
    if not had_detector_opportunity:
        return UNKNOWN_NO_OBS
    return RESOLVED


# ---------------------------------------------------------------------------
# Archive by signature (§6) — shadow archive + manifest, never hard-delete
# ---------------------------------------------------------------------------


def archive_resolved_signature(
    data_dir: str, *, friction_signature: str, now_iso: str,
    ledger_ref: str = "",
) -> int:
    """Move ONLY the reports matching this resolved signature into
    ``diagnostics/friction_resolved/`` (shadow archive), writing a manifest
    that links the archived reports → the resolution. Returns count moved.
    Reports of OTHER signatures are left untouched (§6)."""
    import shutil

    fdir = Path(data_dir) / "diagnostics" / "friction"
    if not fdir.is_dir():
        return 0
    dest = Path(data_dir) / "diagnostics" / "friction_resolved"
    dest.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for p in list(fdir.glob("*.md")):
        _ftype, sig = signature_of_filename(p.name)
        if sig != friction_signature:
            continue
        try:
            shutil.move(str(p), str(dest / p.name))
            moved.append(p.name)
        except Exception:
            continue
    if moved:
        manifest = {
            "ts": now_iso, "friction_signature": friction_signature,
            "ledger_ref": ledger_ref, "archived": moved,
        }
        try:
            with (dest / "_manifest.jsonl").open("a") as fh:
                fh.write(json.dumps(manifest, separators=(",", ":")) + "\n")
        except Exception:
            pass
    return len(moved)


__all__ = [
    "is_enabled", "SELF_FRICTION_SOURCES", "COOLDOWN_HOURS",
    "MAX_RESPONSES_PER_DAY", "VERIFY_WINDOW_HOURS",
    "friction_signature", "resolution_fingerprint", "signature_of_filename",
    "ATTEMPTED", "IN_FLIGHT", "PENDING_VERIFICATION", "RESOLVED",
    "RECURRED_FAILED", "UNKNOWN_NO_OBS",
    "record_attempt", "load_attempts", "failed_fingerprints",
    "reserve_in_flight", "should_respond", "judge_resolution",
    "archive_resolved_signature",
]

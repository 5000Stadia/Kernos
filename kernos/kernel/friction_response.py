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
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

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
    "self",  # content-derived: the report's context implicates our own machinery
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
    _hex = re.compile(r"^[0-9a-f]{6,}$")
    if stem.startswith("FRICTION_"):
        # Two historical orderings exist in the wild, BOTH FRICTION_<date>_
        # <time>_…: the CURRENT writer (friction.py:446) is <TYPE>_<uuid8>
        # (hash trailing); older reports are <hash>_<TYPE> (hash leading).
        toks = stem[len("FRICTION_"):].split("_")
        while toks and toks[0].isdigit():   # drop date + time (both numeric)
            toks.pop(0)
        if toks and _hex.match(toks[-1]):
            toks = toks[:-1]                 # current: <TYPE>_<uuid8>
        elif toks and _hex.match(toks[0]):
            toks = toks[1:]                  # legacy: <hash>_<TYPE>
        ftype = "_".join(toks)
    else:
        # <ts>_<TYPE>_<hash> — strip a leading timestamp + a trailing hex hash.
        s = re.sub(r"^\d{4}[-_]?\d{2}[-_]?\d{2}[T_]?[\d\-:.]*_", "", stem)
        s = re.sub(r"_[0-9a-f]{6,}$", "", s)
        ftype = s
    ftype = ftype or "UNKNOWN"
    return ftype, friction_signature(friction_type=ftype)


_RECOMMENDATION_RE = re.compile(r"^#*\s*Recommendation:\s*(.+)$", re.MULTILINE)


def signature_from_report(name: str, body: str = "") -> tuple[str, str]:
    """A STABLE per-problem (type, signature) using the report BODY, not just
    the coarse filename type (Codex code-review High-4). Folds the report's
    Recommendation (a stable detector field) into the signature, so two
    different problems sharing a generic type don't collapse — while same-cause
    repeats (e.g. one connection leak) still share a signature. Falls back to
    type-only when there's no body."""
    ftype, _ = signature_of_filename(name)
    rec = ""
    if body:
        m = _RECOMMENDATION_RE.search(body)
        if m:
            rec = " ".join(m.group(1).split())[:120]
    return ftype, friction_signature(friction_type=ftype, code=rec)


# Markers of friction emitted BY our own remediation machinery — content-based
# provenance, since the detectors don't (yet) stamp a source field. Conservative
# by intent: if a report's context mentions a remediation lane, treat it as
# self-friction and never auto-respond (Codex code-review High-3).
_SELF_MARKERS = (
    "improve_kernos", "friction_response", "friction-response",
    "self_maintenance", "self-maintenance", "diagnose_issue",
    "recursive_self_heal", "recursive self-heal", "att_",
)


def source_of_report(body: str) -> str:
    """'self' if the report's context implicates our own remediation machinery
    (so the self-friction denylist can actually fire); else 'detector'."""
    low = (body or "").lower()
    return "self" if any(m in low for m in _SELF_MARKERS) else "detector"


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


def _build_record(
    *, friction_signature: str, friction_type: str, resolution_fingerprint: str,
    state: str, now_iso: str, attempted_resolution: str = "", commit_sha: str = "",
    source: str = "", notes: str = "",
) -> dict:
    return {
        "ts": now_iso, "friction_signature": friction_signature,
        "friction_type": friction_type,
        "resolution_fingerprint": resolution_fingerprint,
        "attempted_resolution": str(attempted_resolution)[:500],
        "state": state, "commit_sha": commit_sha, "source": source,
        "notes": str(notes)[:300],
    }


def record_attempt(
    data_dir: str, *, friction_signature: str, friction_type: str,
    resolution_fingerprint: str, state: str, now_iso: str,
    attempted_resolution: str = "", commit_sha: str = "", source: str = "",
    notes: str = "",
) -> bool:
    """Append one resolution record under an exclusive file lock + fsync.
    Returns True on durable success, False on failure (so a caller relying on
    persistence — e.g. the reservation — can refuse rather than proceed on a
    silently-lost write; Codex code-review Med-6)."""
    rec = _build_record(
        friction_signature=friction_signature, friction_type=friction_type,
        resolution_fingerprint=resolution_fingerprint, state=state,
        now_iso=now_iso, attempted_resolution=attempted_resolution,
        commit_sha=commit_sha, source=source, notes=notes,
    )
    try:
        p = _ledger_path(data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            _flock(fh)
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return True
    except Exception:
        return False


def _flock(fh) -> None:
    try:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except Exception:
        pass  # non-POSIX or unsupported FS — single-process bot tolerates it


def load_attempts(data_dir: str, *, limit: int | None = None) -> list[dict]:
    """Read the resolution ledger. ``limit`` tails the most recent N rows;
    None reads ALL — guard checks (anti-loop, cooldown) MUST read all so an old
    failed fix isn't retried once the tail scrolls past it (Codex Med-5)."""
    p = _ledger_path(data_dir)
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        lines = p.read_text(errors="replace").splitlines()
        if limit is not None:
            lines = lines[-limit:]
        for line in lines:
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
    """Durably + ATOMICALLY claim a response slot for this SIGNATURE before any
    expensive work: load-check-append happens under a single exclusive lock, so
    there's no load-then-append race (Codex Med-6). Returns False if one is
    already open (in_flight / pending) OR the write didn't persist."""
    try:
        p = _ledger_path(data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a+") as fh:
            _flock(fh)
            fh.seek(0)
            attempts = []
            for line in fh.read().splitlines():
                line = line.strip()
                if line:
                    try:
                        attempts.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            if _latest_state(attempts, friction_signature) in _OPEN_STATES:
                return False
            rec = _build_record(
                friction_signature=friction_signature,
                friction_type=friction_type, resolution_fingerprint="",
                state=IN_FLIGHT, now_iso=now_iso, source="friction_response",
            )
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return True
    except Exception:
        return False


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

    attempts = load_attempts(data_dir)  # None => full scan (anti-loop must be durable)

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

    # Global daily budget — charge ONCE per response (each response makes
    # exactly one IN_FLIGHT reservation), not per ledger row (Codex Med-7).
    day = (now_iso or "")[:10]
    todays = sum(1 for r in attempts
                 if str(r.get("ts", ""))[:10] == day
                 and str(r.get("state")) == IN_FLIGHT)
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
        try:
            body = p.read_text(errors="replace")
        except Exception:
            body = ""
        _ftype, sig = signature_from_report(p.name, body)  # SAME key as elsewhere
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


# ---------------------------------------------------------------------------
# Durable governance items (SELF-REVIEW-SURFACING-INTEGRITY-V1)
#
# A human-gated finding routed to a one-shot ephemeral whisper is
# indistinguishable from a finding that was never raised. Governance items are
# the persistent queue behind the human gate: opened on a deterministic
# condition, upserted by stable signature, never TTL'd, closed only when the
# condition itself clears, and shadow-archived (never deleted) on closure.
#
# v1 admits ONLY deterministically-verifiable conditions (one producer: the
# self-review coverage gap). Stochastic LLM findings stay whisper-only — a
# queue whose items cannot be reliably closed is the very failure this fixes.
# ---------------------------------------------------------------------------

GOVERNANCE_PREFIX = "GOVERNANCE_"


def governance_filename(signature: str) -> str:
    """Stable, path-safe filename for a governance signature.

    The raw signature (e.g. ``self-review:coverage-gap``) contains a colon and
    must never reach a path. Slug + short hash keeps it readable AND collision-
    free, and is stable across upserts by construction.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", (signature or "").lower()).strip("_")[:48]
    digest = hashlib.sha256((signature or "").encode()).hexdigest()[:12]
    return f"{GOVERNANCE_PREFIX}{slug}_{digest}.md"


def _governance_field(body: str, field: str) -> str:
    """Read a single ``Field: value`` line out of a governance report."""
    want = field.lower() + ":"
    for line in (body or "").splitlines()[:20]:
        s = line.strip()
        if s.lower().startswith(want):
            return s.split(":", 1)[1].strip()
    return ""


def _render_governance_item(
    *, signature: str, title: str, condition: str, payload: list,
    opened_iso: str, last_seen_iso: str,
) -> str:
    lines = [
        f"# GOVERNANCE: {title}",
        "",
        f"Class: {GOVERNANCE_CLASS}",
        f"Signature: {signature}",
        "Human-gated: true",
        f"Opened: {opened_iso}",
        f"Last-seen: {last_seen_iso}",
        "",
        "## Condition",
        condition,
        "",
        "## Payload",
    ]
    lines.extend(f"- {item}" for item in payload)
    lines.append("")
    lines.append(
        "This item is HUMAN-GATED. It is bookkeeping, not authority: no "
        "auto-trigger consumes it and nothing here may be self-applied. It "
        "stays open until the condition above genuinely clears."
    )
    return "\n".join(lines) + "\n"


def upsert_governance_item(
    data_dir: str, *, signature: str, title: str, condition: str,
    payload: list, now_iso: str,
) -> bool:
    """Open or update the governance item for ``signature``. Idempotent.

    Re-detection updates payload + last-seen and PRESERVES the original
    ``Opened:`` stamp — one item per condition, never a duplicate per scan.
    Returns True on a durable write, False otherwise; the caller tracks that
    acknowledgement SEPARATELY from any surfacing state, so a failed write
    stays retryable without re-nagging the whisper.
    """
    try:
        fdir = Path(data_dir) / "diagnostics" / "friction"
        fdir.mkdir(parents=True, exist_ok=True)
        path = fdir / governance_filename(signature)
        opened_iso = now_iso
        if path.is_file():
            prior = _governance_field(path.read_text(errors="replace"), "Opened")
            if prior:
                opened_iso = prior
        body = _render_governance_item(
            signature=signature, title=title, condition=condition,
            payload=list(payload), opened_iso=opened_iso,
            last_seen_iso=now_iso,
        )
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(body)
        tmp.replace(path)  # atomic
        return True
    except Exception:
        logger.warning(
            "GOVERNANCE_ITEM_WRITE_FAILED signature=%s — item remains "
            "eligible for retry on the next scan", signature, exc_info=True,
        )
        return False


def open_governance_items(data_dir: str) -> list[dict]:
    """Enumerate OPEN governance items. No TTL — they persist until resolved.

    This is the recovery path: a missed one-shot whisper must not lose the
    finding. Reading the queue is NOT surfacing it — enumeration never
    re-whispers. Best-effort: [] on any error.
    """
    out: list[dict] = []
    try:
        fdir = Path(data_dir) / "diagnostics" / "friction"
        if not fdir.is_dir():
            return []
        paths = sorted(fdir.glob(f"{GOVERNANCE_PREFIX}*.md"))
    except Exception:
        return []
    for p in paths:
        try:
            body = p.read_text(errors="replace")
        except Exception:
            continue
        if report_class(body) != GOVERNANCE_CLASS:
            continue
        payload = [
            ln[2:].strip() for ln in body.splitlines() if ln.startswith("- ")
        ]
        out.append({
            "signature": _governance_field(body, "Signature"),
            "title": (body.splitlines() or [""])[0].lstrip("# ").strip(),
            "payload": payload,
            "opened_iso": _governance_field(body, "Opened"),
            "last_seen_iso": _governance_field(body, "Last-seen"),
            "human_gated": _governance_field(body, "Human-gated").lower() == "true",
        })
    return out


def quarantined_reports(data_dir: str) -> list[dict]:
    """Friction reports whose declared class is unrecognized.

    These are excluded from Shape B and every auto-trigger (fail-closed), so
    without a durable listing they would vanish from operator view entirely —
    a log warning is not a recovery surface. Best-effort: [] on any error.
    """
    out: list[dict] = []
    try:
        fdir = Path(data_dir) / "diagnostics" / "friction"
        if not fdir.is_dir():
            return []
        paths = sorted(fdir.glob("*.md"))
    except Exception:
        return []
    for p in paths:
        try:
            body = p.read_text(errors="replace")
        except Exception:
            continue
        if report_class(body) != UNKNOWN_CLASS:
            continue
        declared = ""
        for line in body.splitlines()[:12]:
            s = line.strip()
            if s.lower().startswith("class:"):
                declared = s.split(":", 1)[1].strip()
                break
        out.append({"file": p.name, "declared_class": declared,
                    "excerpt": body[:200]})
    return out


def close_governance_item(
    data_dir: str, *, signature: str, now_iso: str, resolving_condition: str,
) -> bool:
    """Close a governance item: shadow-archive it, never hard-delete.

    Reuses the existing ``diagnostics/friction_resolved/`` substrate and its
    ``_manifest.jsonl`` convention rather than inventing a parallel archive.
    The archived filename carries a closure timestamp so a later recurrence of
    the SAME condition reopens cleanly without colliding. Returns True if an
    open item was archived.
    """
    import shutil

    try:
        src = Path(data_dir) / "diagnostics" / "friction" / governance_filename(signature)
        if not src.is_file():
            return False
        body = src.read_text(errors="replace")
        payload = [
            ln[2:].strip() for ln in body.splitlines() if ln.startswith("- ")
        ]
        opened_iso = _governance_field(body, "Opened")
        dest = Path(data_dir) / "diagnostics" / "friction_resolved"
        dest.mkdir(parents=True, exist_ok=True)

        # The closure AUDIT is written before the move. If the manifest cannot
        # be written we must not move the only open copy — otherwise the audit
        # is permanently unrecoverable and the caller has no way to retry.
        manifest = {
            "ts": now_iso,
            "class": GOVERNANCE_CLASS,
            "governance_signature": signature,
            "opened_iso": opened_iso,
            "closed_iso": now_iso,
            "resolving_condition": resolving_condition,
            "final_payload": payload,
        }
        try:
            with (dest / "_manifest.jsonl").open("a") as fh:
                fh.write(json.dumps(manifest, separators=(",", ":")) + "\n")
        except Exception:
            logger.warning(
                "GOVERNANCE_CLOSE_MANIFEST_FAILED signature=%s — leaving the "
                "item OPEN so closure stays retryable", signature, exc_info=True)
            return False

        # Full-resolution, collision-resistant, no-clobber destination. A
        # truncated stamp collapses two closures in the same minute onto one
        # name and shutil.move would silently overwrite the earlier archive,
        # destroying it while the manifest still claimed both.
        stamp = re.sub(r"[^0-9A-Za-z]+", "", now_iso)
        target = dest / f"{src.stem}_closed_{stamp}.md"
        n = 1
        while target.exists():
            target = dest / f"{src.stem}_closed_{stamp}_{n}.md"
            n += 1
        shutil.move(str(src), str(target))
        return True
    except Exception:
        logger.warning(
            "GOVERNANCE_ITEM_CLOSE_FAILED signature=%s", signature,
            exc_info=True,
        )
        return False


# ---------------------------------------------------------------------------
# Open-friction inventory + the response orchestrator (seam-injected)
# ---------------------------------------------------------------------------

RESOLVED_WINDOW_HOURS = 24.0  # quiet this long (with activity) ⇒ resolved


OPPORTUNITY_CLASS = "opportunity"
GOVERNANCE_CLASS = "governance"
UNKNOWN_CLASS = "unknown"
ERROR_CLASS = "error"

#: Classes reactive Shape B must never pick up. `opportunity` is worked by the
#: daily self-review; `governance` is human-gated bookkeeping; `unknown` is a
#: quarantined mis-classification. SELF-REVIEW-SURFACING-INTEGRITY-V1.
SHAPE_B_EXCLUDED_CLASSES = frozenset(
    {OPPORTUNITY_CLASS, GOVERNANCE_CLASS, UNKNOWN_CLASS}
)

_RECOGNIZED_CLASSES = frozenset(
    {OPPORTUNITY_CLASS, GOVERNANCE_CLASS, ERROR_CLASS}
)


def report_class(body: str) -> str:
    """Parse the report's `Class:` front-matter.

    SELF-REVIEW-SURFACING-INTEGRITY-V1 — the classification boundary FAILS
    CLOSED. An explicitly-stated but unrecognized class becomes ``unknown``
    (quarantined) rather than ``error``: ``error`` is not a neutral bucket, it
    enters reactive Shape B and can reach gated automation, so a typo in
    ``Class: governance`` must never *broaden* a human-gated item into an
    automated lane.

    - no ``Class:`` header      → ``error``  (legacy compatibility, unchanged)
    - explicit ``error``        → ``error``
    - exact ``opportunity``     → ``opportunity``
    - exact ``governance``      → ``governance``
    - any other explicit value  → ``unknown`` (excluded from every auto lane)
    """
    for line in (body or "").splitlines()[:12]:
        s = line.strip().lower()
        if s.startswith("class:"):
            declared = s.split(":", 1)[1].strip()
            if declared in _RECOGNIZED_CLASSES:
                return declared
            logger.warning(
                "FRICTION_REPORT_UNKNOWN_CLASS declared=%r — quarantined as "
                "'unknown'; excluded from Shape B and every auto-trigger",
                declared[:80],
            )
            return UNKNOWN_CLASS
    return ERROR_CLASS


def list_open_signatures(data_dir: str, *, max_files: int = 300) -> list[dict]:
    """Group the OPEN friction reports by signature (recurring first). Each:
    {signature, type, count, latest_mtime, sample_body}. Reads both naming
    conventions; archived reports live elsewhere so they're excluded."""
    fdir = Path(data_dir) / "diagnostics" / "friction"
    if not fdir.is_dir():
        return []
    files = sorted(fdir.glob("*.md"),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:max_files]
    groups: dict[str, dict] = {}
    for p in files:
        try:
            body = p.read_text(errors="replace")
        except Exception:
            body = ""
        if report_class(body) in SHAPE_B_EXCLUDED_CLASSES:
            continue  # opportunity → daily self-review; governance → human-gated;
            #           unknown → quarantined. None are reactive Shape B work.
        ftype, sig = signature_from_report(p.name, body)  # body-aware, stable
        src = source_of_report(body)
        g = groups.setdefault(sig, {
            "signature": sig, "type": ftype, "count": 0,
            "latest_mtime": 0.0, "sample_body": "", "source": "detector",
        })
        g["count"] += 1
        if src == "self":
            g["source"] = "self"  # any self-tagged report taints the group
        mt = p.stat().st_mtime
        if mt >= g["latest_mtime"]:
            g["latest_mtime"] = mt
            g["sample_body"] = body[:1500]
    return sorted(groups.values(), key=lambda g: -g["count"])


async def respond_once(
    data_dir: str, *, now_iso: str, diagnose_fn, surface_fn,
) -> dict:
    """Process the single most-pressing eligible open friction signature:
    gate → reserve → diagnose → surface-first → record. Seam-injected
    (``diagnose_fn(sig, ftype, body) -> {cause, touched, proposed_fix}``;
    ``surface_fn(sig, ftype, diag) -> None``) so it's testable without the live
    diagnose/whisper paths. Surface-first v1 — no autonomous code change."""
    if not is_enabled():
        return {"outcome": "disabled"}
    for info in list_open_signatures(data_dir):
        sig, ftype = info["signature"], info["type"]
        ok, reason = should_respond(
            data_dir, friction_signature=sig,
            source=info.get("source", "detector"), now_iso=now_iso,
        )
        if not ok:
            continue
        if not reserve_in_flight(
            data_dir, friction_signature=sig, friction_type=ftype, now_iso=now_iso,
        ):
            continue
        try:
            diag = await diagnose_fn(sig, ftype, info.get("sample_body", "")) or {}
        except Exception as exc:
            # Clear the in-flight claim so it can retry after cooldown.
            record_attempt(
                data_dir, friction_signature=sig, friction_type=ftype,
                resolution_fingerprint="", state=ATTEMPTED, now_iso=now_iso,
                notes=f"diagnose_failed:{str(exc)[:80]}", source="friction_response",
            )
            return {"outcome": "diagnose_error", "signature": sig,
                    "error": str(exc)[:200]}
        fp = resolution_fingerprint(
            cause=str(diag.get("cause", "")), touched=diag.get("touched", []),
        )
        # Anti-loop: never re-surface a plan that already failed for this sig.
        if fp in failed_fingerprints(load_attempts(data_dir), sig):
            record_attempt(
                data_dir, friction_signature=sig, friction_type=ftype,
                resolution_fingerprint=fp, state=ATTEMPTED, now_iso=now_iso,
                notes="skipped: resolution already failed",
                source="friction_response",
            )
            continue
        try:
            await surface_fn(sig, ftype, diag)
        except Exception:
            record_attempt(
                data_dir, friction_signature=sig, friction_type=ftype,
                resolution_fingerprint=fp, state=ATTEMPTED, now_iso=now_iso,
                notes="surface_failed", source="friction_response",
            )
            return {"outcome": "surface_error", "signature": sig}
        record_attempt(
            data_dir, friction_signature=sig, friction_type=ftype,
            resolution_fingerprint=fp, state=PENDING_VERIFICATION, now_iso=now_iso,
            attempted_resolution=str(diag.get("proposed_fix", ""))[:500],
            source="friction_response",
        )
        return {"outcome": "surfaced", "signature": sig, "type": ftype,
                "resolution_fingerprint": fp}
    return {"outcome": "nothing_eligible"}


def verify_and_archive(data_dir: str, *, now_iso: str) -> dict:
    """Close the loop on PENDING_VERIFICATION signatures. A NEW report of the
    signature after we surfaced ⇒ recurred_failed (feeds the anti-loop). Quiet
    for the window WITH real detector opportunity ⇒ resolved ⇒ archive. Quiet
    but no opportunity (bot idle / down) ⇒ unknown — idle is NOT proof of
    resolution (Codex High-2: opportunity is derived from actual post-pending
    friction activity, not from 'the loop happened to run')."""
    from datetime import datetime
    fdir = Path(data_dir) / "diagnostics" / "friction"
    attempts = load_attempts(data_dir)  # full scan
    latest: dict[str, dict] = {}
    for r in attempts:
        latest[str(r.get("friction_signature"))] = r

    # Index open reports once: (signature -> recurred?) and a global "any
    # friction at all after pending" = real detector opportunity.
    report_index: list[tuple[str, float]] = []
    if fdir.is_dir():
        for p in fdir.glob("*.md"):
            try:
                body = p.read_text(errors="replace")
            except Exception:
                body = ""
            if report_class(body) in SHAPE_B_EXCLUDED_CLASSES:
                continue  # not Shape B detector activity (V3 + SRSI-V1)
            _t, s = signature_from_report(p.name, body)
            report_index.append((s, p.stat().st_mtime))

    resolved, recurred = [], []
    for sig, rec in latest.items():
        if str(rec.get("state")) != PENDING_VERIFICATION:
            continue
        pending_ts = str(rec.get("ts", ""))
        try:
            pend_epoch = datetime.fromisoformat(pending_ts).timestamp()
        except (ValueError, TypeError):
            pend_epoch = 0.0
        recurred_after = any(
            s == sig and mt > pend_epoch + 1 for s, mt in report_index)
        # detector opportunity = ANY friction (of any signature) was produced
        # after we surfaced ⇒ the detectors were live and would have re-fired
        # this signature if it were still broken.
        had_opportunity = any(mt > pend_epoch + 1 for _s, mt in report_index)
        if recurred_after:
            record_attempt(
                data_dir, friction_signature=sig,
                friction_type=str(rec.get("friction_type", "")),
                resolution_fingerprint=str(rec.get("resolution_fingerprint", "")),
                state=RECURRED_FAILED, now_iso=now_iso, source="friction_response",
                notes="recurred after surfaced fix",
            )
            recurred.append(sig)
            continue
        gap = _hours_between(pending_ts, now_iso)
        if gap is None or gap < RESOLVED_WINDOW_HOURS:
            continue  # still pending
        if not had_opportunity:
            record_attempt(
                data_dir, friction_signature=sig,
                friction_type=str(rec.get("friction_type", "")),
                resolution_fingerprint=str(rec.get("resolution_fingerprint", "")),
                state=UNKNOWN_NO_OBS, now_iso=now_iso, source="friction_response",
            )
            continue
        n = archive_resolved_signature(
            data_dir, friction_signature=sig, now_iso=now_iso, ledger_ref=pending_ts,
        )
        record_attempt(
            data_dir, friction_signature=sig,
            friction_type=str(rec.get("friction_type", "")),
            resolution_fingerprint=str(rec.get("resolution_fingerprint", "")),
            state=RESOLVED, now_iso=now_iso, source="friction_response",
            notes=f"quiet {RESOLVED_WINDOW_HOURS}h, archived {n}",
        )
        resolved.append(sig)
    return {"resolved": resolved, "recurred": recurred}


# ---------------------------------------------------------------------------
# Conversational authorization (§3, §3A-i) — bind a plain "yes" to ONE pending
# fix, or no-op. A thin layer over REQUEST-APPROVAL-ACTION-V1 receipts: the
# durable single-use + TTL state lives in the receipt; THIS adds the natural-
# language + same-user/space/reply binding rules on top.
# ---------------------------------------------------------------------------

PENDING_FIX_KIND = "friction_fix_authorization"
ASK_TTL_SECONDS = 900  # short — the ask expects an answer in the moment

_AFFIRMATIVES = frozenset({
    "yes", "yeah", "yep", "yup", "ya", "sure", "ok", "okay", "k", "go",
    "do it", "go ahead", "go for it", "please do", "yes please", "do that",
    "sounds good", "fix it", "approved", "approve", "confirm", "confirmed",
    "send it", "make it so", "proceed",
})
_NEGATIONS = ("no", "not", "don't", "dont", "stop", "wait", "hold", "cancel",
              "nope", "nah", "never")


def is_affirmative(text: str) -> bool:
    """Conservative: a SHORT, clearly-affirmative reply with no negation.
    Anything ambiguous returns False so it no-ops + re-asks rather than acting
    on a stray 'yes' (§3A-i)."""
    norm = " ".join((text or "").lower().split())
    norm = norm.strip(" .!?,")
    if not norm or len(norm.split()) > 4:
        return False
    if any(neg in norm.split() for neg in _NEGATIONS):
        return False
    if norm in _AFFIRMATIVES:
        return True
    # allow a leading affirmative phrase ("yes please do it")
    return any(norm == a or norm.startswith(a + " ") for a in _AFFIRMATIVES)


def authorize_natural_yes(
    pending: list[dict], *, user_id: str, space_id: str,
    in_reply_to: str, text: str,
) -> tuple[str | None, str]:
    """Decide whether a reply authorizes a pending fix. ``pending`` is the list
    of OPEN (non-expired, un-consumed) friction-fix receipts, each a dict with
    ``approval_id`` + binding fields ``user_id``/``space_id``/``ask_message_id``.
    Returns ``(approval_id, reason)`` to consume, or ``(None, reason)`` to no-op.
    Enforces ALL of §3A-i: same user + space, exactly one pending, direct reply
    OR a bare next-turn affirmative, and a genuinely affirmative message."""
    here = [p for p in pending if p.get("space_id") == space_id]
    if len(here) != 1:
        return None, ("no_pending" if not here else "multiple_pending")
    p = here[0]
    if p.get("user_id") != user_id:
        return None, "different_user"
    # Direct reply to the ask, OR a bare affirmative next-turn (no reply
    # threading). A reply to some OTHER message is not a binding yes.
    rt = (in_reply_to or "").strip()
    if rt and rt != str(p.get("ask_message_id", "")):
        return None, "reply_to_other"
    if not is_affirmative(text):
        return None, "not_affirmative"
    return str(p.get("approval_id", "")), "authorized"


__all__ = [
    "is_enabled", "SELF_FRICTION_SOURCES", "COOLDOWN_HOURS",
    "PENDING_FIX_KIND", "ASK_TTL_SECONDS", "is_affirmative",
    "authorize_natural_yes",
    "MAX_RESPONSES_PER_DAY", "VERIFY_WINDOW_HOURS",
    "friction_signature", "resolution_fingerprint", "signature_of_filename",
    "ATTEMPTED", "IN_FLIGHT", "PENDING_VERIFICATION", "RESOLVED",
    "RECURRED_FAILED", "UNKNOWN_NO_OBS",
    "record_attempt", "load_attempts", "failed_fingerprints",
    "reserve_in_flight", "should_respond", "judge_resolution",
    "archive_resolved_signature", "RESOLVED_WINDOW_HOURS",
    "list_open_signatures", "respond_once", "verify_and_archive",
]

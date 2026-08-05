"""GOVERNANCE-STATE-DOCUMENT-V1 — one atomically-replaced document.

Supersedes the three-artifact governance lifecycle (open item + archive file +
append-only manifest) shipped across b1db4b6..f38b31f. Nine review rounds found
nine reachable failure families there, and the reviewer's diagnosis was that the
source lifecycle raced the audit lifecycle while the lock protected only the
latter.

This does not add a tenth guard. It removes the artifacts. One document holds
both open items and retained closed history, so most of those families become
*unrepresentable* rather than *guarded*: there is no archive to validate, no
path derived from persisted state, no manifest to race, and no source lifecycle
separate from the audit lifecycle.

Design rules that are load-bearing (each traces to a specific prior failure):

* **Document-wide lock over the whole read-decide-write.** Per-signature locks
  are wrong for writers replacing one shared document. Lock acquisition failure
  **fails closed** — the race is worse than refusing to record, and there is no
  unlocked fallback.
* **Compare-and-close.** ``close(signature, expected_occurrence)`` — a
  signature-only close lets an ambiguous retry absorb a *new* recurrence, which
  is the central lost-recurrence family.
* **Occurrence identity is persisted**, never re-derived from a timestamp, and a
  recurrence always mints a new one.
* **Candidate validation preserves every prior closed entry AND every unrelated
  open entry** before the replace is allowed.
* **Shadow archive, never delete** becomes retained ``closed`` state.

Full contract: ``specs/GOVERNANCE-STATE-DOCUMENT-V1.md``; the failure families
it answers: ``docs/reference/governance-lifecycle-failure-state-enumeration.md``.
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: Bounded so migration notes cannot become an unbounded log inside state.
MAX_MIGRATION_NOTES = 50


class CloseResult(str, Enum):
    """Outcome of a compare-and-close.

    A bool cannot distinguish "already done" from "refused because something
    newer is open", and the caller must not treat those alike.
    """

    CLOSED = "closed"
    ALREADY_CLOSED = "already_closed"
    OCCURRENCE_MISMATCH = "occurrence_mismatch"
    NOT_OPEN = "not_open"
    STATE_ERROR = "state_error"


@dataclass(frozen=True)
class GovernanceItem:
    """One open occurrence, as surfaced to readers."""

    occurrence: str
    signature: str
    title: str
    condition: str
    payload: tuple
    opened_iso: str
    last_seen_iso: str
    human_gated: bool = True


def state_path(data_dir: str) -> Path:
    return Path(data_dir) / "diagnostics" / "governance" / "state.json"


def _lock_path(data_dir: str) -> Path:
    return state_path(data_dir).with_name("state.json.lock")


def new_occurrence_id(signature: str, opened_iso: str, prior: str = "") -> str:
    """Identity for one OPEN occurrence.

    Mixing in ``prior`` guarantees a successor differs from its predecessor even
    when both are created within the same clock tick — a recurrence must never
    inherit the identity of the occurrence it follows.
    """
    basis = f"{signature}|{opened_iso}"
    if prior:
        basis = f"{basis}|{prior}"
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


class StateError(RuntimeError):
    """Unreadable, corrupt, or unknown-version state. Always fails closed."""


@contextmanager
def _document_lock(data_dir: str):
    """Serialize the ENTIRE read-decide-write across processes.

    Fails closed. An unlocked fallback would let two writers replace the
    document from stale snapshots, each silently discarding the other's work —
    which is strictly worse than declining to record.
    """
    path = _lock_path(data_dir)
    fh = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = path.open("a+")
        try:
            import fcntl
        except ImportError as exc:  # pragma: no cover - platform guard
            raise StateError(
                "no cross-process lock available on this platform; refusing to "
                "mutate governance state unlocked") from exc
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise StateError(f"could not acquire governance state lock: {exc}") from exc
        yield
    finally:
        if fh is not None:
            try:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                fh.close()
            except Exception:
                pass


def _empty_document() -> dict:
    return {"schema_version": SCHEMA_VERSION, "migration_notes": [],
            "open": {}, "closed": []}


def read_document(data_dir: str) -> dict:
    """Load the state document, or raise ``StateError``.

    A missing document is an empty document. An unreadable, malformed, or
    unknown-version one is NOT — reporting those as empty would let a write
    silently discard committed history.
    """
    path = state_path(data_dir)
    if not path.is_file():
        return _empty_document()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StateError(f"governance state unreadable: {exc}") from exc
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise StateError(f"governance state is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise StateError("governance state is not an object")
    version = doc.get("schema_version")
    if version != SCHEMA_VERSION:
        raise StateError(
            f"unknown governance schema_version {version!r} "
            f"(this build understands {SCHEMA_VERSION})")
    for key, kind in (("open", dict), ("closed", list), ("migration_notes", list)):
        if not isinstance(doc.get(key), kind):
            raise StateError(f"governance state field {key!r} has the wrong shape")
    return doc


def _validate_candidate(previous: dict, candidate: dict, *, signature: str) -> None:
    """Refuse a write that would drop history or an unrelated open item.

    Validating only closed history would let a write silently discard another
    signature's open entry — the write path must not be able to lose work it was
    never asked to touch.
    """
    if candidate.get("schema_version") != SCHEMA_VERSION:
        raise StateError("candidate has the wrong schema_version")

    prior_closed = {c["occurrence"] for c in previous.get("closed", [])}
    new_closed = {c["occurrence"] for c in candidate.get("closed", [])}
    lost = prior_closed - new_closed
    if lost:
        raise StateError(f"candidate would drop closed history: {sorted(lost)}")

    prior_open = set(previous.get("open", {})) - {signature}
    new_open = set(candidate.get("open", {}))
    dropped = prior_open - new_open
    if dropped:
        raise StateError(
            f"candidate would drop unrelated open entries: {sorted(dropped)}")

    if len(candidate.get("migration_notes", [])) > MAX_MIGRATION_NOTES:
        raise StateError("migration_notes exceeded its bound")


def _write_document(data_dir: str, doc: dict) -> None:
    """Atomically replace the document. Durability is fsync'd, not assumed."""
    path = state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".state-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, separators=(",", ":"), sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
        # fsync the directory so the rename itself survives a crash.
        dir_fd = os.open(str(path.parent), os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            tmp.unlink()
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                logger.warning("GOVERNANCE_TMP_CLEANUP_FAILED path=%s", tmp)
        raise


def upsert_item(
    data_dir: str, *, signature: str, title: str, condition: str,
    payload: list, now_iso: str, human_gated: bool = True,
) -> str:
    """Open or update the occurrence for ``signature``; return its occurrence id.

    Re-detection updates payload and last-seen and PRESERVES the original
    ``opened_iso`` and occurrence — one item per condition, never a duplicate
    per scan. A signature whose previous occurrence is already closed starts a
    NEW occurrence, so a recurrence is never folded into closed history.
    """
    with _document_lock(data_dir):
        doc = read_document(data_dir)
        existing = doc["open"].get(signature)
        if existing:
            occurrence = existing["occurrence"]
            opened_iso = existing["opened_iso"]
        else:
            prior = ""
            for entry in reversed(doc["closed"]):
                if entry["signature"] == signature:
                    prior = entry["occurrence"]
                    break
            opened_iso = now_iso
            occurrence = new_occurrence_id(signature, opened_iso, prior)

        candidate = json.loads(json.dumps(doc))
        candidate["open"][signature] = {
            "occurrence": occurrence,
            "signature": signature,
            "title": title,
            "condition": condition,
            "payload": list(payload),
            "opened_iso": opened_iso,
            "last_seen_iso": now_iso,
            "human_gated": bool(human_gated),
        }
        _validate_candidate(doc, candidate, signature=signature)
        _write_document(data_dir, candidate)
        return occurrence


def close_item(
    data_dir: str, *, signature: str, expected_occurrence: str,
    now_iso: str, resolving_condition: str,
) -> CloseResult:
    """Compare-and-close: close ONLY the occurrence the caller observed.

    Precedence is part of the contract, not an implementation detail —
    ``ALREADY_CLOSED`` is checked before ``OCCURRENCE_MISMATCH`` so an ambiguous
    retry acknowledges the occurrence it meant while leaving a newer one
    untouched, and the next scan evaluates that newer one independently.
    """
    try:
        with _document_lock(data_dir):
            doc = read_document(data_dir)

            if any(c["occurrence"] == expected_occurrence for c in doc["closed"]):
                return CloseResult.ALREADY_CLOSED

            entry = doc["open"].get(signature)
            if entry is None:
                return CloseResult.NOT_OPEN
            if entry["occurrence"] != expected_occurrence:
                return CloseResult.OCCURRENCE_MISMATCH

            candidate = json.loads(json.dumps(doc))
            closing = candidate["open"].pop(signature)
            candidate["closed"].append({
                "occurrence": closing["occurrence"],
                "signature": closing["signature"],
                "title": closing["title"],
                "payload": closing["payload"],
                "opened_iso": closing["opened_iso"],
                "closed_iso": now_iso,
                "resolving_condition": resolving_condition,
                "human_gated": closing["human_gated"],
            })
            _validate_candidate(doc, candidate, signature=signature)
            _write_document(data_dir, candidate)
            return CloseResult.CLOSED
    except StateError as exc:
        logger.warning("GOVERNANCE_CLOSE_STATE_ERROR signature=%s: %s", signature, exc)
        return CloseResult.STATE_ERROR


def open_items(data_dir: str) -> list:
    """Every open occurrence. Read-only: enumeration is never surfacing."""
    try:
        doc = read_document(data_dir)
    except StateError as exc:
        logger.warning("GOVERNANCE_READ_FAILED: %s", exc)
        return []
    return [
        GovernanceItem(
            occurrence=e["occurrence"], signature=e["signature"], title=e["title"],
            condition=e.get("condition", ""), payload=tuple(e.get("payload", ())),
            opened_iso=e["opened_iso"], last_seen_iso=e["last_seen_iso"],
            human_gated=bool(e.get("human_gated", True)),
        )
        for e in doc["open"].values()
    ]


def closed_items(data_dir: str) -> list:
    """Retained closed history — the shadow archive. Never deleted."""
    try:
        return list(read_document(data_dir)["closed"])
    except StateError:
        return []

"""Instance-identity resolver — CLI-FIRST-CORE-V1 A4.

One resolver for every entry point (server, repl, CLI). Precedence:

1. ``KERNOS_INSTANCE_ID`` env — explicit always wins (today's behavior).
2. A persisted identity marker in the data dir (written by a prior boot).
3. Legacy adoption: exactly one distinct ``instance_id`` across the
   instance DB's ``members`` rows → adopt it and persist the marker.
   Rows may be ``discord:*``-, phone-, or explicit-keyed — all valid.
4. Ambiguity (multiple legacy candidates) → refuse, listing candidates
   and the env-var instruction. Never guess a tenant.
5. Fresh data dir → generate one collision-resistant, platform-neutral
   ID and persist it atomically. Identity is never re-derived from
   mutable hostname/user state on later boots.

The marker file is ``{data_dir}/instance_identity.json``. Writes are
tmp-file + ``os.replace`` atomic. The resolver is synchronous on the
filesystem side; the legacy scan reads sqlite directly (read-only) so it
can run before the async InstanceDB is constructed.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_MARKER_NAME = "instance_identity.json"
_MARKER_VERSION = 1


class AmbiguousInstanceIdentity(Exception):
    """Multiple legacy instance ids exist; refuse to guess the tenant."""

    def __init__(self, candidates: list[str]) -> None:
        self.candidates = candidates
        listing = ", ".join(candidates)
        super().__init__(
            "Multiple instance identities exist in this data dir: "
            f"{listing}. Set KERNOS_INSTANCE_ID to the one this process "
            "should serve."
        )


def _marker_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / _MARKER_NAME


def _read_marker(data_dir: str | Path) -> str:
    """Return the persisted instance id, or '' when absent/invalid."""
    try:
        raw = json.loads(_marker_path(data_dir).read_text(encoding="utf-8"))
        value = raw.get("instance_id", "")
        return value if isinstance(value, str) else ""
    except (OSError, json.JSONDecodeError, AttributeError):
        return ""


def _write_marker(data_dir: str | Path, instance_id: str, source: str) -> None:
    """Atomically persist the resolved identity beside the data it keys."""
    path = _marker_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}-"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "version": _MARKER_VERSION,
                    "instance_id": instance_id,
                    "source": source,
                },
                handle,
                sort_keys=True,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _legacy_instance_ids(data_dir: str | Path) -> list[str]:
    """Distinct non-empty instance ids from the instance DB, read-only.

    Missing DB or missing table → empty list (fresh install).
    """
    db_path = Path(data_dir) / "instance.db"
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT DISTINCT instance_id FROM members "
                "WHERE instance_id IS NOT NULL AND instance_id != ''"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("Legacy instance-id scan failed (treated as fresh): %s", exc)
        return []
    return sorted({row[0] for row in rows})


def _generate_instance_id() -> str:
    """Collision-resistant, platform-neutral, stable-once-persisted."""
    return f"kernos:{uuid.uuid4().hex[:12]}"


def resolve_instance_id(data_dir: str | Path | None = None) -> str:
    """Resolve this process's instance identity per CLI-FIRST-CORE-V1 A4.

    Raises AmbiguousInstanceIdentity when the data dir holds multiple
    legacy tenants and no explicit env override was given.
    """
    explicit = os.getenv("KERNOS_INSTANCE_ID", "")
    if explicit:
        return explicit

    _data_dir = data_dir or os.getenv("KERNOS_DATA_DIR", "./data")

    persisted = _read_marker(_data_dir)
    if persisted:
        return persisted

    legacy = _legacy_instance_ids(_data_dir)
    if len(legacy) == 1:
        adopted = legacy[0]
        _write_marker(_data_dir, adopted, source="legacy-adoption")
        logger.info("INSTANCE: adopted legacy identity %s (persisted)", adopted)
        return adopted
    if len(legacy) > 1:
        raise AmbiguousInstanceIdentity(legacy)

    generated = _generate_instance_id()
    _write_marker(_data_dir, generated, source="generated")
    logger.info("INSTANCE: generated fresh identity %s (persisted)", generated)
    return generated

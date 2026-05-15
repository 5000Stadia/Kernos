"""Friction pattern catalog — stable IDs over Friction Observer signals.

FRICTION-PATTERN-STABLE-IDS-V1. SQLite-backed catalog of named friction
patterns plus the auto-classifier that tags incoming
``FrictionSignal`` instances against them. Lives in shared
``data/instance.db``, scoped by ``instance_id`` columns; mirrors the
architectural shape of ``kernos/kernel/reference/catalog.py``
(REFERENCE-PRIMITIVE-V1).

Pattern IDs are **immutable** kebab-case slugs; identity that changes
over time rides in ``display_name``, ``description``, and
append-only ``aliases``. Lifecycle: active → resolved → reactivated
→ archived, with ``record_occurrence`` (active/reactivated) and
``record_recurrence`` (resolved → may reactivate) as separate paths.

Classifier has two paths:

* **Path A** — exact match against a pattern's ``signal_type_keys``.
  Deterministic; scores 1.0; not subject to a threshold.
* **Path B** — normalized token-overlap (Jaccard + phrase bonus) on
  cleaned descriptions. Subject to
  ``KERNOS_FRICTION_TOKEN_OVERLAP_THRESHOLD`` (default 0.6).

Path A wins over Path B. ``signal_type_keys`` are unique per instance
across active/reactivated patterns (enforced both at create-time and
on lifecycle transitions back into active/reactivated).

All mutating methods open SQLite transactions via ``BEGIN IMMEDIATE``
with a bounded retry loop on ``SQLITE_BUSY``
(``KERNOS_FRICTION_TXN_RETRY_LIMIT``); after exhaustion raises
``StoreContention``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite

from kernos.utils import utc_now

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

# Lifecycle states.
LIFECYCLE_ACTIVE = "active"
LIFECYCLE_RESOLVED = "resolved"
LIFECYCLE_REACTIVATED = "reactivated"
LIFECYCLE_ARCHIVED = "archived"

VALID_LIFECYCLE_STATES = frozenset({
    LIFECYCLE_ACTIVE,
    LIFECYCLE_RESOLVED,
    LIFECYCLE_REACTIVATED,
    LIFECYCLE_ARCHIVED,
})

# Permitted transitions per spec Decision 6's method-to-lifecycle table.
# Resolved → reactivated is NOT operator-callable; it happens only via
# the record_recurrence threshold check. Restricting the operator path
# prevents an operator from manually flipping a pattern to reactivated
# (which would bypass the threshold + window discipline that the
# autonomy loop depends on for measurement integrity).
_LIFECYCLE_TRANSITIONS: dict[str, frozenset[str]] = {
    LIFECYCLE_ACTIVE: frozenset({LIFECYCLE_RESOLVED, LIFECYCLE_ARCHIVED}),
    LIFECYCLE_RESOLVED: frozenset({LIFECYCLE_ACTIVE, LIFECYCLE_ARCHIVED}),
    LIFECYCLE_REACTIVATED: frozenset({LIFECYCLE_RESOLVED, LIFECYCLE_ARCHIVED}),
    LIFECYCLE_ARCHIVED: frozenset({LIFECYCLE_ACTIVE}),
}

# classified_by vocabulary (CHECK constraint mirrored in DDL below).
CLASSIFIED_AUTO_SIGNAL_TYPE = "auto-signal-type"
CLASSIFIED_AUTO_TOKEN_OVERLAP = "auto-token-overlap"
CLASSIFIED_MANUAL = "manual"
CLASSIFIED_BACKFILL = "backfill"

VALID_CLASSIFIED_BY = frozenset({
    CLASSIFIED_AUTO_SIGNAL_TYPE,
    CLASSIFIED_AUTO_TOKEN_OVERLAP,
    CLASSIFIED_MANUAL,
    CLASSIFIED_BACKFILL,
})


# Path B normalized scorer constants.
# _PATH_B_MIN_CLEANED_TOKENS: short-description guard floor. Hard-coded
# per architect call Q3 (v3→v4 fold): tunability deferred until soak
# data justifies env-var-promotion.
_PATH_B_MIN_CLEANED_TOKENS = 3

_STOPWORDS = frozenset({
    "the", "and", "for", "with", "that", "this",
    "from", "into", "are", "was",
})

_PHRASE_BONUS = 0.3

_TOKEN_RX = re.compile(r"\w+")
_SLUG_RX = re.compile(r"[^a-z0-9]+")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "FRICTION_PATTERNS: invalid %s=%r, using default %s",
            name, raw, default,
        )
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "FRICTION_PATTERNS: invalid %s=%r, using default %d",
            name, raw, default,
        )
        return default


# Reactivation discipline tunables.
def _reactivation_threshold() -> int:
    return _env_int("KERNOS_FRICTION_REACTIVATION_THRESHOLD", 3)


def _reactivation_window_days() -> int:
    return _env_int("KERNOS_FRICTION_REACTIVATION_WINDOW_DAYS", 7)


def _token_overlap_threshold() -> float:
    return _env_float("KERNOS_FRICTION_TOKEN_OVERLAP_THRESHOLD", 0.6)


def _txn_retry_limit() -> int:
    # Clamp to >=0 (Codex post-impl architecture note): a negative value
    # in the env var would otherwise make the retry loop do no work
    # (range(N+1) where N+1<=0). The 0 case is a legitimate testing
    # path (force immediate-failure semantics for StoreContention pin);
    # negative values aren't.
    return max(0, _env_int("KERNOS_FRICTION_TXN_RETRY_LIMIT", 3))


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FrictionPatternStoreError(Exception):
    """Base class for store-level errors."""


class UnknownPattern(FrictionPatternStoreError):
    """No row for the given (instance_id, pattern_id)."""


class SignalTypeKeyCollision(ValueError):
    """Raised when ``create_pattern`` or ``transition_lifecycle`` into
    active/reactivated would result in ``signal_type_keys`` colliding
    with another active/reactivated pattern's keys for the same
    instance."""


class AliasCollision(ValueError):
    """Raised when ``add_alias`` would create an alias colliding with
    another pattern's ``pattern_id`` OR another pattern's existing
    alias in the same instance."""


class PatternArchived(RuntimeError):
    """Raised when ``record_occurrence`` / ``record_recurrence`` is
    called on an archived pattern. Operator must transition out of
    archived first."""


class InvalidLifecycleTransition(ValueError):
    """Raised when a transition_lifecycle call's new_state is invalid
    or the (current_state, new_state) pair isn't permitted."""


class StoreContention(RuntimeError):
    """Raised after the BEGIN IMMEDIATE retry loop exhausts its budget
    (default 3 retries, env-tunable via
    ``KERNOS_FRICTION_TXN_RETRY_LIMIT``). Indicates extreme concurrent
    contention on the same pattern."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def slugify(seed: str) -> str:
    """Deterministic kebab-case slug. Lowercases, strips non-alphanumeric,
    collapses runs to single hyphen, trims leading/trailing hyphens.

    Returns the empty string if the seed has no usable characters; the
    store appends a uuid8 fallback in that case.
    """
    lowered = (seed or "").lower()
    collapsed = _SLUG_RX.sub("-", lowered).strip("-")
    return collapsed


def _tokenize(text: str) -> list[str]:
    """Lowercased word-character tokens. Mirrors ``canvas.py:1526``
    algorithmically."""
    if not text:
        return []
    return [t.lower() for t in _TOKEN_RX.findall(text)]


def _clean_tokens(tokens: list[str]) -> set[str]:
    """Drop tokens shorter than 3 chars or in the stopword set."""
    return {t for t in tokens if len(t) >= 3 and t not in _STOPWORDS}


def _normalized_score_path_b(
    signal_description: str,
    pattern_description: str,
) -> float:
    """Path B classifier score per the spec's normalized scorer.

    Returns a value in ``[0.0, 1.0]``. Honors
    ``_PATH_B_MIN_CLEANED_TOKENS`` short-description floor.
    """
    signal_clean = _clean_tokens(_tokenize(signal_description))
    pattern_clean = _clean_tokens(_tokenize(pattern_description))

    if (
        len(signal_clean) < _PATH_B_MIN_CLEANED_TOKENS
        or len(pattern_clean) < _PATH_B_MIN_CLEANED_TOKENS
    ):
        return 0.0

    overlap = len(signal_clean & pattern_clean)
    union = len(signal_clean | pattern_clean)
    jaccard = overlap / union if union else 0.0

    needle = (pattern_description or "").lower().strip()
    haystack = (signal_description or "").lower()
    phrase_bonus = _PHRASE_BONUS if needle and needle in haystack else 0.0

    return min(1.0, jaccard + phrase_bonus)


# ---------------------------------------------------------------------------
# Spec frontmatter parser
# ---------------------------------------------------------------------------


_FRONTMATTER_FENCE = "---"
_PATTERN_REFS_KEY = "addresses_friction_patterns"


def parse_spec_pattern_refs(spec_path: Path | str) -> list[str]:
    """Read ``addresses_friction_patterns`` from a spec's YAML frontmatter.

    Returns ``[]`` if the spec has no frontmatter or no field. Tolerant
    of formatting: any parse error returns ``[]`` rather than raising
    (frontmatter is optional metadata, not load-bearing).
    """
    try:
        path = Path(spec_path)
        if not path.exists():
            return []
        with open(path, "r", encoding="utf-8") as f:
            head = f.read(8192)
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("FRICTION_PATTERNS: spec read failed %s: %s", spec_path, exc)
        return []

    stripped = head.lstrip()
    if not stripped.startswith(_FRONTMATTER_FENCE):
        return []

    # Find closing fence.
    after_open = stripped[len(_FRONTMATTER_FENCE):].lstrip("\n")
    close_idx = after_open.find("\n" + _FRONTMATTER_FENCE)
    if close_idx == -1:
        return []
    frontmatter = after_open[:close_idx]

    refs: list[str] = []
    in_addresses = False
    for line in frontmatter.splitlines():
        stripped_line = line.rstrip()
        if not stripped_line:
            in_addresses = False
            continue
        if stripped_line.startswith(f"{_PATTERN_REFS_KEY}:"):
            in_addresses = True
            # Inline-list form: "addresses_friction_patterns: [a, b]"
            tail = stripped_line[len(_PATTERN_REFS_KEY) + 1:].strip()
            if tail.startswith("[") and tail.endswith("]"):
                in_addresses = False
                inner = tail[1:-1].strip()
                if inner:
                    refs.extend(
                        s.strip().strip('"').strip("'")
                        for s in inner.split(",")
                        if s.strip()
                    )
            continue
        if in_addresses:
            # Yaml list item.
            if stripped_line.lstrip().startswith("- "):
                item = stripped_line.lstrip()[2:].strip().strip('"').strip("'")
                if item:
                    refs.append(item)
            else:
                # Hit a new field; stop.
                in_addresses = False
    return [r for r in refs if r]


# ---------------------------------------------------------------------------
# FrictionPattern dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrictionPattern:
    instance_id: str
    pattern_id: str
    description: str
    signal_type_keys: tuple[str, ...] = ()
    display_name: str = ""
    aliases: tuple[str, ...] = ()
    parent_pattern_id: str = ""
    lifecycle_state: str = LIFECYCLE_ACTIVE
    occurrence_count: int = 0
    first_observed_at: str = ""
    last_observed_at: str = ""
    resolved_at: str = ""
    resolved_by_spec: str = ""
    reactivated_at: str = ""
    created_at: str = ""
    # Spec 6: instance-scoped monotonic counter incremented when the
    # pattern enters an active-class state (ACTIVE on creation or
    # archived-revival; REACTIVATED via record_recurrence threshold or
    # operator path). Emitters (FrictionPatternFrequencyEmitter) use
    # this to dedupe: if a fired event's active_epoch matches the
    # pattern's current active_epoch, the emitter knows it's the same
    # activation episode and can decide whether to re-fire. The epoch
    # is per-instance (MAX+1 over the instance's friction_pattern rows)
    # so emitters can persist a "last fired epoch" cursor.
    active_epoch: int = 0


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_FRICTION_PATTERN_DDL = """
CREATE TABLE IF NOT EXISTS friction_pattern (
    instance_id         TEXT NOT NULL,
    pattern_id          TEXT NOT NULL,
    parent_pattern_id   TEXT NOT NULL DEFAULT '',
    display_name        TEXT NOT NULL DEFAULT '',
    description         TEXT NOT NULL,
    signal_type_keys    TEXT NOT NULL DEFAULT '[]',
    aliases             TEXT NOT NULL DEFAULT '[]',
    lifecycle_state     TEXT NOT NULL DEFAULT 'active',
    occurrence_count    INTEGER NOT NULL DEFAULT 0,
    first_observed_at   TEXT NOT NULL DEFAULT '',
    last_observed_at    TEXT NOT NULL DEFAULT '',
    resolved_at         TEXT NOT NULL DEFAULT '',
    resolved_by_spec    TEXT NOT NULL DEFAULT '',
    reactivated_at      TEXT NOT NULL DEFAULT '',
    active_epoch        INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (instance_id, pattern_id),
    CHECK (lifecycle_state IN (
        'active', 'resolved', 'reactivated', 'archived'
    ))
)
"""

_FRICTION_PATTERN_IDX_STATE = """
CREATE INDEX IF NOT EXISTS idx_friction_pattern_instance_state
    ON friction_pattern (instance_id, lifecycle_state)
"""

_FRICTION_PATTERN_IDX_PARENT = """
CREATE INDEX IF NOT EXISTS idx_friction_pattern_parent
    ON friction_pattern (instance_id, parent_pattern_id)
"""

_FRICTION_PATTERN_OCCURRENCE_DDL = """
CREATE TABLE IF NOT EXISTS friction_pattern_occurrence (
    occurrence_id       TEXT PRIMARY KEY,
    instance_id         TEXT NOT NULL,
    pattern_id          TEXT NOT NULL,
    observed_at         TEXT NOT NULL,
    report_path         TEXT NOT NULL DEFAULT '',
    classifier_score    REAL NOT NULL DEFAULT 0.0,
    classified_by       TEXT NOT NULL DEFAULT 'auto-signal-type',
    space_id            TEXT NOT NULL DEFAULT '',
    member_id           TEXT NOT NULL DEFAULT '',
    is_recurrence       INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (instance_id, pattern_id)
        REFERENCES friction_pattern(instance_id, pattern_id)
        ON DELETE RESTRICT,
    CHECK (classified_by IN (
        'auto-signal-type', 'auto-token-overlap', 'manual', 'backfill'
    ))
)
"""

_FRICTION_PATTERN_OCCURRENCE_IDX_PATTERN = """
CREATE INDEX IF NOT EXISTS idx_friction_pattern_occurrence_pattern
    ON friction_pattern_occurrence (instance_id, pattern_id, observed_at)
"""

_FRICTION_PATTERN_OCCURRENCE_IDX_WINDOW = """
CREATE INDEX IF NOT EXISTS idx_friction_pattern_occurrence_window
    ON friction_pattern_occurrence (instance_id, observed_at)
"""

_FRICTION_PATTERN_OCCURRENCE_IDX_REPORT = """
CREATE UNIQUE INDEX IF NOT EXISTS uniq_friction_pattern_occurrence_report
    ON friction_pattern_occurrence (instance_id, report_path)
    WHERE report_path != ''
"""


# ---------------------------------------------------------------------------
# Row marshalling
# ---------------------------------------------------------------------------


def _row_to_pattern(row: aiosqlite.Row) -> FrictionPattern:
    try:
        signal_type_keys = tuple(json.loads(row["signal_type_keys"]) or [])
    except Exception:
        signal_type_keys = ()
    try:
        aliases = tuple(json.loads(row["aliases"]) or [])
    except Exception:
        aliases = ()
    # Spec 6: active_epoch may be absent on pre-migration rows / row
    # shims that don't surface the column; default to 0.
    try:
        active_epoch = int(row["active_epoch"] or 0)
    except (KeyError, IndexError, TypeError, ValueError):
        active_epoch = 0
    return FrictionPattern(
        instance_id=row["instance_id"],
        pattern_id=row["pattern_id"],
        description=row["description"],
        signal_type_keys=signal_type_keys,
        display_name=row["display_name"],
        aliases=aliases,
        parent_pattern_id=row["parent_pattern_id"],
        lifecycle_state=row["lifecycle_state"],
        occurrence_count=row["occurrence_count"],
        first_observed_at=row["first_observed_at"],
        last_observed_at=row["last_observed_at"],
        resolved_at=row["resolved_at"],
        resolved_by_spec=row["resolved_by_spec"],
        reactivated_at=row["reactivated_at"],
        created_at=row["created_at"],
        active_epoch=active_epoch,
    )


# ---------------------------------------------------------------------------
# FrictionPatternStore
# ---------------------------------------------------------------------------


class FrictionPatternStore:
    """SQLite-backed catalog over ``data/instance.db``.

    Owns its own aiosqlite connection (per-module-isolation pattern;
    mirrors :class:`kernos.kernel.reference.catalog.CatalogStore`).
    """

    def __init__(self) -> None:
        self._db: aiosqlite.Connection | None = None
        self._db_path: Path | None = None
        self._write_lock = asyncio.Lock()

    # --- Lifecycle ----------------------------------------------------

    async def start(self, data_dir: str) -> None:
        if self._db is not None:
            return  # idempotent
        self._db_path = Path(data_dir) / "instance.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(
            str(self._db_path), isolation_level=None,
        )
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        # PRAGMA foreign_keys must be re-asserted on every new
        # connection — does not persist.
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute(_FRICTION_PATTERN_DDL)
        await self._db.execute(_FRICTION_PATTERN_IDX_STATE)
        await self._db.execute(_FRICTION_PATTERN_IDX_PARENT)
        await self._db.execute(_FRICTION_PATTERN_OCCURRENCE_DDL)
        await self._db.execute(_FRICTION_PATTERN_OCCURRENCE_IDX_PATTERN)
        await self._db.execute(_FRICTION_PATTERN_OCCURRENCE_IDX_WINDOW)
        await self._db.execute(_FRICTION_PATTERN_OCCURRENCE_IDX_REPORT)
        # WORKFLOW-AUTHORING-PRIMITIVES-V1 (Spec 5) Decision 8 +
        # Codex round-1 Medium 11: workflow_resolvable column on
        # friction_pattern. Architect curates the tag set; the
        # disposition layer's recurrence subscriber emits a soft
        # reflection only for tagged patterns. Idempotent ALTER
        # pattern (mirrors Spec 3's gate_nonce migration).
        async with self._db.execute(
            "SELECT name FROM pragma_table_info('friction_pattern')"
        ) as cur:
            cols = {row[0] for row in await cur.fetchall()}
        if "workflow_resolvable" not in cols:
            try:
                await self._db.execute(
                    "ALTER TABLE friction_pattern "
                    "ADD COLUMN workflow_resolvable INTEGER NOT NULL DEFAULT 0"
                )
            except aiosqlite.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        # Spec 6: active_epoch column migration. Same idempotent ALTER
        # pattern as workflow_resolvable. Existing pre-Spec-6 rows get
        # active_epoch=0 by default — emitters interpret 0 as "epoch
        # not yet recorded for this row" and either backfill on first
        # event or treat as pre-history (acceptable because pre-Spec-6
        # patterns predate the autonomy loop entirely).
        if "active_epoch" not in cols:
            try:
                await self._db.execute(
                    "ALTER TABLE friction_pattern "
                    "ADD COLUMN active_epoch INTEGER NOT NULL DEFAULT 0"
                )
            except aiosqlite.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise

    async def ensure_schema(self, data_dir: str) -> None:
        """Alias for ``start``. Mirrors the API name the spec uses."""
        await self.start(data_dir)

    async def stop(self) -> None:
        if self._db is None:
            return
        try:
            await self._db.close()
        finally:
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "FrictionPatternStore not started"
        return self._db

    # --- Active-epoch helper (Spec 6) ---------------------------------

    async def _next_active_epoch(
        self, db: aiosqlite.Connection, instance_id: str,
    ) -> int:
        """Spec 6: compute MAX(active_epoch)+1 across the instance.
        Called inside _run_in_immediate_txn bodies so the read +
        subsequent INSERT/UPDATE are serialized under the same write
        lock as other lifecycle changes — no race window where two
        transitions race for the same epoch.

        Returns 1 for the first activation in an instance (empty
        table). Pre-Spec-6 rows have active_epoch=0 from the ALTER
        TABLE default; MAX over a mixed set (0s + assigned epochs)
        still gives a monotonic next value.
        """
        async with db.execute(
            "SELECT COALESCE(MAX(active_epoch), 0) FROM friction_pattern "
            "WHERE instance_id = ?",
            (instance_id,),
        ) as cur:
            row = await cur.fetchone()
        current_max = int(row[0]) if row else 0
        return current_max + 1

    # --- Transaction discipline ---------------------------------------

    async def _run_in_immediate_txn(self, fn):
        """Run ``fn(db)`` inside BEGIN IMMEDIATE with bounded
        SQLITE_BUSY retry per architect call Q2. ``fn`` must be an
        async callable taking the connection."""
        retry_limit = _txn_retry_limit()
        backoff_ms = [50, 100, 200]
        async with self._write_lock:
            for attempt in range(retry_limit + 1):
                try:
                    await self.db.execute("BEGIN IMMEDIATE")
                    try:
                        result = await fn(self.db)
                        await self.db.execute("COMMIT")
                        return result
                    except Exception:
                        await self.db.execute("ROLLBACK")
                        raise
                except aiosqlite.OperationalError as exc:
                    msg = str(exc).lower()
                    if "busy" not in msg and "locked" not in msg:
                        raise
                    if attempt >= retry_limit:
                        raise StoreContention(
                            f"BEGIN IMMEDIATE retry exhausted after "
                            f"{retry_limit} retries: {exc}"
                        ) from exc
                    delay_ms = backoff_ms[min(attempt, len(backoff_ms) - 1)]
                    await asyncio.sleep(delay_ms / 1000.0)

    # --- Read paths ---------------------------------------------------

    async def get_pattern(
        self, instance_id: str, pattern_id_or_alias: str,
    ) -> FrictionPattern | None:
        if not pattern_id_or_alias:
            return None
        async with self.db.execute(
            "SELECT * FROM friction_pattern "
            "WHERE instance_id = ? AND pattern_id = ?",
            (instance_id, pattern_id_or_alias),
        ) as cur:
            row = await cur.fetchone()
        if row is not None:
            return _row_to_pattern(row)

        # Fall back to alias lookup. JSON aliases column is small; LIKE
        # is acceptable here because the alias is normalized so the
        # match is exact in token shape (kebab-case slug). Defensively
        # filter in Python to avoid false positives from substring.
        normalized = slugify(pattern_id_or_alias)
        target = normalized or pattern_id_or_alias
        async with self.db.execute(
            "SELECT * FROM friction_pattern WHERE instance_id = ?",
            (instance_id,),
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            try:
                aliases = json.loads(row["aliases"]) or []
            except Exception:
                aliases = []
            if target in aliases or pattern_id_or_alias in aliases:
                return _row_to_pattern(row)
        return None

    async def list_patterns(
        self,
        instance_id: str,
        *,
        lifecycle_state: str | None = None,
        parent_pattern_id: str | None = None,
    ) -> list[FrictionPattern]:
        sql = "SELECT * FROM friction_pattern WHERE instance_id = ?"
        params: list[Any] = [instance_id]
        if lifecycle_state is not None:
            sql += " AND lifecycle_state = ?"
            params.append(lifecycle_state)
        if parent_pattern_id is not None:
            sql += " AND parent_pattern_id = ?"
            params.append(parent_pattern_id)
        sql += " ORDER BY created_at ASC"
        async with self.db.execute(sql, tuple(params)) as cur:
            rows = await cur.fetchall()
        return [_row_to_pattern(r) for r in rows]

    # --- Create -------------------------------------------------------

    async def create_pattern(
        self,
        *,
        instance_id: str,
        description: str,
        signal_type_keys: list[str] | tuple[str, ...] = (),
        parent_pattern_id: str = "",
        display_name: str = "",
        seed_slug: str = "",
    ) -> FrictionPattern:
        if not description:
            raise ValueError("description is required")

        sig_keys = list(signal_type_keys or [])
        seed = seed_slug or description
        base_slug = slugify(seed) or f"pattern-{uuid.uuid4().hex[:8]}"

        async def _do(db: aiosqlite.Connection) -> FrictionPattern:
            # Path A uniqueness invariant: signal_type_keys must not
            # intersect any active/reactivated pattern's keys.
            await self._check_signal_type_keys_unique(
                db, instance_id, sig_keys, exclude_pattern_id=None,
            )

            # Slug collision retry — append numeric suffix.
            pattern_id = base_slug
            suffix = 2
            while True:
                async with db.execute(
                    "SELECT 1 FROM friction_pattern "
                    "WHERE instance_id = ? AND pattern_id = ?",
                    (instance_id, pattern_id),
                ) as cur:
                    exists = await cur.fetchone()
                if not exists:
                    break
                pattern_id = f"{base_slug}-{suffix}"
                suffix += 1

            now = utc_now()
            # Spec 6: new pattern enters ACTIVE → first activation
            # episode for this instance + pattern, so increment the
            # instance-scoped active_epoch counter.
            active_epoch = await self._next_active_epoch(db, instance_id)
            await db.execute(
                "INSERT INTO friction_pattern "
                "(instance_id, pattern_id, parent_pattern_id, display_name, "
                " description, signal_type_keys, aliases, lifecycle_state, "
                " occurrence_count, first_observed_at, last_observed_at, "
                " resolved_at, resolved_by_spec, reactivated_at, "
                " active_epoch, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    instance_id, pattern_id, parent_pattern_id, display_name,
                    description, json.dumps(sig_keys), json.dumps([]),
                    LIFECYCLE_ACTIVE, 0, "", "", "", "", "",
                    active_epoch, now,
                ),
            )
            return FrictionPattern(
                instance_id=instance_id,
                pattern_id=pattern_id,
                description=description,
                signal_type_keys=tuple(sig_keys),
                display_name=display_name,
                aliases=(),
                parent_pattern_id=parent_pattern_id,
                lifecycle_state=LIFECYCLE_ACTIVE,
                active_epoch=active_epoch,
                created_at=now,
            )

        return await self._run_in_immediate_txn(_do)

    async def _check_signal_type_keys_unique(
        self,
        db: aiosqlite.Connection,
        instance_id: str,
        candidate_keys: list[str],
        *,
        exclude_pattern_id: str | None,
    ) -> None:
        if not candidate_keys:
            return
        async with db.execute(
            "SELECT pattern_id, signal_type_keys FROM friction_pattern "
            "WHERE instance_id = ? AND lifecycle_state IN (?, ?)",
            (instance_id, LIFECYCLE_ACTIVE, LIFECYCLE_REACTIVATED),
        ) as cur:
            rows = await cur.fetchall()
        candidate_set = set(candidate_keys)
        for row in rows:
            if exclude_pattern_id and row["pattern_id"] == exclude_pattern_id:
                continue
            try:
                existing = set(json.loads(row["signal_type_keys"]) or [])
            except Exception:
                existing = set()
            overlap = candidate_set & existing
            if overlap:
                raise SignalTypeKeyCollision(
                    f"signal_type_keys {sorted(overlap)} already used by "
                    f"active/reactivated pattern {row['pattern_id']!r} "
                    f"in instance {instance_id!r}"
                )

    # --- Mutable identity fields --------------------------------------

    async def update_description(
        self, instance_id: str, pattern_id: str, new_description: str,
    ) -> FrictionPattern:
        if not new_description:
            raise ValueError("new_description is required")

        async def _do(db: aiosqlite.Connection) -> FrictionPattern:
            await self._require_pattern(db, instance_id, pattern_id)
            await db.execute(
                "UPDATE friction_pattern SET description = ? "
                "WHERE instance_id = ? AND pattern_id = ?",
                (new_description, instance_id, pattern_id),
            )
            return await self._load(db, instance_id, pattern_id)

        return await self._run_in_immediate_txn(_do)

    async def set_display_name(
        self, instance_id: str, pattern_id: str, name: str,
    ) -> FrictionPattern:
        async def _do(db: aiosqlite.Connection) -> FrictionPattern:
            await self._require_pattern(db, instance_id, pattern_id)
            await db.execute(
                "UPDATE friction_pattern SET display_name = ? "
                "WHERE instance_id = ? AND pattern_id = ?",
                (name, instance_id, pattern_id),
            )
            return await self._load(db, instance_id, pattern_id)

        return await self._run_in_immediate_txn(_do)

    async def add_alias(
        self, instance_id: str, pattern_id: str, alias: str,
    ) -> FrictionPattern:
        if not alias:
            raise ValueError("alias is required")
        normalized = slugify(alias)
        if not normalized:
            raise ValueError(f"alias {alias!r} normalizes to empty")

        async def _do(db: aiosqlite.Connection) -> FrictionPattern:
            current = await self._require_pattern(db, instance_id, pattern_id)
            # Idempotent on own pattern: no-op when already present.
            if normalized in (current.aliases or ()):
                return current

            # Collision: normalized alias must not match another pattern's
            # pattern_id or another pattern's existing alias.
            async with db.execute(
                "SELECT pattern_id, aliases FROM friction_pattern "
                "WHERE instance_id = ? AND pattern_id != ?",
                (instance_id, pattern_id),
            ) as cur:
                rows = await cur.fetchall()
            for row in rows:
                if row["pattern_id"] == normalized:
                    raise AliasCollision(
                        f"alias {normalized!r} collides with existing "
                        f"pattern_id in instance {instance_id!r}"
                    )
                try:
                    others = json.loads(row["aliases"]) or []
                except Exception:
                    others = []
                if normalized in others:
                    raise AliasCollision(
                        f"alias {normalized!r} collides with existing alias "
                        f"on pattern {row['pattern_id']!r} in instance "
                        f"{instance_id!r}"
                    )

            new_aliases = list(current.aliases or ()) + [normalized]
            await db.execute(
                "UPDATE friction_pattern SET aliases = ? "
                "WHERE instance_id = ? AND pattern_id = ?",
                (json.dumps(new_aliases), instance_id, pattern_id),
            )
            return await self._load(db, instance_id, pattern_id)

        return await self._run_in_immediate_txn(_do)

    # --- Lifecycle ----------------------------------------------------

    async def transition_lifecycle(
        self,
        instance_id: str,
        pattern_id: str,
        new_state: str,
        *,
        resolved_by_spec: str = "",
    ) -> FrictionPattern:
        if new_state not in VALID_LIFECYCLE_STATES:
            raise InvalidLifecycleTransition(
                f"new_state {new_state!r} not in {sorted(VALID_LIFECYCLE_STATES)}"
            )

        async def _do(db: aiosqlite.Connection) -> FrictionPattern:
            current = await self._require_pattern(db, instance_id, pattern_id)
            if current.lifecycle_state == new_state:
                return current

            # Pair validation per Decision 6's method-to-lifecycle table.
            # Codex post-impl Finding H2: enum membership alone is
            # insufficient — must also validate the (current, new) pair
            # is on the allowed list. Resolved → reactivated is
            # specifically NOT operator-callable; it's the threshold's
            # job. (See _LIFECYCLE_TRANSITIONS comment for rationale.)
            allowed = _LIFECYCLE_TRANSITIONS.get(
                current.lifecycle_state, frozenset(),
            )
            if new_state not in allowed:
                raise InvalidLifecycleTransition(
                    f"transition {current.lifecycle_state!r} → "
                    f"{new_state!r} is not allowed for "
                    f"pattern {pattern_id!r}; "
                    f"allowed from {current.lifecycle_state!r}: "
                    f"{sorted(allowed)}"
                )

            # Re-check signal_type_keys uniqueness on transitions BACK INTO
            # active/reactivated (architect call Q1 round-2 finding 6).
            if new_state in (LIFECYCLE_ACTIVE, LIFECYCLE_REACTIVATED):
                await self._check_signal_type_keys_unique(
                    db, instance_id, list(current.signal_type_keys),
                    exclude_pattern_id=pattern_id,
                )

            now = utc_now()
            updates: dict[str, Any] = {"lifecycle_state": new_state}
            if new_state == LIFECYCLE_RESOLVED:
                updates["resolved_at"] = now
                if resolved_by_spec:
                    updates["resolved_by_spec"] = resolved_by_spec
            elif new_state == LIFECYCLE_REACTIVATED:
                updates["reactivated_at"] = now
            # Spec 6: increment active_epoch when entering an
            # active-class state (ACTIVE via RESOLVED→ACTIVE manual
            # reactivation or ARCHIVED→ACTIVE revival; REACTIVATED via
            # this operator path). The increment happens inside the
            # same _run_in_immediate_txn body so the read+write are
            # serialized and no other transition can race for the
            # same epoch.
            if new_state in (LIFECYCLE_ACTIVE, LIFECYCLE_REACTIVATED):
                updates["active_epoch"] = await self._next_active_epoch(
                    db, instance_id,
                )

            set_clause = ", ".join(f"{k} = ?" for k in updates)
            params = list(updates.values()) + [instance_id, pattern_id]
            await db.execute(
                f"UPDATE friction_pattern SET {set_clause} "
                f"WHERE instance_id = ? AND pattern_id = ?",
                params,
            )
            return await self._load(db, instance_id, pattern_id)

        return await self._run_in_immediate_txn(_do)

    # --- Occurrence recording -----------------------------------------

    async def record_occurrence(
        self,
        *,
        instance_id: str,
        pattern_id: str,
        observed_at: str,
        report_path: str = "",
        classifier_score: float = 0.0,
        classified_by: str = CLASSIFIED_AUTO_SIGNAL_TYPE,
        space_id: str = "",
        member_id: str = "",
    ) -> None:
        """Record an occurrence on an active or reactivated pattern.

        Rejects with ``ValueError`` if pattern is resolved (caller must
        use ``record_recurrence``) or raises ``PatternArchived`` if
        archived. Idempotent on ``(instance_id, report_path)`` via the
        partial UNIQUE index.
        """
        if classified_by not in VALID_CLASSIFIED_BY:
            raise ValueError(
                f"classified_by {classified_by!r} not in "
                f"{sorted(VALID_CLASSIFIED_BY)}"
            )

        async def _do(db: aiosqlite.Connection) -> None:
            current = await self._require_pattern(db, instance_id, pattern_id)
            if current.lifecycle_state == LIFECYCLE_ARCHIVED:
                raise PatternArchived(
                    f"pattern {pattern_id!r} is archived; cannot record occurrence"
                )
            if current.lifecycle_state == LIFECYCLE_RESOLVED:
                raise ValueError(
                    f"pattern {pattern_id!r} is resolved; caller must use "
                    f"record_recurrence"
                )

            # Idempotency vs cross-pattern collision: same-pattern
            # duplicates are silently skipped (the catalog tolerates
            # reprocessing the same report). Cross-pattern duplicates
            # raise so the operator notices the misclassification
            # (per Decision 5's single-label invariant). Pre-check
            # avoids relying on string-matching the integrity error.
            if report_path:
                async with db.execute(
                    "SELECT pattern_id FROM friction_pattern_occurrence "
                    "WHERE instance_id = ? AND report_path = ?",
                    (instance_id, report_path),
                ) as cur:
                    existing = await cur.fetchone()
                if existing is not None:
                    if existing["pattern_id"] == pattern_id:
                        logger.debug(
                            "FRICTION_PATTERN_OCCURRENCE: idempotent skip on "
                            "duplicate report_path=%s", report_path,
                        )
                        return
                    # Cross-pattern duplicate — surface to caller.
                    raise aiosqlite.IntegrityError(
                        f"report_path {report_path!r} already associated with "
                        f"pattern {existing['pattern_id']!r}; cannot record "
                        f"under {pattern_id!r} (single-label invariant)"
                    )

            await db.execute(
                "INSERT INTO friction_pattern_occurrence "
                "(occurrence_id, instance_id, pattern_id, observed_at, "
                " report_path, classifier_score, classified_by, "
                " space_id, member_id, is_recurrence) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    uuid.uuid4().hex, instance_id, pattern_id, observed_at,
                    report_path, classifier_score, classified_by,
                    space_id, member_id, 0,
                ),
            )

            # Update pattern counters.
            now_observed = observed_at or utc_now()
            first_at = current.first_observed_at or now_observed
            await db.execute(
                "UPDATE friction_pattern SET "
                "occurrence_count = occurrence_count + 1, "
                "first_observed_at = ?, last_observed_at = ? "
                "WHERE instance_id = ? AND pattern_id = ?",
                (first_at, now_observed, instance_id, pattern_id),
            )

        await self._run_in_immediate_txn(_do)

    async def record_recurrence(
        self,
        *,
        instance_id: str,
        pattern_id: str,
        observed_at: str,
        report_path: str = "",
        classifier_score: float = 0.0,
        classified_by: str = CLASSIFIED_AUTO_SIGNAL_TYPE,
        space_id: str = "",
        member_id: str = "",
        emit_event=None,
    ) -> bool:
        """Record a recurrence on a resolved pattern. Returns True if
        the recurrence triggered reactivation.

        ``emit_event`` is an optional ``async (event_type, payload)``
        callable used to surface ``friction.pattern_recurrence`` and
        ``friction.pattern_reactivated``. If omitted, no event fires
        (test-friendly default; production wires the real event_stream
        emitter).
        """
        if classified_by not in VALID_CLASSIFIED_BY:
            raise ValueError(
                f"classified_by {classified_by!r} not in "
                f"{sorted(VALID_CLASSIFIED_BY)}"
            )

        triggered_reactivation = False
        recurrence_event: dict | None = None
        reactivation_event: dict | None = None

        async def _do(db: aiosqlite.Connection) -> bool:
            nonlocal triggered_reactivation
            nonlocal recurrence_event
            nonlocal reactivation_event

            current = await self._require_pattern(db, instance_id, pattern_id)
            if current.lifecycle_state == LIFECYCLE_ARCHIVED:
                raise PatternArchived(
                    f"pattern {pattern_id!r} is archived; cannot record recurrence"
                )
            if current.lifecycle_state != LIFECYCLE_RESOLVED:
                raise ValueError(
                    f"pattern {pattern_id!r} is {current.lifecycle_state}; "
                    f"caller must use record_occurrence"
                )

            # Same idempotency-vs-collision split as record_occurrence:
            # same-pattern duplicates are silently skipped; cross-pattern
            # duplicates raise so the single-label invariant is honored.
            if report_path:
                async with db.execute(
                    "SELECT pattern_id FROM friction_pattern_occurrence "
                    "WHERE instance_id = ? AND report_path = ?",
                    (instance_id, report_path),
                ) as cur:
                    existing_row = await cur.fetchone()
                if existing_row is not None:
                    if existing_row["pattern_id"] == pattern_id:
                        logger.debug(
                            "FRICTION_PATTERN_RECURRENCE: idempotent skip on "
                            "duplicate report_path=%s", report_path,
                        )
                        return False
                    raise aiosqlite.IntegrityError(
                        f"report_path {report_path!r} already associated with "
                        f"pattern {existing_row['pattern_id']!r}; cannot record "
                        f"recurrence under {pattern_id!r}"
                    )

            await db.execute(
                "INSERT INTO friction_pattern_occurrence "
                "(occurrence_id, instance_id, pattern_id, observed_at, "
                " report_path, classifier_score, classified_by, "
                " space_id, member_id, is_recurrence) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    uuid.uuid4().hex, instance_id, pattern_id, observed_at,
                    report_path, classifier_score, classified_by,
                    space_id, member_id, 1,
                ),
            )

            recurrence_event = {
                "instance_id": instance_id,
                "resolved_pattern_id": pattern_id,
                "observed_at": observed_at,
                "classified_by": classified_by,
            }

            # Reactivation check: count non-backfill recurrences within
            # the configured window since resolved_at.
            threshold = _reactivation_threshold()
            window_days = _reactivation_window_days()
            window_cutoff_dt = self._compute_window_cutoff(window_days)
            window_cutoff = window_cutoff_dt.isoformat()
            resolved_at = current.resolved_at or window_cutoff
            # Use the later of (resolved_at, window_cutoff) so old
            # post-resolve recurrences outside the window don't count.
            effective_lower = max(resolved_at, window_cutoff)

            async with db.execute(
                "SELECT COUNT(*) FROM friction_pattern_occurrence "
                "WHERE instance_id = ? AND pattern_id = ? "
                "AND is_recurrence = 1 "
                "AND classified_by != ? "
                "AND observed_at >= ?",
                (
                    instance_id, pattern_id, CLASSIFIED_BACKFILL,
                    effective_lower,
                ),
            ) as cur:
                row = await cur.fetchone()
            count = int(row[0]) if row else 0

            if count >= threshold:
                now = utc_now()
                # Spec 6: reactivation is an active-class state
                # transition; increment instance-scoped active_epoch.
                # Same _do txn body serializes the read+write.
                new_epoch = await self._next_active_epoch(db, instance_id)
                await db.execute(
                    "UPDATE friction_pattern SET "
                    "lifecycle_state = ?, reactivated_at = ?, "
                    "active_epoch = ? "
                    "WHERE instance_id = ? AND pattern_id = ?",
                    (
                        LIFECYCLE_REACTIVATED, now, new_epoch,
                        instance_id, pattern_id,
                    ),
                )
                triggered_reactivation = True
                reactivation_event = {
                    "instance_id": instance_id,
                    "pattern_id": pattern_id,
                    "reactivated_at": now,
                    "recurrence_count": count,
                }
            return triggered_reactivation

        result = await self._run_in_immediate_txn(_do)

        # Emit events AFTER the transaction commits.
        # Codex post-impl Finding M4: recurrence/reactivation events
        # must reach event_stream even when the caller doesn't inject
        # an emit_event closure. Fall back to the module-level emitter
        # so the events don't silently disappear in code paths that
        # construct the store directly (tests, scripts, lazy wiring).
        if recurrence_event is not None:
            await self._emit_lifecycle_event(
                instance_id, "friction.pattern_recurrence",
                recurrence_event, emit_event,
            )
        if reactivation_event is not None:
            await self._emit_lifecycle_event(
                instance_id, "friction.pattern_reactivated",
                reactivation_event, emit_event,
            )
        return result

    @staticmethod
    async def _emit_lifecycle_event(
        instance_id: str,
        event_type: str,
        payload: dict,
        emit_event,
    ) -> None:
        """Emit a friction.pattern_* lifecycle event. Uses the injected
        ``emit_event`` callable when supplied; otherwise falls back to
        the module-level ``event_stream.emit`` so events never silently
        disappear.
        """
        if emit_event is not None:
            try:
                await emit_event(event_type, payload)
                return
            except Exception as exc:
                logger.warning(
                    "FRICTION_PATTERNS: %s emit via callable failed: %s",
                    event_type, exc,
                )
        try:
            from kernos.kernel import event_stream
            await event_stream.emit(instance_id, event_type, payload)
        except Exception as exc:
            logger.debug(
                "FRICTION_PATTERNS: %s emit via event_stream failed: %s",
                event_type, exc,
            )

    @staticmethod
    def _compute_window_cutoff(window_days: int) -> Any:
        """Return ISO timestamp ``window_days`` before now (UTC)."""
        from datetime import datetime, timedelta, timezone
        return datetime.now(timezone.utc) - timedelta(days=window_days)

    # --- Frequency queries --------------------------------------------

    async def query_frequency(
        self,
        instance_id: str,
        pattern_id: str,
        *,
        window_start: str,
        window_end: str,
        include_recurrences: bool = False,
        exclude_backfill: bool = False,
    ) -> int:
        sql = (
            "SELECT COUNT(*) FROM friction_pattern_occurrence "
            "WHERE instance_id = ? AND pattern_id = ? "
            "AND observed_at >= ? AND observed_at < ?"
        )
        params: list[Any] = [instance_id, pattern_id, window_start, window_end]
        if not include_recurrences:
            sql += " AND is_recurrence = 0"
        if exclude_backfill:
            sql += " AND classified_by != ?"
            params.append(CLASSIFIED_BACKFILL)
        async with self.db.execute(sql, tuple(params)) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def query_top_patterns(
        self,
        instance_id: str,
        *,
        window_start: str,
        window_end: str,
        limit: int = 10,
    ) -> list[tuple[FrictionPattern, int]]:
        async with self.db.execute(
            "SELECT pattern_id, COUNT(*) AS cnt "
            "FROM friction_pattern_occurrence "
            "WHERE instance_id = ? AND is_recurrence = 0 "
            "AND observed_at >= ? AND observed_at < ? "
            "GROUP BY pattern_id ORDER BY cnt DESC LIMIT ?",
            (instance_id, window_start, window_end, limit),
        ) as cur:
            counts = await cur.fetchall()
        out: list[tuple[FrictionPattern, int]] = []
        for row in counts:
            pattern = await self.get_pattern(instance_id, row["pattern_id"])
            if pattern is not None:
                out.append((pattern, int(row["cnt"])))
        return out

    # --- Internal helpers ---------------------------------------------

    async def _require_pattern(
        self,
        db: aiosqlite.Connection,
        instance_id: str,
        pattern_id: str,
    ) -> FrictionPattern:
        async with db.execute(
            "SELECT * FROM friction_pattern "
            "WHERE instance_id = ? AND pattern_id = ?",
            (instance_id, pattern_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise UnknownPattern(
                f"no pattern {pattern_id!r} in instance {instance_id!r}"
            )
        return _row_to_pattern(row)

    async def _load(
        self,
        db: aiosqlite.Connection,
        instance_id: str,
        pattern_id: str,
    ) -> FrictionPattern:
        return await self._require_pattern(db, instance_id, pattern_id)


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def classify_signal(
    *,
    signal_type: str,
    signal_description: str,
    candidates: list[FrictionPattern],
) -> tuple[FrictionPattern, float, str] | None:
    """Run Path A (signal_type exact) then Path B (token-overlap) over
    the candidate patterns. Returns ``(pattern, score, match_path)`` or
    None.

    ``match_path`` is ``'signal-type'`` or ``'token-overlap'``. Callers
    select ``classified_by`` from this value via
    ``classified_by_for_match_path``.

    Path A is deterministic (score 1.0); not subject to threshold.
    Path B is subject to ``KERNOS_FRICTION_TOKEN_OVERLAP_THRESHOLD``.
    Path A wins over Path B on multi-match. Among Path A candidates,
    ``signal_type_keys`` uniqueness invariant guarantees exactly one.
    Among Path B candidates, highest score wins; ties broken by
    ``created_at`` (older wins).
    """
    if not candidates:
        return None

    # Path A.
    if signal_type:
        for pattern in candidates:
            if pattern.lifecycle_state not in (
                LIFECYCLE_ACTIVE, LIFECYCLE_RESOLVED, LIFECYCLE_REACTIVATED,
            ):
                continue
            if signal_type in (pattern.signal_type_keys or ()):
                return pattern, 1.0, "signal-type"

    # Path B.
    threshold = _token_overlap_threshold()
    best: tuple[FrictionPattern, float, str] | None = None
    for pattern in candidates:
        if pattern.lifecycle_state not in (
            LIFECYCLE_ACTIVE, LIFECYCLE_RESOLVED, LIFECYCLE_REACTIVATED,
        ):
            continue
        score = _normalized_score_path_b(
            signal_description, pattern.description,
        )
        if score < threshold:
            continue
        if (
            best is None
            or score > best[1]
            or (score == best[1] and pattern.created_at < best[0].created_at)
        ):
            best = (pattern, score, "token-overlap")
    return best


def classified_by_for_match_path(match_path: str) -> str:
    """Map ``classify_signal`` result's ``match_path`` to the
    ``classified_by`` vocabulary value."""
    if match_path == "signal-type":
        return CLASSIFIED_AUTO_SIGNAL_TYPE
    if match_path == "token-overlap":
        return CLASSIFIED_AUTO_TOKEN_OVERLAP
    raise ValueError(f"unknown match_path {match_path!r}")


__all__ = [
    # Constants
    "LIFECYCLE_ACTIVE",
    "LIFECYCLE_RESOLVED",
    "LIFECYCLE_REACTIVATED",
    "LIFECYCLE_ARCHIVED",
    "VALID_LIFECYCLE_STATES",
    "CLASSIFIED_AUTO_SIGNAL_TYPE",
    "CLASSIFIED_AUTO_TOKEN_OVERLAP",
    "CLASSIFIED_MANUAL",
    "CLASSIFIED_BACKFILL",
    "VALID_CLASSIFIED_BY",
    # Errors
    "FrictionPatternStoreError",
    "UnknownPattern",
    "SignalTypeKeyCollision",
    "AliasCollision",
    "PatternArchived",
    "InvalidLifecycleTransition",
    "StoreContention",
    # Dataclass + store
    "FrictionPattern",
    "FrictionPatternStore",
    # Classifier
    "classify_signal",
    "classified_by_for_match_path",
    # Helpers
    "slugify",
    "parse_spec_pattern_refs",
]

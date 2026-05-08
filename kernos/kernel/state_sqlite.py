"""SQLite State Store — production backend for Kernos state.

Replaces JsonStateStore with SQLite + WAL mode for concurrent readers,
proper indexing, and future multi-instance support. Implements the same
StateStore ABC — all consumers are unaffected.

One database per-instance: data/{instance_id}/kernos.db
"""
import json
import logging
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from kernos.kernel.state import (
    CovenantRule,
    ConversationSummary,
    EntityNode,
    IdentityEdge,
    KnowledgeEntry,
    PendingAction,
    Preference,
    Soul,
    StateStore,
    InstanceProfile,
)
from kernos.kernel.spaces import ContextSpace
from kernos.utils import utc_now, _safe_name

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS soul (
    instance_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instance_profile (
    instance_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge (
    id TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    content TEXT NOT NULL,
    subject TEXT DEFAULT '',
    category TEXT DEFAULT '',
    lifecycle_archetype TEXT DEFAULT 'structural',
    storage_strength REAL DEFAULT 1.0,
    reinforcement_count INTEGER DEFAULT 1,
    last_reinforced_at TEXT DEFAULT '',
    salience REAL DEFAULT 0.5,
    foresight_signal TEXT DEFAULT '',
    foresight_expires TEXT DEFAULT '',
    content_hash TEXT DEFAULT '',
    space_id TEXT DEFAULT '',
    member_id TEXT DEFAULT '',
    visibility TEXT DEFAULT 'open',
    source_type TEXT DEFAULT '',
    expired_at TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    data TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_tenant ON knowledge(instance_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_subject ON knowledge(instance_id, subject);
CREATE INDEX IF NOT EXISTS idx_knowledge_archetype ON knowledge(instance_id, lifecycle_archetype);
CREATE INDEX IF NOT EXISTS idx_knowledge_foresight ON knowledge(instance_id, foresight_expires)
    WHERE foresight_signal != '';
CREATE INDEX IF NOT EXISTS idx_knowledge_hash ON knowledge(instance_id, content_hash)
    WHERE content_hash != '';
CREATE INDEX IF NOT EXISTS idx_knowledge_active ON knowledge(instance_id, active)
    WHERE active = 1;

CREATE TABLE IF NOT EXISTS covenants (
    id TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    capability TEXT DEFAULT 'general',
    rule_type TEXT NOT NULL,
    description TEXT NOT NULL,
    active INTEGER DEFAULT 1,
    source TEXT DEFAULT 'default',
    layer TEXT DEFAULT 'principle',
    enforcement_tier TEXT DEFAULT '',
    tier TEXT DEFAULT '',
    context_space TEXT DEFAULT '',
    member_id TEXT DEFAULT '',
    superseded_by TEXT DEFAULT '',
    data TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_covenants_tenant ON covenants(instance_id);
CREATE INDEX IF NOT EXISTS idx_covenants_active ON covenants(instance_id, active) WHERE active = 1;

CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    entity_type TEXT DEFAULT '',
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entities_tenant ON entities(instance_id);

CREATE TABLE IF NOT EXISTS identity_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    edge_type TEXT DEFAULT '',
    confidence REAL DEFAULT 1.0,
    data TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edges_tenant ON identity_edges(instance_id);

CREATE TABLE IF NOT EXISTS context_spaces (
    id TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    name TEXT NOT NULL,
    parent_id TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    space_type TEXT DEFAULT 'general',
    posture TEXT DEFAULT '',
    is_default INTEGER DEFAULT 0,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spaces_tenant ON context_spaces(instance_id);

CREATE TABLE IF NOT EXISTS preferences (
    id TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    category TEXT NOT NULL,
    subject TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prefs_tenant ON preferences(instance_id);

CREATE TABLE IF NOT EXISTS pending_actions (
    id TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actions_tenant ON pending_actions(instance_id);

CREATE TABLE IF NOT EXISTS whispers (
    id TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    insight_text TEXT NOT NULL,
    delivery_class TEXT DEFAULT 'ambient',
    source_space_id TEXT DEFAULT '',
    target_space_id TEXT DEFAULT '',
    foresight_signal TEXT DEFAULT '',
    knowledge_entry_id TEXT DEFAULT '',
    surfaced_at TEXT DEFAULT '',
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_whispers_tenant ON whispers(instance_id);
CREATE INDEX IF NOT EXISTS idx_whispers_pending ON whispers(instance_id, surfaced_at)
    WHERE surfaced_at = '';

CREATE TABLE IF NOT EXISTS suppressions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT NOT NULL,
    whisper_id TEXT NOT NULL,
    foresight_signal TEXT DEFAULT '',
    resolution_state TEXT DEFAULT 'surfaced',
    data TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    resolved_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_suppressions_tenant ON suppressions(instance_id);

CREATE TABLE IF NOT EXISTS space_notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT NOT NULL,
    space_id TEXT NOT NULL,
    text TEXT NOT NULL,
    source TEXT DEFAULT '',
    notice_type TEXT DEFAULT 'cross_domain',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notices_space ON space_notices(instance_id, space_id);

CREATE TABLE IF NOT EXISTS conversation_summaries (
    instance_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    platform TEXT DEFAULT '',
    message_count INTEGER DEFAULT 0,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (instance_id, conversation_id)
);
CREATE INDEX IF NOT EXISTS idx_convs_tenant ON conversation_summaries(instance_id);

CREATE TABLE IF NOT EXISTS triggers (
    id TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    action_description TEXT NOT NULL,
    action_type TEXT DEFAULT 'notify',
    status TEXT DEFAULT 'active',
    recurrence TEXT DEFAULT '',
    next_fire_at TEXT DEFAULT '',
    source TEXT DEFAULT '',
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_triggers_tenant ON triggers(instance_id);
CREATE INDEX IF NOT EXISTS idx_triggers_active ON triggers(instance_id, status, next_fire_at)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS relational_messages (
    id                      TEXT NOT NULL,
    instance_id             TEXT NOT NULL,
    origin_member_id        TEXT NOT NULL,
    origin_agent_identity   TEXT DEFAULT '',
    addressee_member_id     TEXT NOT NULL,
    intent                  TEXT NOT NULL,
    content                 TEXT NOT NULL,
    urgency                 TEXT NOT NULL DEFAULT 'normal',
    conversation_id         TEXT NOT NULL,
    state                   TEXT NOT NULL DEFAULT 'pending',
    created_at              TEXT NOT NULL,
    target_space_hint       TEXT DEFAULT '',
    delivered_at            TEXT DEFAULT '',
    surfaced_at             TEXT DEFAULT '',
    resolved_at             TEXT DEFAULT '',
    expired_at              TEXT DEFAULT '',
    resolution_reason       TEXT DEFAULT '',
    reply_to_id             TEXT DEFAULT '',
    envelope_type           TEXT DEFAULT 'message',
    parcel_id               TEXT DEFAULT '',
    canvas_id               TEXT DEFAULT '',
    PRIMARY KEY (instance_id, id)
);
CREATE INDEX IF NOT EXISTS idx_rm_addressee
    ON relational_messages(instance_id, addressee_member_id, state);
CREATE INDEX IF NOT EXISTS idx_rm_conversation
    ON relational_messages(instance_id, conversation_id);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_json(obj: Any) -> str:
    """Serialize a dataclass or dict to JSON string."""
    if hasattr(obj, '__dataclass_fields__'):
        return json.dumps(asdict(obj), ensure_ascii=False)
    return json.dumps(obj, ensure_ascii=False)


def _from_json(text: str, default: Any = None) -> Any:
    """Parse JSON string, returning default on failure."""
    if not text:
        return default or {}
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return default or {}


def _row_to_dict(row: aiosqlite.Row) -> dict:
    """Convert a sqlite Row to a plain dict."""
    return dict(row)


def _build_dataclass(cls, data: dict):
    """Build a dataclass from a dict, ignoring unknown fields."""
    valid = {f.name for f in fields(cls)}
    filtered = {k: v for k, v in data.items() if k in valid}
    return cls(**filtered)


def _merge_row_and_data(row_dict: dict, data_json: str, exclude: set | None = None) -> dict:
    """Merge structured columns with overflow JSON blob."""
    result = dict(row_dict)
    overflow = _from_json(data_json, {})
    exclude = exclude or {"data", "id"}
    for k, v in overflow.items():
        if k not in result or k in exclude:
            result[k] = v
    # Remove the raw data column
    result.pop("data", None)
    return result


# ---------------------------------------------------------------------------
# SqliteStateStore
# ---------------------------------------------------------------------------

class SqliteStateStore(StateStore):
    """SQLite-backed state store with WAL mode for concurrent access."""

    def __init__(self, data_dir: str) -> None:
        self._data_dir = Path(data_dir)
        self._connections: dict[str, aiosqlite.Connection] = {}

    async def _db(self, instance_id: str) -> aiosqlite.Connection:
        """Get or create a connection for an instance."""
        if instance_id not in self._connections:
            db_path = self._data_dir / _safe_name(instance_id) / "kernos.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = await aiosqlite.connect(str(db_path))
            conn.row_factory = aiosqlite.Row
            # Execute schema (IF NOT EXISTS makes this idempotent)
            for statement in _SCHEMA.split(";"):
                stmt = statement.strip()
                if stmt:
                    await conn.execute(stmt)
            # Migrations: add columns that may be missing from older databases
            for _alt in [
                "ALTER TABLE covenants ADD COLUMN member_id TEXT DEFAULT ''",
                "ALTER TABLE relational_messages ADD COLUMN envelope_type TEXT DEFAULT 'message'",
                "ALTER TABLE relational_messages ADD COLUMN parcel_id TEXT DEFAULT ''",
                "ALTER TABLE relational_messages ADD COLUMN canvas_id TEXT DEFAULT ''",
            ]:
                try:
                    await conn.execute(_alt)
                except Exception:
                    pass  # Column already exists
            await conn.commit()
            self._connections[instance_id] = conn
        return self._connections[instance_id]

    async def close_all(self) -> None:
        """Close all database connections."""
        for conn in self._connections.values():
            await conn.close()
        self._connections.clear()

    # --- Overflow JSON helpers ---

    def _pack_overflow(self, obj: Any, columns: set[str]) -> str:
        """Extract fields NOT in columns into a JSON overflow blob."""
        if hasattr(obj, '__dataclass_fields__'):
            d = asdict(obj)
        elif isinstance(obj, dict):
            d = obj
        else:
            return "{}"
        overflow = {k: v for k, v in d.items() if k not in columns}
        return json.dumps(overflow, ensure_ascii=False)

    # ===================================================================
    # Soul
    # ===================================================================

    async def get_soul(self, instance_id: str) -> Soul | None:
        db = await self._db(instance_id)
        async with db.execute("SELECT data FROM soul WHERE instance_id=?", (instance_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return _build_dataclass(Soul, _from_json(row["data"]))

    async def save_soul(self, soul: Soul, *, source: str = "", trigger: str = "") -> None:
        now = utc_now()
        db = await self._db(soul.instance_id)
        data = _to_json(soul)
        await db.execute(
            "INSERT INTO soul (instance_id, data, created_at, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(instance_id) DO UPDATE SET data=?, updated_at=?",
            (soul.instance_id, data, now, now, data, now),
        )
        await db.commit()
        logger.info("SOUL_WRITE: source=%s trigger=%s instance=%s", source, trigger, soul.instance_id)

    # ===================================================================
    # Tenant Profile
    # ===================================================================

    async def get_instance_profile(self, instance_id: str) -> InstanceProfile | None:
        db = await self._db(instance_id)
        async with db.execute("SELECT data FROM instance_profile WHERE instance_id=?", (instance_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return _build_dataclass(InstanceProfile, _from_json(row["data"]))

    async def save_instance_profile(self, instance_id: str, profile: InstanceProfile) -> None:
        now = utc_now()
        db = await self._db(instance_id)
        data = _to_json(profile)
        await db.execute(
            "INSERT INTO instance_profile (instance_id, data, created_at, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(instance_id) DO UPDATE SET data=?, updated_at=?",
            (instance_id, data, now, now, data, now),
        )
        await db.commit()

    # ===================================================================
    # Knowledge
    # ===================================================================

    _KNOWLEDGE_COLS = {
        "id", "instance_id", "content", "subject", "category", "lifecycle_archetype",
        "storage_strength", "reinforcement_count", "last_reinforced_at", "salience",
        "foresight_signal", "foresight_expires", "content_hash", "space_id",
        "member_id", "visibility", "source_type", "expired_at", "active",
        "created_at", "updated_at",
    }

    def _ke_to_row(self, e: KnowledgeEntry) -> tuple:
        d = asdict(e)
        active = 1 if not d.get("expired_at") else 0
        overflow = self._pack_overflow(e, self._KNOWLEDGE_COLS)
        return (
            d["id"], d["instance_id"], d.get("content", ""), d.get("subject", ""),
            d.get("category", ""), d.get("lifecycle_archetype", "structural"),
            d.get("storage_strength", 1.0), d.get("reinforcement_count", 1),
            d.get("last_reinforced_at", ""), d.get("salience", 0.5),
            d.get("foresight_signal", ""), d.get("foresight_expires", ""),
            d.get("content_hash", ""), d.get("space_id", ""),
            d.get("member_id", ""), d.get("visibility", "open"),
            d.get("source_type", ""), d.get("expired_at", ""),
            active, overflow, d.get("created_at", ""), d.get("updated_at", ""),
        )

    def _row_to_ke(self, row: aiosqlite.Row) -> KnowledgeEntry:
        d = dict(row)
        overflow = _from_json(d.pop("data", "{}"), {})
        d.pop("active", None)
        merged = {**overflow, **{k: v for k, v in d.items() if v is not None}}
        return _build_dataclass(KnowledgeEntry, merged)

    async def add_knowledge(self, entry: KnowledgeEntry) -> None:
        db = await self._db(entry.instance_id)
        vals = self._ke_to_row(entry)
        await db.execute(
            "INSERT OR REPLACE INTO knowledge "
            "(id, instance_id, content, subject, category, lifecycle_archetype, "
            "storage_strength, reinforcement_count, last_reinforced_at, salience, "
            "foresight_signal, foresight_expires, content_hash, space_id, "
            "member_id, visibility, source_type, expired_at, active, data, "
            "created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            vals,
        )
        await db.commit()

    async def query_knowledge(
        self, instance_id: str, subject: str | None = None,
        category: str | None = None, tags: list[str] | None = None,
        active_only: bool = True, limit: int = 20,
        member_id: str = "",
    ) -> list[KnowledgeEntry]:
        db = await self._db(instance_id)
        clauses = ["instance_id=?"]
        params: list = [instance_id]
        if subject:
            clauses.append("subject=?")
            params.append(subject)
        if category:
            clauses.append("category=?")
            params.append(category)
        if active_only:
            clauses.append("active=1")
        # Member visibility: own entries + unowned (legacy/instance-level)
        if member_id:
            clauses.append("(member_id=? OR member_id='' OR member_id IS NULL)")
            params.append(member_id)
        sql = f"SELECT * FROM knowledge WHERE {' AND '.join(clauses)} LIMIT ?"
        params.append(limit)
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [self._row_to_ke(r) for r in rows]

    async def update_knowledge(self, instance_id: str, entry_id: str, updates: dict) -> None:
        db = await self._db(instance_id)
        # Read current, apply updates, write back
        async with db.execute("SELECT * FROM knowledge WHERE id=? AND instance_id=?",
                              (entry_id, instance_id)) as cur:
            row = await cur.fetchone()
        if not row:
            return
        d = dict(row)
        overflow = _from_json(d.get("data", "{}"), {})
        now = utc_now()
        for k, v in updates.items():
            if k in self._KNOWLEDGE_COLS:
                d[k] = v
            else:
                overflow[k] = v
        d["data"] = json.dumps(overflow, ensure_ascii=False)
        d["updated_at"] = now
        if d.get("expired_at"):
            d["active"] = 0
        cols = list(d.keys())
        vals = [d[c] for c in cols]
        set_clause = ", ".join(f"{c}=?" for c in cols)
        await db.execute(f"UPDATE knowledge SET {set_clause} WHERE id=? AND instance_id=?",
                         vals + [entry_id, instance_id])
        await db.commit()

    async def save_knowledge_entry(self, entry: KnowledgeEntry) -> None:
        await self.add_knowledge(entry)  # INSERT OR REPLACE

    async def get_knowledge_entry(self, instance_id: str, entry_id: str) -> KnowledgeEntry | None:
        db = await self._db(instance_id)
        async with db.execute("SELECT * FROM knowledge WHERE id=? AND instance_id=?",
                              (entry_id, instance_id)) as cur:
            row = await cur.fetchone()
        return self._row_to_ke(row) if row else None

    async def get_knowledge_hashes(self, instance_id: str) -> set[str]:
        db = await self._db(instance_id)
        async with db.execute(
            "SELECT content_hash FROM knowledge WHERE instance_id=? AND active=1 AND content_hash!=''",
            (instance_id,),
        ) as cur:
            rows = await cur.fetchall()
        return {r["content_hash"] for r in rows}

    async def get_knowledge_by_hash(self, instance_id: str, content_hash: str) -> KnowledgeEntry | None:
        db = await self._db(instance_id)
        async with db.execute(
            "SELECT * FROM knowledge WHERE instance_id=? AND content_hash=? AND active=1",
            (instance_id, content_hash),
        ) as cur:
            row = await cur.fetchone()
        return self._row_to_ke(row) if row else None

    async def query_knowledge_by_foresight(
        self, instance_id: str, expires_before: str,
        expires_after: str = "", space_id: str = "",
    ) -> list[KnowledgeEntry]:
        db = await self._db(instance_id)
        clauses = [
            "instance_id=?", "active=1",
            "foresight_signal!=''", "foresight_expires!=''",
            "foresight_expires<=?",
        ]
        params: list = [instance_id, expires_before]
        if expires_after:
            clauses.append("foresight_expires>?")
            params.append(expires_after)
        if space_id:
            clauses.append("(space_id=? OR space_id='')")
            params.append(space_id)
        sql = f"SELECT * FROM knowledge WHERE {' AND '.join(clauses)}"
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [self._row_to_ke(r) for r in rows]

    # ===================================================================
    # Covenant Rules
    # ===================================================================

    def _cov_to_row(self, r: CovenantRule) -> tuple:
        d = asdict(r)
        # member_id is a first-class column (codex review 2026-05-08:
        # was previously routed through the JSON overflow blob, but the
        # read-side merge order made the column default '' override the
        # overflow value, so reloaded rules always lost their
        # member_id and became instance-level. Including member_id in
        # the structured column set fixes the round-trip).
        _cols = {"id", "instance_id", "capability", "rule_type", "description",
                 "active", "source", "layer", "enforcement_tier", "tier",
                 "context_space", "member_id", "superseded_by",
                 "created_at", "updated_at"}
        overflow = {k: v for k, v in d.items() if k not in _cols}
        return (
            d["id"], d["instance_id"], d.get("capability", "general"),
            d["rule_type"], d["description"], 1 if d.get("active") else 0,
            d.get("source", "default"), d.get("layer", "principle"),
            d.get("enforcement_tier", ""), d.get("tier", ""),
            d.get("context_space") or "",
            d.get("member_id", "") or "",
            d.get("superseded_by", ""),
            json.dumps(overflow, ensure_ascii=False),
            d.get("created_at", ""), d.get("updated_at", ""),
        )

    def _row_to_cov(self, row: aiosqlite.Row) -> CovenantRule:
        d = dict(row)
        overflow = _from_json(d.pop("data", "{}"), {})
        d["active"] = bool(d.get("active", 1))
        if d.get("context_space") == "":
            d["context_space"] = None
        merged = {**overflow, **{k: v for k, v in d.items() if v is not None}}
        return _build_dataclass(CovenantRule, merged)

    async def get_contract_rules(
        self, instance_id: str, capability: str | None = None,
        rule_type: str | None = None, active_only: bool = True,
    ) -> list[CovenantRule]:
        db = await self._db(instance_id)
        clauses = ["instance_id=?"]
        params: list = [instance_id]
        if capability:
            clauses.append("capability=?")
            params.append(capability)
        if rule_type:
            clauses.append("rule_type=?")
            params.append(rule_type)
        if active_only:
            clauses.append("active=1")
            clauses.append("(superseded_by='' OR superseded_by IS NULL)")
        sql = f"SELECT * FROM covenants WHERE {' AND '.join(clauses)}"
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [self._row_to_cov(r) for r in rows]

    async def query_covenant_rules(
        self, instance_id: str, capability: str | None = None,
        context_space_scope: list[str | None] | None = None,
        active_only: bool = True,
        member_id: str = "",
    ) -> list[CovenantRule]:
        rules = await self.get_contract_rules(instance_id, capability, active_only=active_only)
        if context_space_scope is not None:
            scope_set = set()
            for s in context_space_scope:
                scope_set.add(s if s is not None else "")
            if None in context_space_scope:
                scope_set.add("")
                scope_set.add(None)
            rules = [r for r in rules if (r.context_space or "") in scope_set or r.context_space in scope_set]
        # Member scoping: show instance-level + this member's rules
        if member_id:
            rules = [r for r in rules if not getattr(r, "member_id", "") or getattr(r, "member_id", "") == member_id]
        return rules

    async def add_contract_rule(self, rule: CovenantRule) -> None:
        db = await self._db(rule.instance_id)
        vals = self._cov_to_row(rule)
        await db.execute(
            "INSERT OR REPLACE INTO covenants "
            "(id, instance_id, capability, rule_type, description, active, source, "
            "layer, enforcement_tier, tier, context_space, member_id, superseded_by, "
            "data, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            vals,
        )
        await db.commit()

    async def update_contract_rule(self, instance_id: str, rule_id: str, updates: dict) -> None:
        db = await self._db(instance_id)
        async with db.execute("SELECT * FROM covenants WHERE id=? AND instance_id=?",
                              (rule_id, instance_id)) as cur:
            row = await cur.fetchone()
        if not row:
            return
        d = dict(row)
        overflow = _from_json(d.get("data", "{}"), {})
        _cols = {"id", "instance_id", "capability", "rule_type", "description",
                 "active", "source", "layer", "enforcement_tier", "tier",
                 "context_space", "member_id", "superseded_by",
                 "created_at", "updated_at"}
        for k, v in updates.items():
            if k in _cols:
                d[k] = v
            else:
                overflow[k] = v
        if "active" in updates:
            d["active"] = 1 if updates["active"] else 0
        d["data"] = json.dumps(overflow, ensure_ascii=False)
        d["updated_at"] = utc_now()
        cols = list(d.keys())
        vals = [d[c] for c in cols]
        set_clause = ", ".join(f"{c}=?" for c in cols)
        await db.execute(f"UPDATE covenants SET {set_clause} WHERE id=? AND instance_id=?",
                         vals + [rule_id, instance_id])
        await db.commit()

    # ===================================================================
    # Entity Resolution
    # ===================================================================

    async def save_entity_node(self, node: EntityNode) -> None:
        now = utc_now()
        db = await self._db(node.instance_id)
        data = _to_json(node)
        await db.execute(
            "INSERT OR REPLACE INTO entities "
            "(id, instance_id, canonical_name, entity_type, data, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (node.id, node.instance_id, node.canonical_name,
             getattr(node, "entity_type", ""), data, now, now),
        )
        await db.commit()

    async def get_entity_node(self, instance_id: str, entity_id: str) -> EntityNode | None:
        db = await self._db(instance_id)
        async with db.execute("SELECT data FROM entities WHERE id=? AND instance_id=?",
                              (entity_id, instance_id)) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return _build_dataclass(EntityNode, _from_json(row["data"]))

    async def query_entity_nodes(
        self, instance_id: str, name: str | None = None,
        entity_type: str | None = None, active_only: bool = True,
    ) -> list[EntityNode]:
        db = await self._db(instance_id)
        clauses = ["instance_id=?"]
        params: list = [instance_id]
        if name:
            clauses.append("canonical_name=?")
            params.append(name)
        if entity_type:
            clauses.append("entity_type=?")
            params.append(entity_type)
        sql = f"SELECT data FROM entities WHERE {' AND '.join(clauses)}"
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_build_dataclass(EntityNode, _from_json(r["data"])) for r in rows]

    async def save_identity_edge(self, instance_id: str, edge: IdentityEdge) -> None:
        db = await self._db(instance_id)
        data = _to_json(edge)
        await db.execute(
            "INSERT INTO identity_edges "
            "(instance_id, source_id, target_id, edge_type, confidence, data, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (instance_id, edge.source_entity_id, edge.target_entity_id,
             getattr(edge, "edge_type", ""), getattr(edge, "confidence", 1.0),
             data, utc_now()),
        )
        await db.commit()

    async def query_identity_edges(self, instance_id: str, entity_id: str) -> list[IdentityEdge]:
        db = await self._db(instance_id)
        async with db.execute(
            "SELECT data FROM identity_edges WHERE instance_id=? AND (source_id=? OR target_id=?)",
            (instance_id, entity_id, entity_id),
        ) as cur:
            rows = await cur.fetchall()
        return [_build_dataclass(IdentityEdge, _from_json(r["data"])) for r in rows]

    # ===================================================================
    # Pending Actions
    # ===================================================================

    async def save_pending_action(self, action: PendingAction) -> None:
        now = utc_now()
        db = await self._db(action.instance_id)
        data = _to_json(action)
        await db.execute(
            "INSERT OR REPLACE INTO pending_actions "
            "(id, instance_id, status, data, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (action.id, action.instance_id, getattr(action, "status", "pending"),
             data, now, now),
        )
        await db.commit()

    async def get_pending_actions(self, instance_id: str, status: str = "pending") -> list[PendingAction]:
        db = await self._db(instance_id)
        async with db.execute(
            "SELECT data FROM pending_actions WHERE instance_id=? AND status=?",
            (instance_id, status),
        ) as cur:
            rows = await cur.fetchall()
        return [_build_dataclass(PendingAction, _from_json(r["data"])) for r in rows]

    async def update_pending_action(self, instance_id: str, action_id: str, updates: dict) -> None:
        db = await self._db(instance_id)
        async with db.execute("SELECT data FROM pending_actions WHERE id=? AND instance_id=?",
                              (action_id, instance_id)) as cur:
            row = await cur.fetchone()
        if not row:
            return
        d = _from_json(row["data"])
        d.update(updates)
        now = utc_now()
        await db.execute(
            "UPDATE pending_actions SET data=?, status=?, updated_at=? WHERE id=? AND instance_id=?",
            (json.dumps(d, ensure_ascii=False), d.get("status", "pending"), now, action_id, instance_id),
        )
        await db.commit()

    # ===================================================================
    # Context Spaces
    # ===================================================================

    async def save_context_space(self, space: ContextSpace) -> None:
        now = utc_now()
        db = await self._db(space.instance_id)
        data = _to_json(space)
        await db.execute(
            "INSERT OR REPLACE INTO context_spaces "
            "(id, instance_id, name, parent_id, status, space_type, posture, "
            "is_default, data, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (space.id, space.instance_id, space.name, space.parent_id or "",
             space.status, space.space_type, space.posture,
             1 if space.is_default else 0, data, now, now),
        )
        await db.commit()

    async def get_context_space(self, instance_id: str, space_id: str) -> ContextSpace | None:
        db = await self._db(instance_id)
        async with db.execute("SELECT data FROM context_spaces WHERE id=? AND instance_id=?",
                              (space_id, instance_id)) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return _build_dataclass(ContextSpace, _from_json(row["data"]))

    async def list_context_spaces(self, instance_id: str) -> list[ContextSpace]:
        db = await self._db(instance_id)
        async with db.execute("SELECT data FROM context_spaces WHERE instance_id=?",
                              (instance_id,)) as cur:
            rows = await cur.fetchall()
        return [_build_dataclass(ContextSpace, _from_json(r["data"])) for r in rows]

    async def update_context_space(self, instance_id: str, space_id: str, updates: dict) -> None:
        db = await self._db(instance_id)
        async with db.execute("SELECT data FROM context_spaces WHERE id=? AND instance_id=?",
                              (space_id, instance_id)) as cur:
            row = await cur.fetchone()
        if not row:
            return
        d = _from_json(row["data"])
        d.update(updates)
        d["updated_at"] = utc_now()
        data_str = json.dumps(d, ensure_ascii=False)
        await db.execute(
            "UPDATE context_spaces SET name=?, parent_id=?, status=?, space_type=?, "
            "posture=?, is_default=?, data=?, updated_at=? WHERE id=? AND instance_id=?",
            (d.get("name", ""), d.get("parent_id", ""), d.get("status", "active"),
             d.get("space_type", "general"), d.get("posture", ""),
             1 if d.get("is_default") else 0, data_str, d["updated_at"],
             space_id, instance_id),
        )
        await db.commit()

    # ===================================================================
    # Space Notices
    # ===================================================================

    async def append_space_notice(
        self, instance_id: str, space_id: str, text: str,
        source: str = "", notice_type: str = "cross_domain",
    ) -> None:
        db = await self._db(instance_id)
        await db.execute(
            "INSERT INTO space_notices (instance_id, space_id, text, source, notice_type, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (instance_id, space_id, text, source, notice_type, utc_now()),
        )
        await db.commit()

    async def drain_space_notices(self, instance_id: str, space_id: str) -> list[dict]:
        db = await self._db(instance_id)
        async with db.execute(
            "SELECT text, source, notice_type, created_at FROM space_notices "
            "WHERE instance_id=? AND space_id=?",
            (instance_id, space_id),
        ) as cur:
            rows = await cur.fetchall()
        if not rows:
            return []
        notices = [dict(r) for r in rows]
        await db.execute("DELETE FROM space_notices WHERE instance_id=? AND space_id=?",
                         (instance_id, space_id))
        await db.commit()
        return notices

    # ===================================================================
    # Whispers and Suppressions
    # ===================================================================

    async def save_whisper(self, instance_id: str, whisper) -> None:
        from kernos.kernel.awareness import Whisper
        from dataclasses import asdict as _asdict
        db = await self._db(instance_id)
        d = _asdict(whisper)
        # Dedup: skip if a pending whisper with the same foresight_signal exists
        _signal = d.get("foresight_signal", "")
        if _signal:
            async with db.execute(
                "SELECT id FROM whispers WHERE instance_id=? AND foresight_signal=? AND surfaced_at=''",
                (instance_id, _signal),
            ) as cur:
                existing = await cur.fetchone()
            if existing:
                logger.info("WHISPER_DEDUP: signal=%r already pending, skipping", _signal[:60])
                return
        data = json.dumps({k: v for k, v in d.items()
                           if k not in ("id", "whisper_id", "instance_id", "insight_text",
                                        "delivery_class", "source_space_id", "target_space_id",
                                        "foresight_signal", "knowledge_entry_id", "surfaced_at",
                                        "created_at")}, ensure_ascii=False)
        await db.execute(
            "INSERT OR REPLACE INTO whispers "
            "(id, instance_id, insight_text, delivery_class, source_space_id, target_space_id, "
            "foresight_signal, knowledge_entry_id, surfaced_at, data, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (d.get("whisper_id", ""), instance_id, d.get("insight_text", ""),
             d.get("delivery_class", "ambient"), d.get("source_space_id", ""),
             d.get("target_space_id", ""), _signal,
             d.get("knowledge_entry_id", ""), d.get("surfaced_at", ""),
             data, d.get("created_at", utc_now())),
        )
        await db.commit()

    async def get_pending_whispers(self, instance_id: str) -> list:
        from kernos.kernel.awareness import Whisper
        db = await self._db(instance_id)
        now = datetime.now(timezone.utc)
        async with db.execute(
            "SELECT * FROM whispers WHERE instance_id=? AND surfaced_at=''",
            (instance_id,),
        ) as cur:
            rows = await cur.fetchall()
        pending = []
        expired_ids = []
        for row in rows:
            d = dict(row)
            created = d.get("created_at", "")
            # 48h expiry
            if created:
                try:
                    created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    if created_dt.tzinfo is None:
                        created_dt = created_dt.replace(tzinfo=timezone.utc)
                    if (now - created_dt).total_seconds() / 3600 > 48:
                        expired_ids.append(d.get("id", ""))
                        logger.info("WHISPER_EXPIRED: id=%s age_hours=%.1f",
                            d.get("id", "?"), (now - created_dt).total_seconds() / 3600)
                        continue
                except (ValueError, TypeError):
                    pass
            overflow = _from_json(d.get("data", "{}"), {})
            whisper_data = {
                "whisper_id": d.get("id", ""),
                "insight_text": d.get("insight_text", ""),
                "delivery_class": d.get("delivery_class", "ambient"),
                "source_space_id": d.get("source_space_id", ""),
                "target_space_id": d.get("target_space_id", ""),
                "foresight_signal": d.get("foresight_signal", ""),
                "knowledge_entry_id": d.get("knowledge_entry_id", ""),
                "surfaced_at": d.get("surfaced_at", ""),
                "created_at": created,
                **overflow,
            }
            pending.append(_build_dataclass(Whisper, whisper_data))
        # Mark expired whispers
        if expired_ids:
            for wid in expired_ids:
                await db.execute(
                    "UPDATE whispers SET surfaced_at=? WHERE id=? AND instance_id=?",
                    (now.isoformat(), wid, instance_id),
                )
            await db.commit()
        return pending

    async def mark_whisper_surfaced(self, instance_id: str, whisper_id: str) -> None:
        db = await self._db(instance_id)
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "UPDATE whispers SET surfaced_at=? WHERE id=? AND instance_id=?",
            (now, whisper_id, instance_id),
        )
        await db.commit()

    async def delete_whisper(self, instance_id: str, whisper_id: str) -> None:
        db = await self._db(instance_id)
        await db.execute("DELETE FROM whispers WHERE id=? AND instance_id=?",
                         (whisper_id, instance_id))
        await db.commit()

    async def save_suppression(self, instance_id: str, entry) -> None:
        from dataclasses import asdict as _asdict
        db = await self._db(instance_id)
        d = _asdict(entry)
        # Upsert by whisper_id
        await db.execute(
            "INSERT INTO suppressions "
            "(instance_id, whisper_id, foresight_signal, resolution_state, data, created_at, resolved_at) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET resolution_state=?, resolved_at=?, data=?",
            (instance_id, d.get("whisper_id", ""), d.get("foresight_signal", ""),
             d.get("resolution_state", "surfaced"), json.dumps(d, ensure_ascii=False),
             d.get("created_at", utc_now()), d.get("resolved_at", ""),
             d.get("resolution_state", "surfaced"), d.get("resolved_at", ""),
             json.dumps(d, ensure_ascii=False)),
        )
        await db.commit()

    async def get_suppressions(
        self, instance_id: str, knowledge_entry_id: str = "",
        whisper_id: str = "", foresight_signal: str = "",
    ) -> list:
        from kernos.kernel.awareness import SuppressionEntry
        db = await self._db(instance_id)
        clauses = ["instance_id=?"]
        params: list = [instance_id]
        if knowledge_entry_id:
            clauses.append("data LIKE ?")
            params.append(f'%"{knowledge_entry_id}"%')
        if whisper_id:
            clauses.append("whisper_id=?")
            params.append(whisper_id)
        if foresight_signal:
            clauses.append("foresight_signal=?")
            params.append(foresight_signal)
        sql = f"SELECT data FROM suppressions WHERE {' AND '.join(clauses)}"
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_build_dataclass(SuppressionEntry, _from_json(r["data"])) for r in rows]

    async def delete_suppression(self, instance_id: str, whisper_id: str) -> None:
        db = await self._db(instance_id)
        await db.execute("DELETE FROM suppressions WHERE instance_id=? AND whisper_id=?",
                         (instance_id, whisper_id))
        await db.commit()

    # ===================================================================
    # Conversation Summaries
    # ===================================================================

    async def get_conversation_summary(
        self, instance_id: str, conversation_id: str,
    ) -> ConversationSummary | None:
        db = await self._db(instance_id)
        async with db.execute(
            "SELECT data FROM conversation_summaries WHERE instance_id=? AND conversation_id=?",
            (instance_id, conversation_id),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return _build_dataclass(ConversationSummary, _from_json(row["data"]))

    async def save_conversation_summary(self, summary: ConversationSummary) -> None:
        now = utc_now()
        db = await self._db(summary.instance_id)
        data = _to_json(summary)
        await db.execute(
            "INSERT INTO conversation_summaries "
            "(instance_id, conversation_id, platform, message_count, data, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(instance_id, conversation_id) DO UPDATE SET "
            "message_count=?, data=?, updated_at=?",
            (summary.instance_id, summary.conversation_id,
             summary.platform, summary.message_count, data, now, now,
             summary.message_count, data, now),
        )
        await db.commit()

    async def list_conversations(
        self, instance_id: str, active_only: bool = True, limit: int = 20,
    ) -> list[ConversationSummary]:
        db = await self._db(instance_id)
        async with db.execute(
            "SELECT data FROM conversation_summaries WHERE instance_id=? "
            "ORDER BY updated_at DESC LIMIT ?",
            (instance_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [_build_dataclass(ConversationSummary, _from_json(r["data"])) for r in rows]

    # ===================================================================
    # Preferences
    # ===================================================================

    async def add_preference(self, pref: Preference) -> None:
        await self.save_preference(pref)

    async def save_preference(self, pref: Preference) -> None:
        now = utc_now()
        db = await self._db(pref.instance_id)
        data = _to_json(pref)
        await db.execute(
            "INSERT OR REPLACE INTO preferences "
            "(id, instance_id, category, subject, status, data, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (pref.id, pref.instance_id, getattr(pref, "category", ""),
             getattr(pref, "subject", ""), getattr(pref, "status", "active"),
             data, now, now),
        )
        await db.commit()

    async def get_preference(self, instance_id: str, pref_id: str) -> Preference | None:
        db = await self._db(instance_id)
        async with db.execute("SELECT data FROM preferences WHERE id=? AND instance_id=?",
                              (pref_id, instance_id)) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return _build_dataclass(Preference, _from_json(row["data"]))

    async def query_preferences(
        self, instance_id: str, status: str = "", subject: str = "",
        category: str = "", scope: str = "", active_only: bool = True,
    ) -> list[Preference]:
        db = await self._db(instance_id)
        clauses = ["instance_id=?"]
        params: list = [instance_id]
        if status:
            clauses.append("status=?")
            params.append(status)
        elif active_only:
            clauses.append("status='active'")
        if subject:
            clauses.append("subject=?")
            params.append(subject)
        if category:
            clauses.append("category=?")
            params.append(category)
        sql = f"SELECT data FROM preferences WHERE {' AND '.join(clauses)}"
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        results = [_build_dataclass(Preference, _from_json(r["data"])) for r in rows]
        if scope:
            results = [p for p in results if getattr(p, "scope", "") == scope]
        return results

    # --- Relational messaging (RELATIONAL-MESSAGING v5) ---

    async def add_relational_message(self, message) -> None:
        db = await self._db(message.instance_id)
        await db.execute(
            "INSERT INTO relational_messages ("
            "id, instance_id, origin_member_id, origin_agent_identity, "
            "addressee_member_id, intent, content, urgency, conversation_id, "
            "state, created_at, target_space_hint, delivered_at, surfaced_at, "
            "resolved_at, expired_at, resolution_reason, reply_to_id, "
            "envelope_type, parcel_id, canvas_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message.id, message.instance_id, message.origin_member_id,
                message.origin_agent_identity, message.addressee_member_id,
                message.intent, message.content, message.urgency,
                message.conversation_id, message.state, message.created_at,
                message.target_space_hint, message.delivered_at,
                message.surfaced_at, message.resolved_at, message.expired_at,
                message.resolution_reason, message.reply_to_id,
                getattr(message, "envelope_type", "message") or "message",
                getattr(message, "parcel_id", "") or "",
                getattr(message, "canvas_id", "") or "",
            ),
        )
        await db.commit()

    @staticmethod
    def _row_to_rm(row):
        from kernos.kernel.relational_messaging import RelationalMessage
        # Older DBs may not have envelope_type / parcel_id columns yet.
        # sqlite3.Row supports ``[key]`` access with KeyError on miss; fall
        # back to defaults so a pre-parcel DB continues to load cleanly.
        try:
            envelope_type = row["envelope_type"] or "message"
        except (IndexError, KeyError):
            envelope_type = "message"
        try:
            parcel_id = row["parcel_id"] or ""
        except (IndexError, KeyError):
            parcel_id = ""
        try:
            canvas_id = row["canvas_id"] or ""
        except (IndexError, KeyError):
            canvas_id = ""
        return RelationalMessage(
            id=row["id"], instance_id=row["instance_id"],
            origin_member_id=row["origin_member_id"],
            origin_agent_identity=row["origin_agent_identity"] or "",
            addressee_member_id=row["addressee_member_id"],
            intent=row["intent"], content=row["content"],
            urgency=row["urgency"], conversation_id=row["conversation_id"],
            state=row["state"], created_at=row["created_at"],
            target_space_hint=row["target_space_hint"] or "",
            delivered_at=row["delivered_at"] or "",
            surfaced_at=row["surfaced_at"] or "",
            resolved_at=row["resolved_at"] or "",
            expired_at=row["expired_at"] or "",
            resolution_reason=row["resolution_reason"] or "",
            reply_to_id=row["reply_to_id"] or "",
            envelope_type=envelope_type,
            parcel_id=parcel_id,
            canvas_id=canvas_id,
        )

    async def get_relational_message(self, instance_id, message_id):
        db = await self._db(instance_id)
        async with db.execute(
            "SELECT * FROM relational_messages WHERE instance_id=? AND id=?",
            (instance_id, message_id),
        ) as cur:
            row = await cur.fetchone()
        return self._row_to_rm(row) if row else None

    async def query_relational_messages(
        self, instance_id, addressee_member_id="", origin_member_id="",
        states=None, conversation_id="", limit=200,
    ):
        db = await self._db(instance_id)
        clauses = ["instance_id=?"]
        params: list = [instance_id]
        if addressee_member_id:
            clauses.append("addressee_member_id=?")
            params.append(addressee_member_id)
        if origin_member_id:
            clauses.append("origin_member_id=?")
            params.append(origin_member_id)
        if conversation_id:
            clauses.append("conversation_id=?")
            params.append(conversation_id)
        if states:
            placeholders = ",".join("?" for _ in states)
            clauses.append(f"state IN ({placeholders})")
            params.extend(states)
        sql = (
            f"SELECT * FROM relational_messages WHERE {' AND '.join(clauses)} "
            f"ORDER BY created_at ASC LIMIT ?"
        )
        params.append(limit)
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [self._row_to_rm(r) for r in rows]

    async def transition_relational_message_state(
        self, instance_id, message_id, from_state, to_state, updates=None,
    ) -> bool:
        """Atomic CAS on state. SQLite UPDATE with state=? WHERE guard +
        rowcount check. Concurrent callers that lose the race get False.
        """
        updates = updates or {}
        # Only whitelisted columns may be updated alongside the state flip.
        _ALLOWED = {
            "delivered_at", "surfaced_at", "resolved_at", "expired_at",
            "resolution_reason",
        }
        extra_cols = [k for k in updates if k in _ALLOWED]
        set_clause = "state=?" + "".join(f", {c}=?" for c in extra_cols)
        params: list = [to_state]
        for c in extra_cols:
            params.append(updates[c])
        params.extend([instance_id, message_id, from_state])

        db = await self._db(instance_id)
        cursor = await db.execute(
            f"UPDATE relational_messages SET {set_clause} "
            f"WHERE instance_id=? AND id=? AND state=?",
            params,
        )
        await db.commit()
        # rowcount is 1 iff the row existed AND its state matched from_state.
        return (cursor.rowcount or 0) == 1

    async def delete_relational_message(self, instance_id, message_id) -> None:
        db = await self._db(instance_id)
        await db.execute(
            "DELETE FROM relational_messages WHERE instance_id=? AND id=?",
            (instance_id, message_id),
        )
        await db.commit()

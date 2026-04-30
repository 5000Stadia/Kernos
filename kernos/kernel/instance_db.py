"""Instance database — shared state across all instances.

data/instance.db — created at startup if it doesn't exist.
Starts nearly empty in V1 (just the owner as a member).
The architectural slot exists for multi-instance without a second migration.

Tables:
  members         — who belongs to this Kernos instance
  member_channels  — phone number / discord ID → member mapping
  message_relay   — cross-member message queue (V2)
  shared_spaces   — instance-level shared space registry (V2)
"""
import json
import logging
import os
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

_INSTANCE_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS members (
    member_id   TEXT PRIMARY KEY,
    display_name TEXT DEFAULT '',
    role        TEXT DEFAULT 'member',  -- 'owner' | 'member' | 'guest'
    instance_id   TEXT DEFAULT '',        -- maps to per-instance DB
    status      TEXT DEFAULT 'active',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS member_channels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id   TEXT NOT NULL,
    platform    TEXT NOT NULL,           -- 'discord' | 'sms' | 'whatsapp'
    channel_id  TEXT NOT NULL,           -- phone number, discord user ID, etc.
    is_primary  INTEGER DEFAULT 1,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (member_id) REFERENCES members(member_id),
    UNIQUE(platform, channel_id)
);
CREATE INDEX IF NOT EXISTS idx_channels_lookup ON member_channels(platform, channel_id);

CREATE TABLE IF NOT EXISTS message_relay (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    from_member TEXT NOT NULL,
    to_member   TEXT NOT NULL,
    content     TEXT NOT NULL,
    status      TEXT DEFAULT 'pending',  -- 'pending' | 'delivered' | 'expired'
    created_at  TEXT NOT NULL,
    delivered_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_relay_pending ON message_relay(to_member, status)
    WHERE status = 'pending';

-- CANVAS-V1: shared_spaces was declared as an instance-level shared-space
-- registry for V2 but never used. CANVAS-V1 repurposes it as the canvas
-- registry. columns added: canvas_id (aliases space_id), scope,
-- owner_member_id, pinned_to_spaces (JSON list), canvas_yaml_path.
-- Old columns (space_id, name, description, status, created_at, updated_at)
-- are preserved. canvas_id is an alias of space_id for new rows.
CREATE TABLE IF NOT EXISTS shared_spaces (
    space_id           TEXT PRIMARY KEY,          -- canvas_id
    name               TEXT NOT NULL,
    description        TEXT DEFAULT '',
    status             TEXT DEFAULT 'active',     -- active | archived
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    scope              TEXT DEFAULT 'personal',   -- personal | specific | team
    owner_member_id    TEXT DEFAULT '',
    pinned_to_spaces   TEXT DEFAULT '',           -- JSON list, empty = unpinned (universal)
    canvas_yaml_path   TEXT DEFAULT ''            -- absolute path to canvas.yaml on disk
);

CREATE TABLE IF NOT EXISTS canvas_members (
    canvas_id   TEXT NOT NULL,
    member_id   TEXT NOT NULL,
    added_at    TEXT NOT NULL,
    active      INTEGER DEFAULT 1,
    PRIMARY KEY (canvas_id, member_id)
);
CREATE INDEX IF NOT EXISTS idx_canvas_members_member
    ON canvas_members(member_id, active);

CREATE TABLE IF NOT EXISTS member_profiles (
    member_id               TEXT PRIMARY KEY,
    display_name            TEXT DEFAULT '',
    timezone                TEXT DEFAULT '',
    communication_style     TEXT DEFAULT '',
    interaction_count       INTEGER DEFAULT 0,
    hatched                 INTEGER DEFAULT 0,
    hatched_at              TEXT DEFAULT '',
    bootstrap_graduated     INTEGER DEFAULT 0,
    bootstrap_graduated_at  TEXT DEFAULT '',
    agent_name              TEXT DEFAULT '',
    emoji                   TEXT DEFAULT '',
    personality_notes       TEXT DEFAULT '',
    updated_at              TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (member_id) REFERENCES members(member_id)
);

CREATE TABLE IF NOT EXISTS sender_blocks (
    sender_key  TEXT PRIMARY KEY,
    platform    TEXT NOT NULL,
    channel_id  TEXT NOT NULL,
    failure_count INTEGER DEFAULT 0,
    block_tier  INTEGER DEFAULT 0,
    blocked_until TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relationships (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    declarer_member_id TEXT NOT NULL,
    other_member_id    TEXT NOT NULL,
    permission         TEXT NOT NULL DEFAULT 'by-permission',
    updated_at         TEXT NOT NULL,
    UNIQUE(declarer_member_id, other_member_id)
);

CREATE TABLE IF NOT EXISTS platform_config (
    platform    TEXT PRIMARY KEY,
    config      TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS invite_codes (
    code         TEXT PRIMARY KEY,
    created_by   TEXT NOT NULL,
    for_member   TEXT DEFAULT '',
    display_name TEXT DEFAULT '',
    platform     TEXT DEFAULT '',
    role         TEXT DEFAULT 'member',
    status       TEXT DEFAULT 'active',
    used_by      TEXT DEFAULT '',
    created_at   TEXT NOT NULL,
    expires_at   TEXT DEFAULT '',
    used_at      TEXT DEFAULT ''
);

-- PARCEL-PRIMITIVE-V1: cross-space file transfer lifecycle
CREATE TABLE IF NOT EXISTS parcels (
    parcel_id           TEXT PRIMARY KEY,
    instance_id         TEXT NOT NULL,
    sender_member_id    TEXT NOT NULL,
    recipient_member_id TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'packed',
        -- packed | accepted | delivered | declined | expired | failed
    payload_manifest    TEXT NOT NULL DEFAULT '[]',   -- JSON: [{filename, size_bytes, sha256}]
    total_bytes         INTEGER NOT NULL DEFAULT 0,
    note                TEXT DEFAULT '',
    ttl_days            INTEGER NOT NULL DEFAULT 7,
    sender_path         TEXT DEFAULT '',              -- absolute path to sender's staged dir
    recipient_path      TEXT DEFAULT '',              -- populated on delivery
    decline_reason      TEXT DEFAULT '',
    created_at          TEXT NOT NULL,
    responded_at        TEXT DEFAULT '',
    delivered_at        TEXT DEFAULT '',
    expired_at          TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_parcels_recipient
    ON parcels(recipient_member_id, status);
CREATE INDEX IF NOT EXISTS idx_parcels_sender
    ON parcels(sender_member_id, status);
CREATE INDEX IF NOT EXISTS idx_parcels_instance
    ON parcels(instance_id, status);

-- MODEL-AND-STATUS-V1: per-(instance, member, space) chain switch
-- and head override. The override columns are nullable independently
-- and the CHECK enforces the (provider, model) pair invariant
-- (both set or both null).
CREATE TABLE IF NOT EXISTS model_overrides (
    instance_id       TEXT NOT NULL,
    member_id         TEXT NOT NULL,
    space_id          TEXT NOT NULL,
    chain_name        TEXT,
    override_provider TEXT,
    override_model    TEXT,
    set_at            TEXT NOT NULL,
    PRIMARY KEY (instance_id, member_id, space_id),
    CHECK (
        (override_provider IS NULL AND override_model IS NULL)
        OR (override_provider IS NOT NULL AND override_model IS NOT NULL)
    )
);
"""


class InstanceDB:
    """Manages the instance-level database shared across all instances."""

    def __init__(self, data_dir: str) -> None:
        self._db_path = Path(data_dir) / "instance.db"
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Initialize the instance database."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._db_path))
        self._conn.row_factory = aiosqlite.Row
        for stmt in _INSTANCE_SCHEMA.split(";"):
            stmt = stmt.strip()
            if stmt:
                await self._conn.execute(stmt)
        # Migrations: add columns that may be missing from older databases
        for _alt in [
            "ALTER TABLE invite_codes ADD COLUMN platform TEXT DEFAULT ''",
            "ALTER TABLE member_profiles ADD COLUMN agent_name TEXT DEFAULT ''",
            "ALTER TABLE member_profiles ADD COLUMN emoji TEXT DEFAULT ''",
            "ALTER TABLE member_profiles ADD COLUMN personality_notes TEXT DEFAULT ''",
            # CANVAS-V1: shared_spaces repurposed as canvas registry
            "ALTER TABLE shared_spaces ADD COLUMN scope TEXT DEFAULT 'personal'",
            "ALTER TABLE shared_spaces ADD COLUMN owner_member_id TEXT DEFAULT ''",
            "ALTER TABLE shared_spaces ADD COLUMN pinned_to_spaces TEXT DEFAULT ''",
            "ALTER TABLE shared_spaces ADD COLUMN canvas_yaml_path TEXT DEFAULT ''",
        ]:
            try:
                await self._conn.execute(_alt)
            except Exception:
                pass  # Column already exists

        # Relationship schema migration: Phase 2c.1 shipped a multi-field
        # (type, profile, status, proposed_by, confirmed_by) shape. RELATIONSHIP-
        # SIMPLIFY reduces to a directional three-value permission model.
        # Pre-launch: no production data to preserve. Drop and recreate.
        try:
            async with self._conn.execute(
                "PRAGMA table_info(relationships)"
            ) as _cur:
                _cols = {r[1] for r in await _cur.fetchall()}
            if _cols and "relationship_type" in _cols and "permission" not in _cols:
                await self._conn.execute("DROP TABLE relationships")
                await self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS relationships ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "declarer_member_id TEXT NOT NULL,"
                    "other_member_id TEXT NOT NULL,"
                    "permission TEXT NOT NULL DEFAULT 'by-permission',"
                    "updated_at TEXT NOT NULL,"
                    "UNIQUE(declarer_member_id, other_member_id))"
                )
                logger.info(
                    "RELATIONSHIPS_MIGRATE: dropped legacy schema "
                    "(pre-launch, no data preservation)"
                )
        except Exception as _exc:
            logger.warning("RELATIONSHIPS_MIGRATE: check failed: %s", _exc)

        await self._conn.commit()
        logger.info("Instance DB ready: %s", self._db_path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def ensure_owner(self, member_id: str, display_name: str,
                           instance_id: str, platform: str, channel_id: str) -> str:
        """Ensure the owner exists as a member. Called at startup.

        Returns the owner's stable member_id (mem_ format).
        If a legacy platform-prefixed ID exists, migrates it to mem_ format.
        """
        if not self._conn:
            return member_id
        import uuid
        from kernos.utils import utc_now
        now = utc_now()

        # Check if owner already exists (by role, not by the passed member_id)
        async with self._conn.execute(
            "SELECT member_id FROM members WHERE role='owner' LIMIT 1",
        ) as cur:
            row = await cur.fetchone()

        if row:
            # Owner exists — use their existing member_id
            stable_id = row[0]
            # Migrate legacy platform-prefixed IDs to mem_ format
            if not stable_id.startswith("mem_"):
                new_id = f"mem_{uuid.uuid4().hex[:8]}"
                # Update all references atomically
                await self._conn.execute("UPDATE members SET member_id=? WHERE member_id=?", (new_id, stable_id))
                await self._conn.execute("UPDATE member_channels SET member_id=? WHERE member_id=?", (new_id, stable_id))
                await self._conn.execute("UPDATE member_profiles SET member_id=? WHERE member_id=?", (new_id, stable_id))
                await self._conn.execute("UPDATE invite_codes SET created_by=? WHERE created_by=?", (new_id, stable_id))
                await self._conn.commit()
                logger.info("OWNER_MIGRATE: %s → %s", stable_id, new_id)
                stable_id = new_id
        else:
            # Create new owner with stable mem_ ID
            stable_id = f"mem_{uuid.uuid4().hex[:8]}"
            await self._conn.execute(
                "INSERT INTO members (member_id, display_name, role, instance_id, status, created_at, updated_at) "
                "VALUES (?, ?, 'owner', ?, 'active', ?, ?)",
                (stable_id, display_name, instance_id, now, now),
            )

        # Ensure channel mapping exists
        await self._conn.execute(
            "INSERT INTO member_channels (member_id, platform, channel_id, is_primary, created_at) "
            "VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(platform, channel_id) DO UPDATE SET member_id=?",
            (stable_id, platform, channel_id, now, stable_id),
        )
        await self._conn.commit()

        # Ensure owner has a member profile
        existing_profile = await self.get_member_profile(stable_id)
        if not existing_profile:
            await self.upsert_member_profile(stable_id, {"display_name": display_name})
        return stable_id

    async def migrate_soul_to_member_profile(self, member_id: str, soul_fields: dict) -> bool:
        """One-time migration: copy per-user Soul fields into the owner's member profile.

        Called at startup when transitioning from single-user to multi-member.
        Returns True if migration occurred, False if already migrated or nothing to migrate.
        """
        if not self._conn:
            return False
        profile = await self.get_member_profile(member_id)
        if not profile:
            return False
        # Only migrate if the profile has empty fields and soul has data
        updates: dict = {}
        field_map = {
            "user_name": "display_name",
            "timezone": "timezone",
            "communication_style": "communication_style",
            "interaction_count": "interaction_count",
            "bootstrap_graduated": "bootstrap_graduated",
            "bootstrap_graduated_at": "bootstrap_graduated_at",
            # Soul identity fields (Soul Revision spec)
            "agent_name": "agent_name",
            "emoji": "emoji",
            "personality_notes": "personality_notes",
            "hatched": "hatched",
            "hatched_at": "hatched_at",
        }
        for soul_field, profile_field in field_map.items():
            soul_val = soul_fields.get(soul_field)
            profile_val = profile.get(profile_field)
            if soul_val and not profile_val:
                updates[profile_field] = soul_val
        if updates:
            await self.upsert_member_profile(member_id, updates)
            logger.info("SOUL_MIGRATION: migrated %s to member_profile %s", list(updates.keys()), member_id)
            return True
        return False

    # Fallback invite instructions when platform_config is unavailable
    _INVITE_INSTRUCTIONS_FALLBACK: dict[str, str] = {
        "discord": (
            "Send this code as a DM to the Kernos bot on Discord. "
            "The bot will recognize the code and link your account."
        ),
        "telegram": (
            "Open Telegram, find the Kernos bot, and send this code as a message. "
            "The bot will recognize the code and link your account."
        ),
        "sms": (
            "Text this code to the Kernos phone number. "
            "The system will recognize the code and link your account."
        ),
    }

    async def get_invite_instructions(self, platform: str) -> str:
        """Get invite instructions with actual bot handle/phone interpolated."""
        config = await self.get_platform_config(platform)
        if platform == "telegram":
            username = config.get("bot_username", "")
            if username:
                return (
                    f"Open Telegram, search for @{username}, and send this code as a message. "
                    f"The bot will recognize the code and link your account."
                )
        elif platform == "discord":
            bot_name = config.get("bot_name", "")
            if bot_name:
                return (
                    f"Send this code as a DM to {bot_name} on Discord. "
                    f"The bot will recognize the code and link your account."
                )
        elif platform == "sms":
            phone = config.get("phone_number", "")
            if phone:
                return (
                    f"Text this code to {phone}. "
                    f"The system will recognize the code and link your account."
                )
        return self._INVITE_INSTRUCTIONS_FALLBACK.get(
            platform, f"Send this code on {platform} to connect."
        )

    _SETUP_INSTRUCTIONS: dict[str, str] = {
        "discord": (
            "To set up Discord:\n"
            "1. Go to the Discord Developer Portal (discord.com/developers/applications)\n"
            "2. Create a New Application, then go to the Bot section and create a bot\n"
            "3. Enable the Message Content Intent under Privileged Gateway Intents\n"
            "4. Copy the bot token and set DISCORD_BOT_TOKEN in .env\n"
            "5. Set DISCORD_OWNER_ID to your Discord user ID\n"
            "6. Invite the bot to your server using the OAuth2 URL Generator with bot scope + Send Messages permission\n"
            "7. Restart Kernos — the bot will appear online and respond to DMs"
        ),
        "telegram": (
            "To set up Telegram:\n"
            "1. Open Telegram and message @BotFather\n"
            "2. Send /newbot and follow the prompts to create a bot\n"
            "3. Copy the bot token BotFather gives you\n"
            "4. Set TELEGRAM_BOT_TOKEN in .env\n"
            "5. Restart Kernos — the Telegram poller will start and respond to messages"
        ),
        "sms": (
            "To set up SMS:\n"
            "1. Create a Twilio account at twilio.com\n"
            "2. Get a phone number from the Twilio console\n"
            "3. Set these in .env:\n"
            "   TWILIO_ACCOUNT_SID=your_account_sid\n"
            "   TWILIO_AUTH_TOKEN=your_auth_token\n"
            "   TWILIO_PHONE_NUMBER=+1XXXXXXXXXX\n"
            "   OWNER_PHONE_NUMBER=+1XXXXXXXXXX (your personal number)\n"
            "4. Restart Kernos — the SMS poller will start"
        ),
    }

    async def get_member_by_channel(self, platform: str, channel_id: str) -> dict | None:
        """Look up a member by platform + channel ID."""
        if not self._conn:
            return None
        async with self._conn.execute(
            "SELECT m.* FROM members m "
            "JOIN member_channels mc ON m.member_id = mc.member_id "
            "WHERE mc.platform=? AND mc.channel_id=?",
            (platform, channel_id),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_members(self, status: str = "active") -> list[dict]:
        """List all members with the given status."""
        if not self._conn:
            return []
        async with self._conn.execute(
            "SELECT * FROM members WHERE status=?", (status,)
        ) as cur:
            rows = await cur.fetchall()
        members = []
        for r in rows:
            m = dict(r)
            # Attach connected channels
            async with self._conn.execute(
                "SELECT platform, channel_id FROM member_channels WHERE member_id=?",
                (m["member_id"],),
            ) as ch_cur:
                channels = await ch_cur.fetchall()
            m["channels"] = [dict(c) for c in channels]
            members.append(m)
        return members

    # --- Instance Config (stored in platform_config with key "_instance") ---

    async def get_hatching_mode(self) -> str:
        """Get the instance's hatching mode. Returns 'unique' (default) or 'inherit'."""
        config = await self.get_platform_config("_instance")
        return config.get("hatching_mode", "unique")

    async def set_hatching_mode(self, mode: str) -> None:
        """Set hatching mode: 'unique' or 'inherit'."""
        config = await self.get_platform_config("_instance")
        config["hatching_mode"] = mode
        await self.set_platform_config("_instance", config)

    async def get_instance_stewardship(self) -> str:
        """Get the instance's stewardship purpose. What this Kernos is for."""
        config = await self.get_platform_config("_instance")
        return config.get("stewardship", "")

    async def set_instance_stewardship(self, stewardship: str) -> None:
        """Set the instance's stewardship purpose."""
        config = await self.get_platform_config("_instance")
        config["stewardship"] = stewardship
        await self.set_platform_config("_instance", config)
        logger.info("INSTANCE_STEWARDSHIP: set (%d chars)", len(stewardship))

    async def get_template_soul(self) -> dict | None:
        """Get the first hatched member's soul fields for inherit mode."""
        if not self._conn:
            return None
        async with self._conn.execute(
            "SELECT * FROM member_profiles WHERE hatched=1 ORDER BY hatched_at ASC LIMIT 1",
        ) as cur:
            row = await cur.fetchone()
        if row:
            d = dict(row)
            return {
                "agent_name": d.get("agent_name", ""),
                "emoji": d.get("emoji", ""),
                "personality_notes": d.get("personality_notes", ""),
            }
        return None

    # --- Abuse Prevention ---

    # The 24 Escalation: each failure escalates immediately.
    # 24 seconds → 24 minutes → 24 hours → 24 days → 24 years → 24 centuries.
    # After 24 hours it's not practical. But the easter egg is too damn cute.
    _BAN_TIERS: list[tuple[float, str]] = [
        (24 / 3600,                "24 seconds"),    # Tier 1: 24 seconds
        (24 / 60,                  "24 minutes"),    # Tier 2: 24 minutes
        (24,                       "24 hours"),      # Tier 3: 24 hours
        (24 * 24,                  "24 days"),       # Tier 4: 24 days
        (24 * 365 * 24,            "24 years"),      # Tier 5: 24 years
        (24 * 365 * 100 * 24,     "24 centuries"),   # Tier 6: 24 centuries
    ]

    async def check_sender_blocked(self, platform: str, channel_id: str) -> str | None:
        """Check if a sender is currently blocked. Returns block message or None."""
        if not self._conn:
            return None
        from datetime import datetime, timezone
        key = f"{platform}:{channel_id}"
        async with self._conn.execute(
            "SELECT blocked_until, block_tier FROM sender_blocks WHERE sender_key=?", (key,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        blocked_until = row[0]
        if not blocked_until:
            return None
        try:
            expires = datetime.fromisoformat(blocked_until.replace("Z", "+00:00"))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) < expires:
                tier = row[1]
                label = self._BAN_TIERS[min(tier - 1, len(self._BAN_TIERS) - 1)][1] if tier > 0 else "a while"
                return f"Try again in {label}."
        except (ValueError, TypeError):
            pass
        return None

    async def record_sender_failure(self, platform: str, channel_id: str) -> str | None:
        """Record a failed attempt. Each failure immediately escalates the ban tier."""
        if not self._conn:
            return None
        from datetime import datetime, timezone, timedelta
        from kernos.utils import utc_now
        key = f"{platform}:{channel_id}"
        now = utc_now()

        async with self._conn.execute(
            "SELECT failure_count, block_tier, blocked_until FROM sender_blocks WHERE sender_key=?", (key,),
        ) as cur:
            row = await cur.fetchone()

        if row:
            count = row[0] + 1
            tier = row[1]
            currently_blocked = False
            # If currently blocked and still trying — escalate the tier
            if row[2]:
                try:
                    expires = datetime.fromisoformat(row[2].replace("Z", "+00:00"))
                    if expires.tzinfo is None:
                        expires = expires.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) < expires:
                        currently_blocked = True
                        # Escalate: each attempt while blocked makes the next ban longer
                        new_tier = min(tier + 1, len(self._BAN_TIERS))
                        duration_hours, label = self._BAN_TIERS[min(new_tier - 1, len(self._BAN_TIERS) - 1)]
                        new_blocked_until = (datetime.now(timezone.utc) + timedelta(hours=duration_hours)).isoformat()
                        await self._conn.execute(
                            "UPDATE sender_blocks SET block_tier=?, blocked_until=?, failure_count=?, updated_at=? "
                            "WHERE sender_key=?",
                            (new_tier, new_blocked_until, count, now, key),
                        )
                        await self._conn.commit()
                        logger.warning("SENDER_ESCALATED: %s tier=%d→%d duration=%s (attempted while blocked)",
                            key, tier, new_tier, label)
                        return f"Try again in {label}."
                except (ValueError, TypeError):
                    pass
            if not currently_blocked:
                await self._conn.execute(
                    "UPDATE sender_blocks SET failure_count=?, updated_at=? WHERE sender_key=?",
                    (count, now, key),
                )
        else:
            count = 1
            tier = 0
            await self._conn.execute(
                "INSERT INTO sender_blocks (sender_key, platform, channel_id, failure_count, block_tier, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (key, platform, channel_id, count, tier, now, now),
            )

        await self._conn.commit()

        # Every failure escalates immediately
        new_tier = min(tier + 1, len(self._BAN_TIERS))
        duration_hours, label = self._BAN_TIERS[min(new_tier - 1, len(self._BAN_TIERS) - 1)]
        blocked_until = (datetime.now(timezone.utc) + timedelta(hours=duration_hours)).isoformat()
        await self._conn.execute(
            "UPDATE sender_blocks SET block_tier=?, blocked_until=?, updated_at=? "
            "WHERE sender_key=?",
            (new_tier, blocked_until, now, key),
        )
        await self._conn.commit()
        logger.warning("SENDER_BLOCKED: %s tier=%d duration=%s", key, new_tier, label)
        return f"Try again in {label}."

    async def clear_sender_failures(self, platform: str, channel_id: str) -> None:
        """Clear failure count on successful action (invite claim, member resolution)."""
        if not self._conn:
            return
        key = f"{platform}:{channel_id}"
        await self._conn.execute(
            "DELETE FROM sender_blocks WHERE sender_key=?", (key,),
        )
        await self._conn.commit()

    # --- Relationships ---
    #
    # Simplified three-value permission model (RELATIONSHIP-SIMPLIFY).
    # Rows are directional: (declarer_member_id, other_member_id) means
    # "declarer has declared `permission` toward other_member_id."
    # Absence of a row = implicit `by-permission` default (conservative).
    # Bidirectional lookup walks both directions; each side stores its own.

    _VALID_PERMISSIONS = {"full-access", "no-access", "by-permission"}

    async def declare_relationship(
        self, declarer_id: str, other_id: str, permission: str,
    ) -> dict:
        """Declare permission from declarer toward other. Returns the row."""
        if not self._conn:
            return {"error": "Database not available"}
        if permission not in self._VALID_PERMISSIONS:
            return {
                "error": (
                    f"Invalid permission {permission!r}. "
                    f"Must be one of: {sorted(self._VALID_PERMISSIONS)}"
                )
            }
        from kernos.utils import utc_now
        now = utc_now()
        await self._conn.execute(
            "INSERT INTO relationships "
            "(declarer_member_id, other_member_id, permission, updated_at) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(declarer_member_id, other_member_id) "
            "DO UPDATE SET permission=excluded.permission, "
            "updated_at=excluded.updated_at",
            (declarer_id, other_id, permission, now),
        )
        await self._conn.commit()
        logger.info(
            "RELATIONSHIP: %s→%s permission=%s",
            declarer_id, other_id, permission,
        )
        return {
            "declarer_member_id": declarer_id,
            "other_member_id": other_id,
            "permission": permission,
            "updated_at": now,
        }

    async def get_permission(self, declarer_id: str, other_id: str) -> str:
        """Return the permission `declarer_id` has declared toward `other_id`.

        Returns the declared permission, or 'by-permission' if no row exists
        (the implicit conservative default).
        """
        if not self._conn:
            return "by-permission"
        async with self._conn.execute(
            "SELECT permission FROM relationships "
            "WHERE declarer_member_id=? AND other_member_id=?",
            (declarer_id, other_id),
        ) as cur:
            row = await cur.fetchone()
        if row:
            return row["permission"]
        return "by-permission"

    async def get_relationship(self, member_a: str, member_b: str) -> dict | None:
        """Return both directions of the relationship between a and b.

        Returns {permission_a_to_b, permission_b_to_a} or None if neither
        side has declared (both sides at implicit default).
        """
        if not self._conn:
            return None
        a_to_b = await self.get_permission(member_a, member_b)
        b_to_a = await self.get_permission(member_b, member_a)
        # No row either way: treat as fully-default (return None so callers can
        # short-circuit without a row check).
        if a_to_b == "by-permission" and b_to_a == "by-permission":
            # Check whether any row actually exists — the defaults could be
            # explicit or implicit. For the render path we only care about
            # non-default declarations.
            async with self._conn.execute(
                "SELECT 1 FROM relationships WHERE "
                "(declarer_member_id=? AND other_member_id=?) OR "
                "(declarer_member_id=? AND other_member_id=?) LIMIT 1",
                (member_a, member_b, member_b, member_a),
            ) as cur:
                row = await cur.fetchone()
            if not row:
                return None
        return {
            "member_a_id": member_a,
            "member_b_id": member_b,
            "permission_a_to_b": a_to_b,
            "permission_b_to_a": b_to_a,
        }

    async def list_relationships(self, member_id: str) -> list[dict]:
        """List every declaration where `member_id` is either declarer or other.

        Returns per-row dicts with the canonical shape:
          {declarer_member_id, other_member_id, permission, updated_at,
           other_display_name}
        The caller decides how to render each row (e.g., "Tom (full-access)"
        for rows where member_id is the declarer).
        """
        if not self._conn:
            return []
        async with self._conn.execute(
            "SELECT * FROM relationships "
            "WHERE declarer_member_id=? OR other_member_id=?",
            (member_id, member_id),
        ) as cur:
            rows = await cur.fetchall()
        result: list[dict] = []
        for r in rows:
            d = dict(r)
            # Resolve the OTHER member's display name relative to member_id
            if d["declarer_member_id"] == member_id:
                other_id = d["other_member_id"]
            else:
                other_id = d["declarer_member_id"]
            other = await self.get_member(other_id)
            d["other_display_name"] = (
                other.get("display_name", other_id) if other else other_id
            )
            result.append(d)
        return result

    async def list_permissions_for(self, member_id: str) -> dict[str, str]:
        """Return a flat map {author_member_id: permission_member_has_toward_author}.

        Used by the disclosure gate to cache, in one query, every permission
        `member_id` has declared toward any other member. Members with no
        declaration do not appear; callers treat missing as `by-permission`.
        """
        if not self._conn:
            return {}
        async with self._conn.execute(
            "SELECT other_member_id, permission FROM relationships "
            "WHERE declarer_member_id=?",
            (member_id,),
        ) as cur:
            rows = await cur.fetchall()
        return {r["other_member_id"]: r["permission"] for r in rows}

    # --- Invite Codes ---

    def get_setup_instructions(self, platform: str) -> str:
        """Get first-time setup instructions for a platform."""
        return self._SETUP_INSTRUCTIONS.get(platform, f"Setup instructions for {platform} are not yet available.")

    def get_supported_platforms(self) -> list[str]:
        """Return all platforms that have known instructions."""
        return list(self._INVITE_INSTRUCTIONS_FALLBACK.keys())

    async def get_platform_config(self, platform: str) -> dict:
        """Get persisted config for a platform (bot username, phone number, etc.)."""
        if not self._conn:
            return {}
        async with self._conn.execute(
            "SELECT config FROM platform_config WHERE platform=?", (platform,),
        ) as cur:
            row = await cur.fetchone()
        if row:
            try:
                return json.loads(row[0])
            except (json.JSONDecodeError, TypeError):
                return {}
        return {}

    async def set_platform_config(self, platform: str, config: dict) -> None:
        """Persist config for a platform (bot username, phone number, etc.)."""
        if not self._conn:
            return
        await self._conn.execute(
            "INSERT INTO platform_config (platform, config) VALUES (?, ?) "
            "ON CONFLICT(platform) DO UPDATE SET config=?",
            (platform, json.dumps(config), json.dumps(config)),
        )
        await self._conn.commit()
        logger.info("Platform config stored: %s → %s", platform, list(config.keys()))

    # --- Member Profiles ---

    async def get_member_profile(self, member_id: str) -> dict | None:
        """Get a member's profile. Returns None if no profile exists."""
        if not self._conn:
            return None
        async with self._conn.execute(
            "SELECT * FROM member_profiles WHERE member_id=?", (member_id,),
        ) as cur:
            row = await cur.fetchone()
        if row:
            d = dict(row)
            # Normalize booleans from SQLite integers
            d["hatched"] = bool(d.get("hatched", 0))
            d["bootstrap_graduated"] = bool(d.get("bootstrap_graduated", 0))
            return d
        return None

    async def upsert_member_profile(self, member_id: str, updates: dict) -> None:
        """Create or update a member's profile fields."""
        if not self._conn:
            return
        from kernos.utils import utc_now
        existing = await self.get_member_profile(member_id)
        if existing is None:
            # Create new profile
            fields = {
                "member_id": member_id,
                "display_name": updates.get("display_name", ""),
                "timezone": updates.get("timezone", ""),
                "communication_style": updates.get("communication_style", ""),
                "interaction_count": updates.get("interaction_count", 0),
                "hatched": int(updates.get("hatched", False)),
                "hatched_at": updates.get("hatched_at", ""),
                "bootstrap_graduated": int(updates.get("bootstrap_graduated", False)),
                "bootstrap_graduated_at": updates.get("bootstrap_graduated_at", ""),
                "updated_at": utc_now(),
            }
            cols = ", ".join(fields.keys())
            placeholders = ", ".join("?" for _ in fields)
            await self._conn.execute(
                f"INSERT INTO member_profiles ({cols}) VALUES ({placeholders})",
                tuple(fields.values()),
            )
        else:
            # Update existing fields
            set_parts: list[str] = []
            values: list = []
            for k, v in updates.items():
                if k == "member_id":
                    continue
                if k in ("hatched", "bootstrap_graduated"):
                    v = int(v)
                set_parts.append(f"{k}=?")
                values.append(v)
            set_parts.append("updated_at=?")
            values.append(utc_now())
            values.append(member_id)
            await self._conn.execute(
                f"UPDATE member_profiles SET {', '.join(set_parts)} WHERE member_id=?",
                values,
            )
        await self._conn.commit()

    async def increment_interaction_count(self, member_id: str) -> int:
        """Increment and return the new interaction count for a member."""
        if not self._conn:
            return 0
        from kernos.utils import utc_now
        await self._conn.execute(
            "UPDATE member_profiles SET interaction_count = interaction_count + 1, updated_at=? "
            "WHERE member_id=?",
            (utc_now(), member_id),
        )
        await self._conn.commit()
        profile = await self.get_member_profile(member_id)
        return profile["interaction_count"] if profile else 0

    async def get_member(self, member_id: str) -> dict | None:
        """Get a member record from the members table."""
        if not self._conn:
            return None
        async with self._conn.execute(
            "SELECT * FROM members WHERE member_id=?", (member_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_member_channels(self, member_id: str) -> list[dict]:
        """Return all (platform, channel_id) rows for a member.

        Used by the relational-messaging dispatcher to push time-sensitive
        envelopes via the recipient's primary adapter.
        """
        if not self._conn:
            return []
        async with self._conn.execute(
            "SELECT platform, channel_id, is_primary "
            "FROM member_channels WHERE member_id=? "
            "ORDER BY is_primary DESC, created_at ASC",
            (member_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # --- Invite Codes ---

    async def create_invite_code(
        self, created_by: str, platform: str,
        for_member: str = "", display_name: str = "",
        role: str = "member", expires_hours: int = 72,
    ) -> dict:
        """Generate and store a platform-locked invite code.

        Returns dict with code, platform, instructions, expires_hours.
        """
        if not self._conn:
            return {"error": "Database not available"}
        if not platform:
            return {"error": "Platform is required — specify discord, telegram, or sms"}
        import random
        import string
        from kernos.utils import utc_now
        from datetime import datetime, timezone, timedelta

        chars = string.ascii_uppercase + string.digits
        code = "KERN-" + "".join(random.choices(chars, k=4))
        now = utc_now()
        expires = ""
        if expires_hours > 0:
            expires = (datetime.now(timezone.utc) + timedelta(hours=expires_hours)).isoformat()

        await self._conn.execute(
            "INSERT INTO invite_codes "
            "(code, created_by, for_member, display_name, platform, role, status, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (code, created_by, for_member, display_name, platform, role, "active", now, expires),
        )
        await self._conn.commit()
        logger.info("INVITE_CODE_CREATED: code=%s by=%s for=%s name=%s platform=%s expires=%s",
            code, created_by, for_member or "new_user", display_name, platform, expires[:10])

        instructions = await self.get_invite_instructions(platform)
        return {
            "code": code,
            "platform": platform,
            "instructions": instructions,
            "expires_hours": expires_hours,
        }

    async def claim_invite_code(
        self, code: str, platform: str, channel_id: str,
    ) -> dict | None:
        """Attempt to claim an invite code. Returns result dict or None if invalid."""
        if not self._conn:
            return None
        from kernos.utils import utc_now
        from datetime import datetime, timezone
        import uuid

        async with self._conn.execute(
            "SELECT * FROM invite_codes WHERE code=? AND status='active'",
            (code,),
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return None

        d = dict(row)

        # Platform enforcement — code locked to specific platform
        code_platform = d.get("platform", "")
        if code_platform and code_platform != platform:
            logger.info("INVITE_REJECTED: code=%s expected_platform=%s actual_platform=%s",
                code, code_platform, platform)
            return {
                "action": "rejected",
                "static_response": f"This invite code is for {code_platform}, not {platform}.",
            }

        # Check expiry
        if d.get("expires_at"):
            try:
                expires = datetime.fromisoformat(d["expires_at"].replace("Z", "+00:00"))
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > expires:
                    await self._conn.execute(
                        "UPDATE invite_codes SET status='expired' WHERE code=?", (code,))
                    await self._conn.commit()
                    return None
            except (ValueError, TypeError):
                pass

        now = utc_now()
        used_by = f"{platform}:{channel_id}"

        if d.get("for_member"):
            # CHANNEL ADD — link channel to existing member
            member_id = d["for_member"]
            # Validate member exists before linking
            member = await self.get_member(member_id)
            if not member:
                logger.warning("INVITE_REJECTED: for_member=%s not found in members table", member_id)
                return None
            await self.register_channel(member_id, platform, channel_id)
            await self._conn.execute(
                "UPDATE invite_codes SET status='used', used_by=?, used_at=? WHERE code=?",
                (used_by, now, code),
            )
            await self._conn.commit()
            logger.info("INVITE_CLAIMED: code=%s action=channel_add member=%s platform=%s",
                code, member_id, platform)
            return {
                "action": "channel_add",
                "member_id": member_id,
                "static_response": "Connected! This channel is now linked to your Kernos account.",
            }
        else:
            # NEW USER — create member + register channel
            member_id = f"mem_{uuid.uuid4().hex[:8]}"
            display_name = d.get("display_name", "")
            role = d.get("role", "member")
            await self.create_member(member_id, display_name, role, "")
            await self.register_channel(member_id, platform, channel_id)
            # Seed member profile with display_name from invite
            await self.upsert_member_profile(member_id, {"display_name": display_name})
            await self._conn.execute(
                "UPDATE invite_codes SET status='used', used_by=?, used_at=? WHERE code=?",
                (used_by, now, code),
            )
            await self._conn.commit()
            logger.info("MEMBER_CREATED: member_id=%s name=%s code=%s platform=%s",
                member_id, display_name, code, platform)
            return {
                "action": "new_member",
                "member_id": member_id,
                "display_name": display_name,
                "static_response": (
                    f"Welcome to Kernos{', ' + display_name if display_name else ''}! "
                    f"You're all set. Say anything to get started."
                ),
            }

    async def create_member(
        self, member_id: str, display_name: str, role: str, instance_id: str,
    ) -> None:
        """Create a new member record."""
        if not self._conn:
            return
        from kernos.utils import utc_now
        now = utc_now()
        await self._conn.execute(
            "INSERT INTO members (member_id, display_name, role, instance_id, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(member_id) DO NOTHING",
            (member_id, display_name, role, instance_id, "active", now, now),
        )
        await self._conn.commit()

    async def register_channel(
        self, member_id: str, platform: str, channel_id: str,
    ) -> None:
        """Register a new channel for an existing member."""
        if not self._conn:
            return
        from kernos.utils import utc_now
        await self._conn.execute(
            "INSERT INTO member_channels (member_id, platform, channel_id, is_primary, created_at) "
            "VALUES (?,?,?,0,?) ON CONFLICT(platform, channel_id) DO NOTHING",
            (member_id, platform, channel_id, utc_now()),
        )
        await self._conn.commit()

    async def deactivate_member(self, member_id: str) -> bool:
        """Set member status to inactive. Returns True if found."""
        if not self._conn:
            return False
        from kernos.utils import utc_now
        result = await self._conn.execute(
            "UPDATE members SET status='inactive', updated_at=? WHERE member_id=?",
            (utc_now(), member_id),
        )
        await self._conn.commit()
        return result.rowcount > 0

    # =========================================================================
    # Canvases — CANVAS-V1
    # =========================================================================

    async def save_canvas(self, canvas: dict) -> None:
        """Insert or replace a canvas registry row."""
        if not self._conn:
            return
        from kernos.utils import utc_now
        now = utc_now()
        await self._conn.execute(
            """
            INSERT INTO shared_spaces (
                space_id, name, description, status,
                created_at, updated_at,
                scope, owner_member_id, pinned_to_spaces, canvas_yaml_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(space_id) DO UPDATE SET
                name = excluded.name,
                description = excluded.description,
                status = excluded.status,
                updated_at = excluded.updated_at,
                scope = excluded.scope,
                owner_member_id = excluded.owner_member_id,
                pinned_to_spaces = excluded.pinned_to_spaces,
                canvas_yaml_path = excluded.canvas_yaml_path
            """,
            (
                canvas["canvas_id"], canvas["name"], canvas.get("description", ""),
                canvas.get("status", "active"),
                canvas.get("created_at", now), now,
                canvas["scope"], canvas.get("owner_member_id", ""),
                canvas.get("pinned_to_spaces", ""),
                canvas.get("canvas_yaml_path", ""),
            ),
        )
        await self._conn.commit()

    async def get_canvas(self, canvas_id: str) -> dict | None:
        """Return the canvas registry row, or None if not found."""
        if not self._conn:
            return None
        cur = await self._conn.execute(
            "SELECT * FROM shared_spaces WHERE space_id=?", (canvas_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        cols = [c[0] for c in cur.description]
        out = dict(zip(cols, row))
        # Alias the primary-key column for canvas-side clarity
        out["canvas_id"] = out.get("space_id")
        return out

    async def list_canvases_for_member(
        self, *, member_id: str, include_archived: bool = False,
    ) -> list[dict]:
        """Return canvases this member has access to.

        A member has access if:
          - they are an explicit member (canvas_members row, active=1), OR
          - the canvas scope is 'team' (visible to all instance members)

        Personal canvases with scope='personal' + owner_member_id=member_id
        are also returned via the explicit-membership path (canvas_create
        inserts a canvas_members row for the owner).
        """
        if not self._conn:
            return []
        status_clause = "" if include_archived else "AND ss.status = 'active'"
        query = f"""
            SELECT ss.* FROM shared_spaces ss
            WHERE (
                ss.scope = 'team'
                OR EXISTS (
                    SELECT 1 FROM canvas_members cm
                    WHERE cm.canvas_id = ss.space_id
                      AND cm.member_id = ?
                      AND cm.active = 1
                )
            )
            {status_clause}
            ORDER BY ss.updated_at DESC
        """
        cur = await self._conn.execute(query, (member_id,))
        rows = await cur.fetchall()
        cols = [c[0] for c in cur.description]
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            d["canvas_id"] = d.get("space_id")
            out.append(d)
        return out

    async def member_has_canvas_access(
        self, *, canvas_id: str, member_id: str,
    ) -> bool:
        """Fast access-check used by tool-layer enforcement."""
        if not self._conn:
            return False
        canvas = await self.get_canvas(canvas_id)
        if not canvas or canvas.get("status") != "active":
            return False
        if canvas.get("scope") == "team":
            return True
        # Specific or personal → explicit membership required
        cur = await self._conn.execute(
            "SELECT 1 FROM canvas_members WHERE canvas_id=? AND member_id=? AND active=1",
            (canvas_id, member_id),
        )
        row = await cur.fetchone()
        return row is not None

    async def add_canvas_member(
        self, *, canvas_id: str, member_id: str,
    ) -> None:
        """Add a member to a canvas. Idempotent."""
        if not self._conn:
            return
        from kernos.utils import utc_now
        await self._conn.execute(
            """
            INSERT INTO canvas_members (canvas_id, member_id, added_at, active)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(canvas_id, member_id) DO UPDATE SET active = 1
            """,
            (canvas_id, member_id, utc_now()),
        )
        await self._conn.commit()

    async def list_canvas_members(self, canvas_id: str) -> list[str]:
        """Return the active member_ids for a canvas (explicit members only).

        For team canvases, the caller should separately walk `members` and
        merge — this function returns only explicit canvas_members rows.
        """
        if not self._conn:
            return []
        cur = await self._conn.execute(
            "SELECT member_id FROM canvas_members WHERE canvas_id=? AND active=1",
            (canvas_id,),
        )
        rows = await cur.fetchall()
        return [r[0] for r in rows]

    async def find_canvas_by_name(
        self, *, name: str, scope: str = "",
        owner_member_id: str = "",
    ) -> dict | None:
        """Idempotency helper for seeders.

        Matches on (name, scope) with an optional owner_member_id filter
        for personal-scope canvases. Returns the first matching active
        canvas, or None. Used by the first-boot seeder so re-seeding is a
        no-op when the canvas already exists.
        """
        if not self._conn:
            return None
        clauses = ["name=?", "status!='archived'"]
        params: list = [name]
        if scope:
            clauses.append("scope=?")
            params.append(scope)
        if owner_member_id:
            clauses.append("owner_member_id=?")
            params.append(owner_member_id)
        cur = await self._conn.execute(
            f"SELECT * FROM shared_spaces WHERE {' AND '.join(clauses)} LIMIT 1",
            tuple(params),
        )
        row = await cur.fetchone()
        if not row:
            return None
        data = dict(row)
        data["canvas_id"] = data.get("space_id", "")
        return data

    async def archive_canvas(self, canvas_id: str) -> bool:
        """Soft-delete: set status='archived'. Row preserved."""
        if not self._conn:
            return False
        from kernos.utils import utc_now
        result = await self._conn.execute(
            "UPDATE shared_spaces SET status='archived', updated_at=? WHERE space_id=?",
            (utc_now(), canvas_id),
        )
        await self._conn.commit()
        return result.rowcount > 0

    # =========================================================================
    # Parcels — PARCEL-PRIMITIVE-V1
    # =========================================================================

    async def save_parcel(self, parcel: dict) -> None:
        """Insert a new parcel row. Expects the dict shape produced by
        :func:`kernos.kernel.parcel.Parcel.to_row`."""
        if not self._conn:
            return
        await self._conn.execute(
            """
            INSERT INTO parcels (
                parcel_id, instance_id, sender_member_id, recipient_member_id,
                status, payload_manifest, total_bytes, note, ttl_days,
                sender_path, recipient_path, decline_reason,
                created_at, responded_at, delivered_at, expired_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                parcel["parcel_id"], parcel["instance_id"],
                parcel["sender_member_id"], parcel["recipient_member_id"],
                parcel["status"], parcel["payload_manifest"],
                parcel["total_bytes"], parcel.get("note", ""),
                parcel.get("ttl_days", 7),
                parcel.get("sender_path", ""),
                parcel.get("recipient_path", ""),
                parcel.get("decline_reason", ""),
                parcel["created_at"],
                parcel.get("responded_at", ""),
                parcel.get("delivered_at", ""),
                parcel.get("expired_at", ""),
            ),
        )
        await self._conn.commit()

    async def get_parcel(self, parcel_id: str) -> dict | None:
        """Return the parcel row, or None if not found."""
        if not self._conn:
            return None
        cur = await self._conn.execute(
            "SELECT * FROM parcels WHERE parcel_id=?", (parcel_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, row))

    async def update_parcel_status(
        self,
        parcel_id: str,
        status: str,
        *,
        responded_at: str = "",
        delivered_at: str = "",
        expired_at: str = "",
        recipient_path: str = "",
        decline_reason: str = "",
    ) -> bool:
        """Update mutable parcel fields. Empty strings are ignored (no overwrite)."""
        if not self._conn:
            return False
        updates = ["status = ?"]
        args: list = [status]
        for name, val in (
            ("responded_at", responded_at),
            ("delivered_at", delivered_at),
            ("expired_at", expired_at),
            ("recipient_path", recipient_path),
            ("decline_reason", decline_reason),
        ):
            if val:
                updates.append(f"{name} = ?")
                args.append(val)
        args.append(parcel_id)
        query = f"UPDATE parcels SET {', '.join(updates)} WHERE parcel_id = ?"
        result = await self._conn.execute(query, args)
        await self._conn.commit()
        return result.rowcount > 0

    async def list_parcels(
        self,
        *,
        instance_id: str,
        member_id: str = "",
        direction: str = "all",   # sent | received | all
        status: str = "all",
        limit: int = 200,
    ) -> list[dict]:
        """Return parcels for a member filtered by direction + status.

        ``direction='sent'`` selects parcels where ``sender_member_id == member_id``;
        ``'received'`` selects ``recipient_member_id == member_id``; ``'all'``
        unions both.
        """
        if not self._conn:
            return []
        clauses = ["instance_id = ?"]
        args: list = [instance_id]
        if member_id:
            if direction == "sent":
                clauses.append("sender_member_id = ?")
                args.append(member_id)
            elif direction == "received":
                clauses.append("recipient_member_id = ?")
                args.append(member_id)
            else:  # 'all'
                clauses.append("(sender_member_id = ? OR recipient_member_id = ?)")
                args.extend([member_id, member_id])
        if status and status != "all":
            clauses.append("status = ?")
            args.append(status)
        args.append(limit)
        query = (
            "SELECT * FROM parcels WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC LIMIT ?"
        )
        cur = await self._conn.execute(query, args)
        rows = await cur.fetchall()
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in rows]

    async def expire_stale_parcels(self, *, now_iso: str) -> int:
        """Transition any ``packed`` parcel whose TTL has elapsed to ``expired``.

        Returns the number of parcels transitioned. Called from a maintenance
        tick (e.g. startup or periodic scheduler pass). Parallel to the RM
        ``state=expired`` pattern — identical shape, identical discipline.
        """
        if not self._conn:
            return 0
        from datetime import datetime, timezone, timedelta

        try:
            now_dt = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        except Exception:
            now_dt = datetime.now(timezone.utc)
        cur = await self._conn.execute(
            "SELECT parcel_id, created_at, ttl_days FROM parcels WHERE status = 'packed'",
        )
        stale: list[str] = []
        for row in await cur.fetchall():
            parcel_id, created_at, ttl_days = row
            try:
                created_dt = datetime.fromisoformat(
                    (created_at or "").replace("Z", "+00:00"),
                )
            except Exception:
                continue
            if created_dt + timedelta(days=int(ttl_days or 7)) <= now_dt:
                stale.append(parcel_id)
        for pid in stale:
            await self._conn.execute(
                "UPDATE parcels SET status='expired', expired_at=? WHERE parcel_id=?",
                (now_iso, pid),
            )
        if stale:
            await self._conn.commit()
        return len(stale)

    # ------------------------------------------------------------------
    # MODEL-AND-STATUS-V1: per-(instance, member, space) chain + head
    # override.
    # ------------------------------------------------------------------

    async def get_model_override(
        self, *, instance_id: str, member_id: str, space_id: str,
    ) -> dict | None:
        """Return the persisted override for a (member, space) pair, or
        None if no row exists. Shape:

            {
                "chain_name": str | None,
                "override_provider": str | None,
                "override_model": str | None,
                "set_at": str,
            }
        """
        if not self._conn:
            return None
        async with self._conn.execute(
            "SELECT chain_name, override_provider, override_model, set_at "
            "FROM model_overrides "
            "WHERE instance_id = ? AND member_id = ? AND space_id = ?",
            (instance_id, member_id, space_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return {
            "chain_name": row["chain_name"],
            "override_provider": row["override_provider"],
            "override_model": row["override_model"],
            "set_at": row["set_at"],
        }

    async def set_model_chain(
        self,
        *,
        instance_id: str,
        member_id: str,
        space_id: str,
        chain_name: str,
    ) -> None:
        """Switch the active chain for a (member, space). Clears any
        prior provider/model head override since the new chain has its
        own head."""
        if not self._conn:
            return
        from kernos.utils import utc_now
        await self._conn.execute(
            "INSERT INTO model_overrides "
            "(instance_id, member_id, space_id, chain_name, "
            " override_provider, override_model, set_at) "
            "VALUES (?, ?, ?, ?, NULL, NULL, ?) "
            "ON CONFLICT(instance_id, member_id, space_id) DO UPDATE SET "
            " chain_name = excluded.chain_name, "
            " override_provider = NULL, "
            " override_model = NULL, "
            " set_at = excluded.set_at",
            (instance_id, member_id, space_id, chain_name, utc_now()),
        )
        await self._conn.commit()

    async def set_model_head_override(
        self,
        *,
        instance_id: str,
        member_id: str,
        space_id: str,
        provider: str,
        model: str,
    ) -> None:
        """Record a (provider, model) head override for a (member,
        space). Preserves any existing chain_name selection — the
        override prepends the chain head at dispatch time without
        changing which chain is active."""
        if not self._conn:
            return
        from kernos.utils import utc_now
        await self._conn.execute(
            "INSERT INTO model_overrides "
            "(instance_id, member_id, space_id, chain_name, "
            " override_provider, override_model, set_at) "
            "VALUES (?, ?, ?, NULL, ?, ?, ?) "
            "ON CONFLICT(instance_id, member_id, space_id) DO UPDATE SET "
            " override_provider = excluded.override_provider, "
            " override_model = excluded.override_model, "
            " set_at = excluded.set_at",
            (
                instance_id, member_id, space_id, provider, model,
                utc_now(),
            ),
        )
        await self._conn.commit()

    async def reset_model_override(
        self, *, instance_id: str, member_id: str, space_id: str,
    ) -> bool:
        """Clear both chain selection and head override for a
        (member, space). Returns True when a row was deleted."""
        if not self._conn:
            return False
        cur = await self._conn.execute(
            "DELETE FROM model_overrides "
            "WHERE instance_id = ? AND member_id = ? AND space_id = ?",
            (instance_id, member_id, space_id),
        )
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

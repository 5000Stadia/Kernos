"""Tests for member identity resolution and invite codes."""
import pytest

from kernos.kernel.instance_db import InstanceDB
from kernos.messages.handler import _extract_invite_code


class TestExtractInviteCode:
    def test_extracts_valid_code(self):
        assert _extract_invite_code("KERN-A7X3") == "KERN-A7X3"

    def test_extracts_from_sentence(self):
        assert _extract_invite_code("Hi, my code is KERN-B9P2 thanks") == "KERN-B9P2"

    def test_case_insensitive(self):
        assert _extract_invite_code("kern-a7x3") == "KERN-A7X3"

    def test_no_code_returns_none(self):
        assert _extract_invite_code("Hello, how are you?") is None

    def test_partial_code_not_matched(self):
        assert _extract_invite_code("KERN-A7") is None

    def test_invalid_prefix_not_matched(self):
        assert _extract_invite_code("KORN-A7X3") is None


@pytest.fixture
async def idb(tmp_path):
    db = InstanceDB(str(tmp_path))
    await db.connect()
    yield db
    await db.close()


class TestInstanceDB:
    async def test_ensure_owner(self, idb):
        await idb.ensure_owner("owner1", "the design review", "inst1", "discord", "12345")
        members = await idb.list_members()
        assert len(members) == 1
        assert members[0]["role"] == "owner"
        assert members[0]["display_name"] == "the design review"

    async def test_get_member_by_channel(self, idb):
        stable_id = await idb.ensure_owner("owner1", "the design review", "inst1", "discord", "12345")
        member = await idb.get_member_by_channel("discord", "12345")
        assert member is not None
        assert member["member_id"] == stable_id
        assert stable_id.startswith("mem_")

    async def test_unknown_channel_returns_none(self, idb):
        member = await idb.get_member_by_channel("discord", "99999")
        assert member is None


class TestInviteCodes:
    async def test_create_code(self, idb):
        result = await idb.create_invite_code("owner1", platform="discord", display_name="Sarah")
        assert result["code"].startswith("KERN-")
        assert result["platform"] == "discord"
        assert "instructions" in result

    async def test_create_requires_platform(self, idb):
        result = await idb.create_invite_code("owner1", platform="", display_name="Sarah")
        assert "error" in result

    async def test_instructions_returned(self, idb):
        result = await idb.create_invite_code("owner1", platform="telegram", display_name="Bob")
        assert "Telegram" in result["instructions"] or "telegram" in result["instructions"].lower()

    async def test_claim_new_user(self, idb):
        await idb.ensure_owner("owner1", "the design review", "inst1", "discord", "12345")
        result = await idb.create_invite_code("owner1", platform="sms", display_name="Sarah")
        code = result["code"]

        result = await idb.claim_invite_code(code, "sms", "+15551234567")
        assert result is not None
        assert result["action"] == "new_member"
        assert result["display_name"] == "Sarah"
        assert "Welcome" in result["static_response"]

        # Sarah should now be findable by channel
        member = await idb.get_member_by_channel("sms", "+15551234567")
        assert member is not None

    async def test_claim_channel_add(self, idb):
        stable_id = await idb.ensure_owner("owner1", "the design review", "inst1", "discord", "12345")
        r = await idb.create_invite_code(stable_id, platform="sms", for_member=stable_id)
        code = r["code"]

        result = await idb.claim_invite_code(code, "sms", "+15559876543")
        assert result is not None
        assert result["action"] == "channel_add"
        assert "Connected" in result["static_response"]

        # Owner should now be findable on both channels
        m1 = await idb.get_member_by_channel("discord", "12345")
        m2 = await idb.get_member_by_channel("sms", "+15559876543")
        assert m1["member_id"] == m2["member_id"]

    async def test_invalid_code_returns_none(self, idb):
        result = await idb.claim_invite_code("KERN-ZZZZ", "sms", "+1555")
        assert result is None

    async def test_platform_enforcement(self, idb):
        """Code for discord rejected on sms."""
        await idb.ensure_owner("owner1", "the design review", "inst1", "discord", "12345")
        r = await idb.create_invite_code("owner1", platform="discord", display_name="Sarah")
        code = r["code"]
        # Try to claim on wrong platform
        result = await idb.claim_invite_code(code, "sms", "+15551111111")
        assert result is not None
        assert result["action"] == "rejected"
        assert "discord" in result["static_response"].lower()

    async def test_code_used_once(self, idb):
        await idb.ensure_owner("owner1", "the design review", "inst1", "discord", "12345")
        r = await idb.create_invite_code("owner1", platform="sms", display_name="Sarah")
        code = r["code"]

        # First claim succeeds
        result1 = await idb.claim_invite_code(code, "sms", "+15551111111")
        assert result1 is not None

        # Second claim fails (code already used)
        result2 = await idb.claim_invite_code(code, "sms", "+15552222222")
        assert result2 is None

    async def test_deactivate_member(self, idb):
        await idb.ensure_owner("owner1", "the design review", "inst1", "discord", "12345")
        r = await idb.create_invite_code("owner1", platform="sms", display_name="Bob")
        await idb.claim_invite_code(r["code"], "sms", "+15553333333")

        members_before = await idb.list_members()
        assert len(members_before) == 2

        # Find Bob's member_id
        bob = next(m for m in members_before if m["display_name"] == "Bob")
        success = await idb.deactivate_member(bob["member_id"])
        assert success is True

        # Bob is now inactive
        active = await idb.list_members(status="active")
        assert len(active) == 1

    async def test_list_members_shows_channels(self, idb):
        await idb.ensure_owner("owner1", "the design review", "inst1", "discord", "12345")
        members = await idb.list_members()
        assert len(members[0]["channels"]) == 1
        assert members[0]["channels"][0]["platform"] == "discord"


class TestPlatformConfig:
    async def test_get_empty_config(self, idb):
        config = await idb.get_platform_config("telegram")
        assert config == {}

    async def test_set_and_get_config(self, idb):
        await idb.set_platform_config("telegram", {"bot_username": "my_test_bot", "bot_name": "Test Bot"})
        config = await idb.get_platform_config("telegram")
        assert config["bot_username"] == "my_test_bot"
        assert config["bot_name"] == "Test Bot"

    async def test_update_config(self, idb):
        await idb.set_platform_config("telegram", {"bot_username": "old_bot"})
        await idb.set_platform_config("telegram", {"bot_username": "new_bot"})
        config = await idb.get_platform_config("telegram")
        assert config["bot_username"] == "new_bot"

    async def test_multiple_platforms(self, idb):
        await idb.set_platform_config("telegram", {"bot_username": "tg_bot"})
        await idb.set_platform_config("sms", {"phone_number": "+15551234567"})
        tg = await idb.get_platform_config("telegram")
        sms = await idb.get_platform_config("sms")
        assert tg["bot_username"] == "tg_bot"
        assert sms["phone_number"] == "+15551234567"


class TestDynamicInviteInstructions:
    async def test_telegram_with_username(self, idb):
        await idb.set_platform_config("telegram", {"bot_username": "kernos_test_bot"})
        instructions = await idb.get_invite_instructions("telegram")
        assert "@kernos_test_bot" in instructions

    async def test_telegram_without_username(self, idb):
        instructions = await idb.get_invite_instructions("telegram")
        assert "Kernos bot" in instructions  # Fallback text
        assert "@" not in instructions

    async def test_discord_with_bot_name(self, idb):
        await idb.set_platform_config("discord", {"bot_name": "Kernos#1234"})
        instructions = await idb.get_invite_instructions("discord")
        assert "Kernos#1234" in instructions

    async def test_sms_with_phone(self, idb):
        await idb.set_platform_config("sms", {"phone_number": "+15551234567"})
        instructions = await idb.get_invite_instructions("sms")
        assert "+15551234567" in instructions

    async def test_invite_code_includes_dynamic_instructions(self, idb):
        await idb.set_platform_config("telegram", {"bot_username": "dynamic_bot"})
        result = await idb.create_invite_code("owner1", platform="telegram", display_name="Sarah")
        assert "@dynamic_bot" in result["instructions"]


class TestAbusePrevention:
    async def test_no_block_initially(self, idb):
        result = await idb.check_sender_blocked("telegram", "attacker1")
        assert result is None

    async def test_first_failure_blocks_24_seconds(self, idb):
        ban_msg = await idb.record_sender_failure("telegram", "attacker1")
        assert ban_msg is not None
        assert "24 seconds" in ban_msg
        # Should also be blocked on check
        result = await idb.check_sender_blocked("telegram", "attacker1")
        assert result is not None
        assert "24 seconds" in result

    async def test_successful_resolution_clears_failures(self, idb):
        await idb.record_sender_failure("telegram", "gooduser1")
        # Blocked after first failure
        assert await idb.check_sender_blocked("telegram", "gooduser1") is not None
        # Successful resolution clears everything
        await idb.clear_sender_failures("telegram", "gooduser1")
        assert await idb.check_sender_blocked("telegram", "gooduser1") is None

    async def test_different_senders_independent(self, idb):
        await idb.record_sender_failure("telegram", "bad1")
        assert await idb.check_sender_blocked("telegram", "bad1") is not None
        assert await idb.check_sender_blocked("telegram", "good1") is None

    async def test_tiers_escalate_through_the_24s(self, idb):
        """The 24 Escalation: 24s → 24m → 24h → 24d → 24y → 24 centuries."""
        # Tier 1: first failure → 24 seconds
        msg = await idb.record_sender_failure("discord", "persistent")
        assert "24 seconds" in msg

        # Simulate unban, fail again → tier 2: 24 minutes
        await idb._conn.execute(
            "UPDATE sender_blocks SET blocked_until='' WHERE sender_key=?",
            ("discord:persistent",),
        )
        await idb._conn.commit()
        msg = await idb.record_sender_failure("discord", "persistent")
        assert "24 minutes" in msg

        # Simulate unban, fail again → tier 3: 24 hours
        await idb._conn.execute(
            "UPDATE sender_blocks SET blocked_until='' WHERE sender_key=?",
            ("discord:persistent",),
        )
        await idb._conn.commit()
        msg = await idb.record_sender_failure("discord", "persistent")
        assert "24 hours" in msg

        # Simulate unban, fail again → tier 4: 24 days
        await idb._conn.execute(
            "UPDATE sender_blocks SET blocked_until='' WHERE sender_key=?",
            ("discord:persistent",),
        )
        await idb._conn.commit()
        msg = await idb.record_sender_failure("discord", "persistent")
        assert "24 days" in msg

        # Simulate unban, fail again → tier 5: 24 years
        await idb._conn.execute(
            "UPDATE sender_blocks SET blocked_until='' WHERE sender_key=?",
            ("discord:persistent",),
        )
        await idb._conn.commit()
        msg = await idb.record_sender_failure("discord", "persistent")
        assert "24 years" in msg

        # Simulate unban, fail again → tier 6: 24 centuries
        await idb._conn.execute(
            "UPDATE sender_blocks SET blocked_until='' WHERE sender_key=?",
            ("discord:persistent",),
        )
        await idb._conn.commit()
        msg = await idb.record_sender_failure("discord", "persistent")
        assert "24 centuries" in msg

    async def test_spamming_while_blocked_escalates(self, idb):
        """Attempting while blocked escalates the ban — doesn't stay stuck at tier 1."""
        # First failure → 24 seconds
        msg = await idb.record_sender_failure("telegram", "spammer")
        assert "24 seconds" in msg

        # Spam while blocked → escalates to 24 minutes
        msg = await idb.record_sender_failure("telegram", "spammer")
        assert "24 minutes" in msg

        # Keep spamming → 24 hours
        msg = await idb.record_sender_failure("telegram", "spammer")
        assert "24 hours" in msg

        # Keep going → 24 days
        msg = await idb.record_sender_failure("telegram", "spammer")
        assert "24 days" in msg

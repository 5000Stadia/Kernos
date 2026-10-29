"""Tests for conversation log system (P1-P4)."""
import json
from pathlib import Path

import pytest

from kernos.kernel.conversation_log import ConversationLogger, _parse_entries


class TestConversationLoggerAppend:
    async def test_creates_log_file(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "space_abc", "user", "discord", "Hello world")

        log = tmp_path / "tenants" / "t1" / "spaces" / "space_abc" / "logs" / "log_001.txt"
        assert log.exists()
        text = log.read_text()
        assert "[user]" in text
        assert "[discord]" in text
        assert "Hello world" in text

    async def test_appends_multiple_entries(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "space_abc", "user", "discord", "Hello")
        await logger.append("t1", "space_abc", "assistant", "discord", "Hi there!")

        log = tmp_path / "tenants" / "t1" / "spaces" / "space_abc" / "logs" / "log_001.txt"
        entries = _parse_entries(log.read_text())
        assert len(entries) == 2
        assert entries[0]["role"] == "user"
        assert entries[1]["role"] == "assistant"

    async def test_cross_channel_same_log(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "space_abc", "user", "discord", "From Discord")
        await logger.append("t1", "space_abc", "user", "sms", "From SMS")

        log = tmp_path / "tenants" / "t1" / "spaces" / "space_abc" / "logs" / "log_001.txt"
        entries = _parse_entries(log.read_text())
        assert len(entries) == 2
        assert entries[0]["channel"] == "discord"
        assert entries[1]["channel"] == "sms"

    async def test_scheduled_channel(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "space_abc", "assistant", "scheduled", "Reminder: dentist")

        log = tmp_path / "tenants" / "t1" / "spaces" / "space_abc" / "logs" / "log_001.txt"
        assert "[scheduled]" in log.read_text()

    async def test_whisper_channel(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "space_abc", "assistant", "whisper", "Meeting in 30 min")

        log = tmp_path / "tenants" / "t1" / "spaces" / "space_abc" / "logs" / "log_001.txt"
        assert "[whisper]" in log.read_text()


class TestMultilineContent:
    async def test_multiline_written_naturally(self, tmp_path):
        """Multiline content is written with real newlines, not escaped."""
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "s1", "assistant", "discord",
                           "Line 1\nLine 2\nLine 3")

        log = tmp_path / "tenants" / "t1" / "spaces" / "s1" / "logs" / "log_001.txt"
        text = log.read_text()
        # Real newlines, not escaped
        assert "\\n" not in text
        assert "Line 1\nLine 2\nLine 3" in text

    async def test_multiline_parsed_correctly(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "s1", "assistant", "discord",
                           "Line 1\nLine 2\nLine 3")
        await logger.append("t1", "s1", "user", "discord", "Got it")

        entries = await logger.read_recent("t1", "s1")
        assert len(entries) == 2
        assert entries[0]["content"] == "Line 1\nLine 2\nLine 3"
        assert entries[1]["content"] == "Got it"


class TestMetaFile:
    async def test_meta_created(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "space_abc", "user", "discord", "Hello")

        meta_path = tmp_path / "tenants" / "t1" / "spaces" / "space_abc" / "logs" / "meta.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        assert meta["current_log"] == 1
        assert meta["current_log_tokens_est"] > 0

    async def test_token_estimate_increases(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "space_abc", "user", "discord", "Short")
        meta1 = logger._load_meta("t1", "space_abc")
        est1 = meta1["current_log_tokens_est"]

        await logger.append("t1", "space_abc", "assistant", "discord",
                           "A much longer response with more content")
        meta2 = logger._load_meta("t1", "space_abc")
        assert meta2["current_log_tokens_est"] > est1


class TestTimestamp:
    async def test_custom_timestamp(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "space_abc", "user", "discord", "Hello",
                           timestamp="2026-03-22T14:00:06-07:00")

        log = tmp_path / "tenants" / "t1" / "spaces" / "space_abc" / "logs" / "log_001.txt"
        assert "[2026-03-22T14:00:06-07:00]" in log.read_text()


class TestLogFormat:
    async def test_format_matches_spec(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "space_abc", "user", "discord", "Test message",
                           timestamp="2026-03-22T14:00:06-07:00")

        log = tmp_path / "tenants" / "t1" / "spaces" / "space_abc" / "logs" / "log_001.txt"
        text = log.read_text().strip()
        assert text == "[2026-03-22T14:00:06-07:00] [user] [discord] Test message"


class TestEmptySpaceId:
    async def test_skips_empty_space(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "", "user", "discord", "Hello")
        tenants_dir = tmp_path / "tenants"
        assert not tenants_dir.exists()


# ---------------------------------------------------------------------------
# P2: Read — read_recent + parse
# ---------------------------------------------------------------------------


class TestReadRecent:
    async def test_reads_entries_in_order(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "s1", "user", "discord", "First")
        await logger.append("t1", "s1", "assistant", "discord", "Second")
        await logger.append("t1", "s1", "user", "sms", "Third")

        entries = await logger.read_recent("t1", "s1")
        assert len(entries) == 3
        assert entries[0]["content"] == "First"
        assert entries[1]["content"] == "Second"
        assert entries[2]["content"] == "Third"
        assert entries[2]["channel"] == "sms"

    async def test_cross_channel_in_same_read(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "s1", "user", "discord", "From Discord")
        await logger.append("t1", "s1", "user", "sms", "From SMS")

        entries = await logger.read_recent("t1", "s1")
        channels = [e["channel"] for e in entries]
        assert "discord" in channels
        assert "sms" in channels

    async def test_empty_log_returns_empty(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        assert await logger.read_recent("t1", "s1") == []

    async def test_empty_space_returns_empty(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        assert await logger.read_recent("t1", "") == []

    async def test_max_messages_cap(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        for i in range(10):
            await logger.append("t1", "s1", "user", "discord", f"Msg {i}")

        entries = await logger.read_recent("t1", "s1", max_messages=3, token_budget=100000)
        assert len(entries) == 3
        assert entries[0]["content"] == "Msg 7"
        assert entries[2]["content"] == "Msg 9"

    async def test_token_budget_limits_entries(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "s1", "user", "discord", "Short")
        await logger.append("t1", "s1", "assistant", "discord", "A" * 2000)
        await logger.append("t1", "s1", "user", "discord", "After long")

        entries = await logger.read_recent("t1", "s1", token_budget=200)
        assert len(entries) < 3
        assert len(entries) >= 1

    async def test_scheduled_and_whisper_channels(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "s1", "assistant", "scheduled", "Reminder!")
        await logger.append("t1", "s1", "assistant", "whisper", "Meeting in 30 min")

        entries = await logger.read_recent("t1", "s1")
        assert len(entries) == 2
        assert entries[0]["channel"] == "scheduled"
        assert entries[1]["channel"] == "whisper"


class TestParseEntries:
    def test_parses_single_line(self):
        text = "[2026-03-22T14:00:06-07:00] [user] [discord] Hello there\n"
        entries = _parse_entries(text)
        assert len(entries) == 1
        assert entries[0]["content"] == "Hello there"
        assert entries[0]["role"] == "user"
        assert entries[0]["channel"] == "discord"

    def test_parses_multiline_content(self):
        text = (
            "[2026-03-22T14:00:06-07:00] [assistant] [discord] Line 1\n"
            "Line 2\n"
            "Line 3\n"
            "[2026-03-22T14:01:00-07:00] [user] [discord] Got it\n"
        )
        entries = _parse_entries(text)
        assert len(entries) == 2
        assert entries[0]["content"] == "Line 1\nLine 2\nLine 3"
        assert entries[1]["content"] == "Got it"

    def test_empty_text(self):
        assert _parse_entries("") == []

    def test_no_valid_entries(self):
        assert _parse_entries("just some random text\nno entries here\n") == []


# ---------------------------------------------------------------------------
# P3: Compaction support
# ---------------------------------------------------------------------------


class TestGetCurrentLogInfo:
    async def test_returns_info_with_log(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "s1", "user", "discord", "Hello")

        info = await logger.get_current_log_info("t1", "s1")
        assert info["log_number"] == 1
        assert info["tokens_est"] > 0
        assert info["exists"] is True

    async def test_returns_info_without_log(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        info = await logger.get_current_log_info("t1", "s1")
        assert info["log_number"] == 1
        assert info["exists"] is False


class TestReadCurrentLogText:
    async def test_reads_full_text(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "s1", "user", "discord", "Hello")
        await logger.append("t1", "s1", "assistant", "discord", "Hi!")

        text, num = await logger.read_current_log_text("t1", "s1")
        assert num == 1
        assert "Hello" in text
        assert "Hi!" in text

    async def test_raises_when_no_file(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        with pytest.raises(FileNotFoundError):
            await logger.read_current_log_text("t1", "s1")


class TestRollLog:
    async def test_advances_log_number(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "s1", "user", "discord", "Before roll")

        old_num, new_num = await logger.roll_log("t1", "s1")
        assert old_num == 1
        assert new_num == 2

        meta = logger._load_meta("t1", "s1")
        assert meta["current_log"] == 2
        assert meta["current_log_tokens_est"] == 0

    async def test_new_appends_go_to_new_log(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "s1", "user", "discord", "In log 1")
        await logger.roll_log("t1", "s1")
        await logger.append("t1", "s1", "user", "discord", "In log 2")

        log1 = tmp_path / "tenants" / "t1" / "spaces" / "s1" / "logs" / "log_001.txt"
        assert "In log 1" in log1.read_text()

        log2 = tmp_path / "tenants" / "t1" / "spaces" / "s1" / "logs" / "log_002.txt"
        assert "In log 2" in log2.read_text()
        assert "In log 1" not in log2.read_text()

    async def test_read_recent_reads_from_new_log(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "s1", "user", "discord", "Old message")
        await logger.roll_log("t1", "s1")
        await logger.append("t1", "s1", "user", "discord", "New message")

        entries = await logger.read_recent("t1", "s1")
        assert len(entries) == 1
        assert entries[0]["content"] == "New message"


class TestSeedFromPrevious:
    async def test_seeds_tail_entries(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        for i in range(20):
            await logger.append("t1", "s1", "user", "discord", f"Msg {i}")
        await logger.roll_log("t1", "s1")

        seeded = await logger.seed_from_previous("t1", "s1", 1, tail_entries=5)
        assert seeded == 5

        entries = await logger.read_recent("t1", "s1")
        assert len(entries) == 5
        assert entries[0]["content"] == "Msg 15"
        assert entries[4]["content"] == "Msg 19"

    async def test_seeds_all_when_fewer_than_tail(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "s1", "user", "discord", "Only one")
        await logger.roll_log("t1", "s1")

        seeded = await logger.seed_from_previous("t1", "s1", 1, tail_entries=10)
        assert seeded == 1

    async def test_returns_zero_for_missing_log(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        assert await logger.seed_from_previous("t1", "s1", 99) == 0

    async def test_updates_token_estimate(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "s1", "user", "discord", "A" * 100)
        await logger.roll_log("t1", "s1")

        meta_before = logger._load_meta("t1", "s1")
        assert meta_before["current_log_tokens_est"] == 0

        await logger.seed_from_previous("t1", "s1", 1)
        meta_after = logger._load_meta("t1", "s1")
        assert meta_after["current_log_tokens_est"] > 0

    async def test_multiline_entries_seeded_correctly(self, tmp_path):
        """Multiline entries survive seeding intact."""
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "s1", "assistant", "discord",
                           "Line 1\nLine 2\nLine 3")
        await logger.roll_log("t1", "s1")
        await logger.seed_from_previous("t1", "s1", 1, tail_entries=1)

        entries = await logger.read_recent("t1", "s1")
        assert len(entries) == 1
        assert entries[0]["content"] == "Line 1\nLine 2\nLine 3"


# ---------------------------------------------------------------------------
# P4: read_log_text + remember_details
# ---------------------------------------------------------------------------


class TestReadLogText:
    async def test_reads_archived_log(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        await logger.append("t1", "s1", "user", "discord", "In log 1")
        await logger.roll_log("t1", "s1")
        await logger.append("t1", "s1", "user", "discord", "In log 2")

        text = await logger.read_log_text("t1", "s1", 1)
        assert text is not None
        assert "In log 1" in text

        text2 = await logger.read_log_text("t1", "s1", 2)
        assert text2 is not None
        assert "In log 2" in text2

    async def test_returns_none_for_missing(self, tmp_path):
        logger = ConversationLogger(data_dir=str(tmp_path))
        assert await logger.read_log_text("t1", "s1", 99) is None


class TestParseLogRef:
    def test_standard_format(self):
        from kernos.kernel.reasoning import ReasoningService
        assert ReasoningService._parse_log_ref("log_003") == 3

    def test_bare_number(self):
        from kernos.kernel.reasoning import ReasoningService
        assert ReasoningService._parse_log_ref("3") == 3

    def test_no_underscore(self):
        from kernos.kernel.reasoning import ReasoningService
        assert ReasoningService._parse_log_ref("log003") == 3

    def test_invalid(self):
        from kernos.kernel.reasoning import ReasoningService
        assert ReasoningService._parse_log_ref("abc") is None


class TestExtractRelevantSection:
    def test_finds_matching_lines_with_context(self):
        from kernos.kernel.reasoning import ReasoningService
        log = "\n".join([
            "[ts] [user] [discord] Line 1",
            "[ts] [user] [discord] Line 2",
            "[ts] [user] [discord] Henderson deal discussion",
            "[ts] [user] [discord] Line 4",
            "[ts] [user] [discord] Line 5",
        ])
        result = ReasoningService._extract_relevant_section(log, "Henderson", context_lines=1)
        assert "Henderson" in result
        assert "Line 2" in result
        assert "Line 4" in result

    def test_returns_empty_on_no_match(self):
        from kernos.kernel.reasoning import ReasoningService
        assert ReasoningService._extract_relevant_section("no match here", "Henderson") == ""


class TestRememberDetailsHandler:
    async def test_no_source_ref_returns_guidance(self):
        from kernos.kernel.reasoning import ReasoningService
        from unittest.mock import MagicMock
        svc = ReasoningService(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        result = await svc._handle_remember_details("t1", "s1", {"source_ref": ""})
        assert "remember()" in result

    async def test_invalid_ref_returns_error(self):
        from kernos.kernel.reasoning import ReasoningService
        from unittest.mock import MagicMock
        svc = ReasoningService(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        result = await svc._handle_remember_details("t1", "s1", {"source_ref": "xyz"})
        assert "Could not parse" in result

    async def test_retrieves_from_archived_log(self, tmp_path):
        from kernos.kernel.reasoning import ReasoningService
        from unittest.mock import MagicMock, AsyncMock

        conv_logger = ConversationLogger(data_dir=str(tmp_path))
        await conv_logger.append("t1", "s1", "user", "discord", "Henderson discussed")
        await conv_logger.roll_log("t1", "s1")

        svc = ReasoningService(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        handler_mock = MagicMock()
        # Satisfy HandlerProtocol: read_log_text delegates to conv_logger
        async def _read_log(tid, sid, num, member_id=""):
            return await conv_logger.read_log_text(tid, sid, num, member_id=member_id)
        handler_mock.read_log_text = _read_log
        svc.set_handler(handler_mock)

        result = await svc._handle_remember_details("t1", "s1", {
            "source_ref": "log_001", "query": "Henderson",
        })
        assert "Henderson" in result
        assert "log_001" in result

    async def test_full_log_without_query(self, tmp_path):
        from kernos.kernel.reasoning import ReasoningService
        from unittest.mock import MagicMock

        conv_logger = ConversationLogger(data_dir=str(tmp_path))
        await conv_logger.append("t1", "s1", "user", "discord", "Short log")

        svc = ReasoningService(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        handler_mock = MagicMock()
        async def _read_log(tid, sid, num, member_id=""):
            return await conv_logger.read_log_text(tid, sid, num, member_id=member_id)
        handler_mock.read_log_text = _read_log
        svc.set_handler(handler_mock)

        result = await svc._handle_remember_details("t1", "s1", {"source_ref": "1"})
        assert "Short log" in result
        assert "full log" in result


# ---------------------------------------------------------------------------
# Seeded tokens tracking (Bug fix: compaction cascade)
# ---------------------------------------------------------------------------


    async def test_reads_the_requesting_members_own_log(self, tmp_path):
        """remember_details must read the member's log, never another's."""
        from kernos.kernel.reasoning import ReasoningService
        from unittest.mock import MagicMock

        conv_logger = ConversationLogger(data_dir=str(tmp_path))
        await conv_logger.append("t1", "s1", "user", "discord", "Henderson pricing",
                                 member_id="mem_a")
        await conv_logger.roll_log("t1", "s1", member_id="mem_a")

        svc = ReasoningService(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        handler_mock = MagicMock()

        async def _read_log(tid, sid, num, member_id=""):
            return await conv_logger.read_log_text(tid, sid, num, member_id=member_id)
        handler_mock.read_log_text = _read_log
        svc.set_handler(handler_mock)

        own = await svc._handle_remember_details(
            "t1", "s1", {"source_ref": "log_001", "query": "Henderson"},
            member_id="mem_a",
        )
        assert "Henderson pricing" in own
        other = await svc._handle_remember_details(
            "t1", "s1", {"source_ref": "log_001", "query": "Henderson"},
            member_id="mem_b",
        )
        assert "Henderson" not in other
        assert "not found" in other

    async def test_member_read_never_falls_back_to_shared_folder(self, tmp_path):
        conv_logger = ConversationLogger(data_dir=str(tmp_path))
        # An unscoped write lands in the shared legacy folder.
        await conv_logger.append("t1", "s1", "user", "discord", "shared text")
        assert await conv_logger.read_log_text("t1", "s1", 1) is not None
        assert await conv_logger.read_log_text("t1", "s1", 1, member_id="mem_a") is None
        text, _ = await conv_logger.read_current_log_text("t1", "s1")
        assert "shared text" in text
        try:
            text, _ = await conv_logger.read_current_log_text("t1", "s1", member_id="mem_a")
        except FileNotFoundError:
            text = ""
        assert "shared text" not in text


class TestSeededTokensTracking:
    async def test_seed_tracks_seeded_tokens(self, tmp_path):
        """After seeding, seeded_tokens_est is tracked separately."""
        logger = ConversationLogger(tmp_path)
        # Write some entries to log 1
        for i in range(5):
            await logger.append("t1", "s1", "user", "discord", f"Message {i} with some content")
            await logger.append("t1", "s1", "assistant", "discord", f"Response {i} with some content")

        # Roll and seed
        old_num, new_num = await logger.roll_log("t1", "s1")
        seeded = await logger.seed_from_previous("t1", "s1", old_num, tail_entries=5)
        assert seeded == 5

        # Check that seeded tokens are tracked
        info = await logger.get_current_log_info("t1", "s1")
        assert info["seeded_tokens_est"] > 0
        assert info["tokens_est"] >= info["seeded_tokens_est"]

    async def test_new_tokens_excludes_seeded(self, tmp_path):
        """New conversation tokens = total - seeded."""
        logger = ConversationLogger(tmp_path)
        # Write entries to log 1
        for i in range(3):
            await logger.append("t1", "s1", "user", "discord", f"Old message {i}")
            await logger.append("t1", "s1", "assistant", "discord", f"Old response {i}")

        # Roll and seed
        old_num, _ = await logger.roll_log("t1", "s1")
        await logger.seed_from_previous("t1", "s1", old_num, tail_entries=3)

        info_after_seed = await logger.get_current_log_info("t1", "s1")
        seeded = info_after_seed["seeded_tokens_est"]
        total_after_seed = info_after_seed["tokens_est"]
        assert total_after_seed == seeded  # no new content yet

        # Add genuine new content
        await logger.append("t1", "s1", "user", "discord", "Brand new message")
        await logger.append("t1", "s1", "assistant", "discord", "Brand new response")

        info_after_new = await logger.get_current_log_info("t1", "s1")
        new_tokens = info_after_new["tokens_est"] - info_after_new["seeded_tokens_est"]
        assert new_tokens > 0
        # seeded_tokens_est should be unchanged
        assert info_after_new["seeded_tokens_est"] == seeded

    async def test_roll_resets_seeded_tokens(self, tmp_path):
        """Rolling a log resets seeded_tokens_est to 0."""
        logger = ConversationLogger(tmp_path)
        await logger.append("t1", "s1", "user", "discord", "Hello")
        old_num, _ = await logger.roll_log("t1", "s1")
        await logger.seed_from_previous("t1", "s1", old_num, tail_entries=1)

        info = await logger.get_current_log_info("t1", "s1")
        assert info["seeded_tokens_est"] > 0

        # Roll again — seeded tokens should reset
        await logger.roll_log("t1", "s1")
        info2 = await logger.get_current_log_info("t1", "s1")
        assert info2["seeded_tokens_est"] == 0

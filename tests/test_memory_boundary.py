"""Boundary work (compaction + fact harvest) runs after the reply, on new text only.

persist.run records the turn and marks boundary work; the space runner calls
persist.run_boundary once the reply is out. The boundary compacts the log
without the head seeded from the previous log (already compacted there) and
harvests only what no earlier harvest of this log covered.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from kernos.kernel.compaction import CompactionState
from kernos.kernel.conversation_log import ConversationLogger
from kernos.messages.phases import persist

INST, SPACE, MEMBER = "t1", "sp1", "mem_a"


async def _log_with_seeds(tmp_path):
    conv = ConversationLogger(data_dir=str(tmp_path))
    await conv.append(INST, SPACE, "user", "discord", "OLD turn one", member_id=MEMBER)
    await conv.append(INST, SPACE, "assistant", "discord", "OLD turn two", member_id=MEMBER)
    old, _ = await conv.roll_log(INST, SPACE, member_id=MEMBER)
    await conv.seed_from_previous(INST, SPACE, old, tail_entries=2, member_id=MEMBER)
    await conv.append(INST, SPACE, "user", "discord", "NEW turn one", member_id=MEMBER)
    await conv.append(INST, SPACE, "assistant", "discord", "NEW turn two", member_id=MEMBER)
    return conv


def _handler(conv, comp_state):
    compaction = MagicMock()
    compaction.load_state = AsyncMock(return_value=comp_state)
    compaction.save_state = AsyncMock()
    compaction.compact_from_log = AsyncMock(side_effect=lambda *a, **k: a[5])
    reasoning = MagicMock()
    reasoning.get_last_real_input_tokens = MagicMock(return_value=0)
    return SimpleNamespace(
        _compacting=set(), compaction=compaction, conv_logger=conv,
        reasoning=reasoning, state=MagicMock(), events=MagicMock(), _retrieval=None,
        send_outbound=AsyncMock(), _process_compaction_follow_ups=AsyncMock(),
        _assess_domain_creation=AsyncMock(), _produce_child_briefings=AsyncMock(),
    )


def _ctx(handler):
    return SimpleNamespace(
        handler=handler, instance_id=INST, active_space_id=SPACE, member_id=MEMBER,
        active_space=MagicMock(), message=SimpleNamespace(platform="repl"), trace=None,
    )


async def test_boundary_compacts_without_the_seeded_head(tmp_path):
    conv = await _log_with_seeds(tmp_path)
    handler = _handler(conv, CompactionState(space_id=SPACE, compaction_threshold=1))
    with patch("kernos.kernel.fact_harvest.harvest_facts",
               new=AsyncMock(return_value={"primary_ok": True})) as harvest:
        await persist.run_boundary(_ctx(handler))
    compacted = handler.compaction.compact_from_log.call_args.args[3]
    assert "NEW turn one" in compacted and "OLD turn" not in compacted
    harvested = harvest.call_args.args[5]
    assert "NEW turn two" in harvested and "OLD turn" not in harvested
    assert handler._compacting == set()


async def test_boundary_harvests_only_what_no_earlier_harvest_covered(tmp_path):
    conv = await _log_with_seeds(tmp_path)
    text, num = await conv.read_current_log_text(INST, SPACE, member_id=MEMBER)
    upto = text.index("[", text.index("NEW turn one"))  # through "NEW turn one"
    await conv.mark_harvested(INST, SPACE, num, upto, member_id=MEMBER)
    handler = _handler(conv, CompactionState(space_id=SPACE, compaction_threshold=1))
    with patch("kernos.kernel.fact_harvest.harvest_facts",
               new=AsyncMock(return_value={"primary_ok": True})) as harvest:
        await persist.run_boundary(_ctx(handler))
    harvested = harvest.call_args.args[5]
    assert "NEW turn two" in harvested and "NEW turn one" not in harvested
    # Compaction still covers the whole new span.
    assert "NEW turn one" in handler.compaction.compact_from_log.call_args.args[3]


async def test_seeds_keep_their_speaker(tmp_path):
    conv = ConversationLogger(data_dir=str(tmp_path))
    await conv.append(INST, SPACE, "system", "receipt", "tool ran", member_id=MEMBER)
    old, _ = await conv.roll_log(INST, SPACE, member_id=MEMBER)
    await conv.seed_from_previous(INST, SPACE, old, tail_entries=1, member_id=MEMBER)
    text, _ = await conv.read_current_log_text(INST, SPACE, member_id=MEMBER)
    assert "[system] [receipt] tool ran" in text


async def test_successful_compaction_clears_failures_durably(tmp_path):
    conv = await _log_with_seeds(tmp_path)
    cs = CompactionState(space_id=SPACE, compaction_threshold=1,
                         consecutive_failures=2, last_compaction_failure_at="")
    handler = _handler(conv, cs)
    with patch("kernos.kernel.fact_harvest.harvest_facts",
               new=AsyncMock(return_value={"primary_ok": True})):
        await persist.run_boundary(_ctx(handler))
    saved = handler.compaction.save_state.call_args.args[2]
    assert saved.consecutive_failures == 0


async def test_guard_is_per_member(tmp_path):
    conv = await _log_with_seeds(tmp_path)
    handler = _handler(conv, CompactionState(space_id=SPACE, compaction_threshold=1))
    handler._compacting.add((SPACE, "mem_b"))  # another member compacting here
    with patch("kernos.kernel.fact_harvest.harvest_facts",
               new=AsyncMock(return_value={"primary_ok": True})):
        await persist.run_boundary(_ctx(handler))
    handler.compaction.compact_from_log.assert_awaited_once()


async def test_harvest_log_tail_advances_the_mark_only_on_success(tmp_path):
    from kernos.kernel.fact_harvest import harvest_log_tail
    conv = await _log_with_seeds(tmp_path)
    handler = SimpleNamespace(conv_logger=conv, reasoning=MagicMock(), state=MagicMock(),
                              events=None, _retrieval=None)
    with patch("kernos.kernel.fact_harvest.harvest_facts",
               new=AsyncMock(return_value={"primary_ok": False})) as harvest:
        await harvest_log_tail(handler, INST, SPACE, member_id=MEMBER)
        assert "OLD turn" not in harvest.call_args.args[5]
    info = await conv.get_current_log_info(INST, SPACE, member_id=MEMBER)
    assert info["harvested_chars"] == 0
    with patch("kernos.kernel.fact_harvest.harvest_facts",
               new=AsyncMock(return_value={"primary_ok": True})):
        await harvest_log_tail(handler, INST, SPACE, member_id=MEMBER)
    info = await conv.get_current_log_info(INST, SPACE, member_id=MEMBER)
    text, _ = await conv.read_current_log_text(INST, SPACE, member_id=MEMBER)
    assert info["harvested_chars"] == len(text)
    with patch("kernos.kernel.fact_harvest.harvest_facts", new=AsyncMock()) as harvest:
        assert await harvest_log_tail(handler, INST, SPACE, member_id=MEMBER) is None
        harvest.assert_not_called()


async def test_persist_run_defers_boundary_work():
    """persist.run must not compact; it only marks the turn for run_boundary."""
    import inspect
    src = inspect.getsource(persist.run)
    assert "compact_from_log" not in src and "harvest_facts" not in src
    assert "boundary_pending = True" in src

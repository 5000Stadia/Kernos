"""Substrate tests for kernos.kernel.external_agents.acpx_adapter.

This module is the actual substrate boundary the harness shims wrap.
ACPX (openclaw/acpx) speaks the Agent Client Protocol — these tests
pin the pieces that don't require the ``acpx`` binary itself:

  * ``derive_session_id`` — deterministic substrate-coordinate hash
  * NDJSON event parsing — ``_parse_ndjson_event`` shape discipline
  * ``_extract_agent_message_chunk`` — ACP envelope variants
  * ``_extract_stop_reason`` — JSON-RPC response detection
  * ``is_acpx_available`` — bring-up probe behavior
  * Alias map — ``claude_code/codex/gemini`` → ACPX agent names

Live ``dispatch`` end-to-end is exercised by the harness shim
TestClaudeLive / TestCodexLive when ``KERNOS_LIVE_AGENT_TESTS=1``.
"""
from __future__ import annotations

import shutil

import pytest

from kernos.kernel.external_agents import acpx_adapter
from kernos.kernel.external_agents.acpx_adapter import (
    SUPPORTED_TARGETS,
    _ParseFailure,
    _extract_agent_message_chunk,
    _extract_stop_reason,
    _parse_ndjson_event,
    derive_session_id,
    is_acpx_available,
)


# ===========================================================================
# derive_session_id — substrate-coordinate hash discipline
# ===========================================================================


class TestDeriveSessionId:
    def test_deterministic_for_same_coordinates(self):
        a = derive_session_id(
            instance_id="kernos-prod",
            target="claude_code",
            member_id="m1",
            conversation_id="conv-42",
        )
        b = derive_session_id(
            instance_id="kernos-prod",
            target="claude_code",
            member_id="m1",
            conversation_id="conv-42",
        )
        assert a == b
        assert a  # not empty

    def test_distinct_for_different_targets(self):
        a = derive_session_id(
            instance_id="i", target="claude_code", member_id="m",
            conversation_id="c",
        )
        b = derive_session_id(
            instance_id="i", target="codex", member_id="m",
            conversation_id="c",
        )
        assert a != b

    def test_distinct_for_different_conversations(self):
        a = derive_session_id(
            instance_id="i", target="claude_code", member_id="m",
            conversation_id="c1",
        )
        b = derive_session_id(
            instance_id="i", target="claude_code", member_id="m",
            conversation_id="c2",
        )
        assert a != b

    def test_distinct_for_different_members(self):
        a = derive_session_id(
            instance_id="i", target="claude_code", member_id="m1",
            conversation_id="c",
        )
        b = derive_session_id(
            instance_id="i", target="claude_code", member_id="m2",
            conversation_id="c",
        )
        assert a != b

    def test_returns_16_char_prefix(self):
        # Architect call: 16-char prefix fits ACPX session storage
        # comfortably and stays operator-readable in logs.
        s = derive_session_id(
            instance_id="kernos-prod",
            target="claude_code",
            member_id="member-1",
            conversation_id="conv-abc",
        )
        assert len(s) == 16
        # Sanitized hex chars only (lowercase 0-9a-f)
        assert all(c in "0123456789abcdef" for c in s)

    def test_blank_member_and_conv_still_produces_id(self):
        # Some dispatches are out-of-conversation (system bring-up,
        # health check). Empty member_id/conversation_id should still
        # produce a valid id, not blow up.
        s = derive_session_id(
            instance_id="kernos-prod", target="claude_code",
        )
        assert len(s) == 16

    def test_empty_instance_and_target_produces_empty_or_constant(self):
        # Defensive: if caller passes nothing meaningful, we still
        # return a string (possibly empty) rather than raise.
        s = derive_session_id(instance_id="", target="")
        assert isinstance(s, str)


# ===========================================================================
# NDJSON parse discipline (Codex review folds #4 + #5)
# ===========================================================================


class TestParseNdjsonEvent:
    def test_parses_valid_json_object(self):
        out = _parse_ndjson_event('{"method": "session/update"}')
        assert out == {"method": "session/update"}

    def test_blank_line_returns_none(self):
        assert _parse_ndjson_event("") is None
        assert _parse_ndjson_event("   ") is None
        assert _parse_ndjson_event("\t\n") is None

    def test_malformed_json_raises_parse_failure(self):
        with pytest.raises(_ParseFailure):
            _parse_ndjson_event("not-json{")

    def test_valid_json_non_dict_raises_parse_failure(self):
        # Fold #4: bare strings/arrays/numbers are valid JSON but
        # wrong shape; downstream .get() would crash.
        with pytest.raises(_ParseFailure):
            _parse_ndjson_event('[1, 2, 3]')
        with pytest.raises(_ParseFailure):
            _parse_ndjson_event('"just a string"')
        with pytest.raises(_ParseFailure):
            _parse_ndjson_event('42')
        with pytest.raises(_ParseFailure):
            _parse_ndjson_event('null')


class TestExtractAgentMessageChunk:
    def test_canonical_acp_session_update_text(self):
        event = {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "hello"},
                },
            },
        }
        assert _extract_agent_message_chunk(event) == "hello"

    def test_acp_session_update_value_variant(self):
        # Some adapters wrap as {value: ...} rather than {text: ...}
        event = {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text_delta", "value": "world"},
                },
            },
        }
        assert _extract_agent_message_chunk(event) == "world"

    def test_forward_compat_top_level_text_delta(self):
        event = {"type": "text_delta", "delta": "frag"}
        assert _extract_agent_message_chunk(event) == "frag"

    def test_unrelated_event_returns_none(self):
        assert _extract_agent_message_chunk(
            {"method": "tool/call", "params": {"x": 1}}
        ) is None
        assert _extract_agent_message_chunk({}) is None

    def test_malformed_nested_shapes_dont_crash(self):
        # Fold #4: each nesting level dict-checked explicitly so
        # malformed envelopes degrade silently instead of crashing
        # the streaming drain task.
        evil_shapes = [
            {"method": "session/update", "params": "not-a-dict"},
            {"method": "session/update", "params": {"update": "not-a-dict"}},
            {"method": "session/update", "params": {"update": {
                "sessionUpdate": "agent_message_chunk",
                "content": "not-a-dict",
            }}},
            {"method": "session/update", "params": {"update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"text": 42},  # text not a string
            }}},
            {"type": "text_delta", "delta": {"nested": "wrong"}},
        ]
        for ev in evil_shapes:
            assert _extract_agent_message_chunk(ev) is None


class TestExtractErrorMessage:
    """2026-05-20 root-cause fix: claude-acp returned JSON-RPC
    error envelopes on stdout (billing_error). Our dispatch was
    blind to them because it only surfaced stderr. Pin the
    extractor that fixes the visibility gap."""

    def test_extracts_message_with_kind(self):
        from kernos.kernel.external_agents.acpx_adapter import (
            _extract_error_message,
        )
        # Exact shape captured from the live failure
        event = {
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": -32603,
                "message": "Internal error: Credit balance is too low",
                "data": {
                    "acpxCode": "RUNTIME",
                    "origin": "cli",
                    "sessionId": "unknown",
                    "errorKind": "billing_error",
                },
            },
        }
        out = _extract_error_message(event)
        assert out is not None
        assert "Credit balance is too low" in out
        assert "[billing_error]" in out

    def test_extracts_message_without_kind(self):
        from kernos.kernel.external_agents.acpx_adapter import (
            _extract_error_message,
        )
        event = {
            "error": {"code": -32603, "message": "Something else broke"},
        }
        out = _extract_error_message(event)
        assert out == "Something else broke"

    def test_no_error_returns_none(self):
        from kernos.kernel.external_agents.acpx_adapter import (
            _extract_error_message,
        )
        # Normal session/update event — no error
        assert _extract_error_message({
            "method": "session/update",
            "params": {"update": {"sessionUpdate": "agent_message_chunk"}},
        }) is None
        assert _extract_error_message({}) is None

    def test_malformed_error_shapes_dont_crash(self):
        from kernos.kernel.external_agents.acpx_adapter import (
            _extract_error_message,
        )
        for evil in [
            {"error": "string-not-dict"},
            {"error": {"code": 1}},  # no message
            {"error": {"message": ""}},  # empty message
            {"error": {"message": 42}},  # non-string message
            {"error": {"message": "ok", "data": "not-dict"}},
            {"error": {"message": "ok", "data": {"errorKind": 99}}},
        ]:
            # Must return either None or a useful string, never crash
            result = _extract_error_message(evil)
            assert result is None or isinstance(result, str)


class TestExtractStopReason:
    def test_recognizes_jsonrpc_result_stop_reason(self):
        event = {"id": 1, "result": {"stopReason": "end_turn"}}
        assert _extract_stop_reason(event) == "end_turn"

    def test_other_stop_reason_values(self):
        for reason in ["max_tokens", "stop_sequence", "tool_use", "refusal"]:
            event = {"result": {"stopReason": reason}}
            assert _extract_stop_reason(event) == reason

    def test_missing_result_returns_none(self):
        assert _extract_stop_reason({"method": "session/update"}) is None

    def test_result_without_stop_reason_returns_none(self):
        assert _extract_stop_reason({"result": {"other": "thing"}}) is None

    def test_non_string_stop_reason_ignored(self):
        # Defensive: a numeric or null stopReason shouldn't be
        # surfaced as if completion happened.
        assert _extract_stop_reason({"result": {"stopReason": 42}}) is None
        assert _extract_stop_reason({"result": {"stopReason": None}}) is None

    def test_empty_string_stop_reason_treated_as_not_complete(self):
        assert _extract_stop_reason({"result": {"stopReason": ""}}) is None


# ===========================================================================
# Bring-up probe — is_acpx_available
# ===========================================================================


class TestIsAcpxAvailable:
    def test_returns_tuple_of_bool_and_string(self):
        ok, detail = is_acpx_available()
        assert isinstance(ok, bool)
        assert isinstance(detail, str)

    def test_returns_false_when_binary_missing(self, monkeypatch):
        # Force the binary lookup to miss
        monkeypatch.setattr(acpx_adapter, "_acpx_binary", lambda: "")
        ok, detail = is_acpx_available()
        assert ok is False
        assert detail  # has a reason string


# ===========================================================================
# Alias map / supported targets
# ===========================================================================


class TestStaleSessionDetection:
    """2026-05-19 live-bug pin: ACPX named sessions can go stale
    when the bound agent process dies. `sessions ensure` reports
    the session "exists" so subsequent dispatch hits stderr
    'agent needs reconnect' with rc=1. There's no `sessions
    reset` — close + re-ensure is the only path. dispatch() now
    auto-retries once on this exact failure shape."""

    def test_marker_detected_in_real_stderr(self):
        from kernos.kernel.external_agents.acpx_adapter import (
            _stderr_indicates_stale_agent,
        )
        # String captured from server.log during the 2026-05-19
        # bug report (path anonymized).
        live_stderr = (
            "[acpx] session 5ffe4c7047de1deb "
            "(6e22e50b-072a-4cb3-9edf-5cc7551197a0) · "
            "/home/user/Kernos-main · agent needs reconnect\n"
        )
        assert _stderr_indicates_stale_agent(live_stderr) is True

    def test_marker_case_insensitive(self):
        from kernos.kernel.external_agents.acpx_adapter import (
            _stderr_indicates_stale_agent,
        )
        assert _stderr_indicates_stale_agent(
            "AGENT NEEDS RECONNECT"
        ) is True

    def test_alternative_marker(self):
        from kernos.kernel.external_agents.acpx_adapter import (
            _stderr_indicates_stale_agent,
        )
        assert _stderr_indicates_stale_agent(
            "[acpx] agent disconnected from session foo"
        ) is True

    def test_empty_stderr_not_detected(self):
        from kernos.kernel.external_agents.acpx_adapter import (
            _stderr_indicates_stale_agent,
        )
        assert _stderr_indicates_stale_agent("") is False

    def test_unrelated_error_not_detected(self):
        from kernos.kernel.external_agents.acpx_adapter import (
            _stderr_indicates_stale_agent,
        )
        # Must NOT false-positive on generic errors
        assert _stderr_indicates_stale_agent(
            "connection refused"
        ) is False
        assert _stderr_indicates_stale_agent(
            "Error: missing required argument"
        ) is False
        assert _stderr_indicates_stale_agent(
            "permission denied"
        ) is False


class TestValidateConsultInput:
    """Behavior tests for the pure ``validate_consult_input`` helper.

    Replaces the prior static-source tests (which Codex flagged as
    weak — they only proved literal strings existed in reasoning.py,
    not that the validation behaved). The helper is pure, so tests
    call it directly with input dicts and assert on outputs.

    Origin: 2026-05-20 live agent failed schema validation silently —
    `consult(harness="claude_code")` (missing question) reached acpx
    and exited rc=2. Handler-side validation catches this before
    dispatch. See ``validate_consult_input`` in tool.py.
    """

    def test_valid_input_returns_tuple(self):
        from kernos.kernel.external_agents.tool import (
            validate_consult_input,
        )
        result = validate_consult_input(
            {"harness": "codex", "question": "What is 2+2?"},
        )
        assert result == ("codex", "What is 2+2?")

    def test_empty_harness_returns_error(self):
        # A genuinely empty harness still surfaces the clean error (no blind
        # default) — only a clear prompt-in-harness or alias is recovered.
        from kernos.kernel.external_agents.tool import validate_consult_input
        result = validate_consult_input({"harness": "", "question": "x"})
        assert isinstance(result, dict) and result["error"] == "InvalidConsultCall"

    def test_missing_harness_returns_error(self):
        from kernos.kernel.external_agents.tool import validate_consult_input
        result = validate_consult_input({"question": "x"})
        assert isinstance(result, dict) and result["error"] == "InvalidConsultCall"

    def test_label_harness_with_real_question_defaults(self):
        # TOOL-ARG-REPAIR-V1 §2.2 role-based: the live rerun shape — harness
        # is the TASK NAME ("synchronous_consult"), question is real → the
        # harness was a label; default to codex instead of failing the step.
        from kernos.kernel.external_agents.tool import validate_consult_input
        result = validate_consult_input(
            {"harness": "synchronous_consult",
             "question": "Reply with the word hello."},
        )
        assert result == ("codex", "Reply with the word hello.")
        # Same for an arbitrary short label.
        assert validate_consult_input({"harness": "xyz", "question": "x"}) == ("codex", "x")

    def test_explicit_unsupported_agent_still_errors(self):
        # Spec §3 risk boundary: a harness naming a KNOWN other agent must
        # hard-fail, never silently route to codex.
        from kernos.kernel.external_agents.tool import validate_consult_input
        for agent in ("aider", "cursor", "Perplexity", "swe-agent"):
            result = validate_consult_input({"harness": agent, "question": "x"})
            assert isinstance(result, dict), agent
            assert result["error"] == "InvalidConsultCall"

    def test_denylist_gates_all_recovery_branches(self):
        # Codex review P2: the denylist must apply BEFORE swap and
        # prompt-in-harness recovery — these two shapes previously slipped
        # through and silently rerouted an explicit other-agent request.
        from kernos.kernel.external_agents.tool import validate_consult_input
        # Swap-bypass: question canonicalizes to a valid harness.
        result = validate_consult_input({"harness": "aider", "question": "codex"})
        assert isinstance(result, dict) and result["error"] == "InvalidConsultCall"
        # Prompt-in-harness bypass: spaced denylisted name, no question.
        result = validate_consult_input({"harness": "swe agent"})
        assert isinstance(result, dict) and result["error"] == "InvalidConsultCall"

    def test_label_harness_without_question_still_errors(self):
        # A short label and NO question: nothing to run — clean error.
        from kernos.kernel.external_agents.tool import validate_consult_input
        result = validate_consult_input({"harness": "synchronous_consult"})
        assert isinstance(result, dict) and result["error"] == "InvalidConsultCall"

    def test_near_miss_harness_aliases_map_to_canonical(self):
        from kernos.kernel.external_agents.tool import validate_consult_input
        assert validate_consult_input({"harness": "cc", "question": "x"}) == ("claude_code", "x")
        assert validate_consult_input({"harness": "claude", "question": "x"}) == ("claude_code", "x")
        assert validate_consult_input({"harness": "gpt", "question": "x"}) == ("codex", "x")
        assert validate_consult_input({"harness": "CODEX", "question": "x"}) == ("codex", "x")

    def test_prompt_in_harness_no_question_recovered(self):
        # The exact Test 16 fumble: the prompt was passed as `harness`, no
        # valid harness anywhere → use the text as the question, default codex.
        from kernos.kernel.external_agents.tool import validate_consult_input
        prompt = "Summarize the intent of daily mode in self_update.py"
        assert validate_consult_input({"harness": prompt}) == ("codex", prompt)

    def test_swapped_harness_and_question_recovered(self):
        # harness holds the prompt, question holds the harness name → unswap.
        from kernos.kernel.external_agents.tool import validate_consult_input
        prompt = "Explain the dispatch gate in two sentences."
        assert validate_consult_input({"harness": prompt, "question": "codex"}) == ("codex", prompt)

    def test_empty_question_returns_error(self):
        from kernos.kernel.external_agents.tool import (
            validate_consult_input,
        )
        result = validate_consult_input(
            {"harness": "codex", "question": ""},
        )
        assert isinstance(result, dict)
        assert "question" in result["message"]

    def test_missing_question_returns_error(self):
        from kernos.kernel.external_agents.tool import (
            validate_consult_input,
        )
        result = validate_consult_input({"harness": "codex"})
        assert isinstance(result, dict)
        assert "question" in result["message"]

    def test_prompt_alias_accepted(self):
        """Live 2026-05-20: agent used `prompt` instead of
        `question`. Single alias preserves intent without
        speculation."""
        from kernos.kernel.external_agents.tool import (
            validate_consult_input,
        )
        result = validate_consult_input(
            {"harness": "codex", "prompt": "hello"},
        )
        assert result == ("codex", "hello")

    def test_question_wins_when_both_supplied_with_same_value(self):
        from kernos.kernel.external_agents.tool import (
            validate_consult_input,
        )
        result = validate_consult_input(
            {"harness": "codex", "question": "x", "prompt": "x"},
        )
        assert result == ("codex", "x")

    def test_conflicting_question_and_prompt_refused(self):
        """Codex audit: alias fallback that silently masks
        conflicting values is dangerous — refuse instead."""
        from kernos.kernel.external_agents.tool import (
            validate_consult_input,
        )
        result = validate_consult_input(
            {"harness": "codex", "question": "A", "prompt": "B"},
        )
        assert isinstance(result, dict)
        assert result["error"] == "InvalidConsultCall"
        assert "different" in result["message"].lower()

    def test_speculative_alias_target_rejected(self):
        """`target` was a speculative alias — dropped per Codex
        audit. Confirm it now produces InvalidConsultCall instead
        of silently working."""
        from kernos.kernel.external_agents.tool import (
            validate_consult_input,
        )
        result = validate_consult_input(
            {"target": "codex", "question": "x"},
        )
        assert isinstance(result, dict)
        assert result["error"] == "InvalidConsultCall"

    def test_speculative_alias_text_rejected(self):
        """`text` was a speculative alias — dropped per Codex audit."""
        from kernos.kernel.external_agents.tool import (
            validate_consult_input,
        )
        result = validate_consult_input(
            {"harness": "codex", "text": "x"},
        )
        assert isinstance(result, dict)
        assert result["error"] == "InvalidConsultCall"

    def test_non_string_harness_does_not_crash(self):
        """Codex audit: prior implementation did .strip() on the
        get-result, which crashed on int/list/None values. Coerce
        to "" instead so the caller sees a clean InvalidConsultCall."""
        from kernos.kernel.external_agents.tool import (
            validate_consult_input,
        )
        result = validate_consult_input(
            {"harness": 42, "question": "x"},
        )
        assert isinstance(result, dict)
        assert result["error"] == "InvalidConsultCall"

    def test_non_string_question_does_not_crash(self):
        from kernos.kernel.external_agents.tool import (
            validate_consult_input,
        )
        result = validate_consult_input(
            {"harness": "codex", "question": ["hello"]},
        )
        assert isinstance(result, dict)
        assert result["error"] == "InvalidConsultCall"

    def test_whitespace_only_harness_treated_as_empty(self):
        from kernos.kernel.external_agents.tool import (
            validate_consult_input,
        )
        result = validate_consult_input(
            {"harness": "   ", "question": "x"},
        )
        assert isinstance(result, dict)
        assert result["error"] == "InvalidConsultCall"

    def test_none_input_does_not_crash(self):
        from kernos.kernel.external_agents.tool import (
            validate_consult_input,
        )
        result = validate_consult_input(None)
        assert isinstance(result, dict)
        assert result["error"] == "InvalidConsultCall"

    def test_string_input_does_not_crash(self):
        """Codex audit round 2: ``tool_input or {}`` only catches
        None; a stray string passed by a buggy caller used to hit
        ``.get()`` on a str. Now coerced to empty dict."""
        from kernos.kernel.external_agents.tool import (
            validate_consult_input,
        )
        result = validate_consult_input("oops")  # type: ignore[arg-type]
        assert isinstance(result, dict)
        assert result["error"] == "InvalidConsultCall"

    def test_int_input_does_not_crash(self):
        from kernos.kernel.external_agents.tool import (
            validate_consult_input,
        )
        result = validate_consult_input(3)  # type: ignore[arg-type]
        assert isinstance(result, dict)
        assert result["error"] == "InvalidConsultCall"

    def test_list_input_does_not_crash(self):
        from kernos.kernel.external_agents.tool import (
            validate_consult_input,
        )
        result = validate_consult_input(["harness", "question"])  # type: ignore[arg-type]
        assert isinstance(result, dict)
        assert result["error"] == "InvalidConsultCall"


class TestSupportedConsultHarnesses:
    """The shared constant feeds the schema enum AND the handler
    validator — verifying it stays the one source of truth."""

    def test_schema_enum_matches_constant(self):
        from kernos.kernel.external_agents.tool import (
            CONSULT_TOOL, SUPPORTED_CONSULT_HARNESSES,
        )
        enum = CONSULT_TOOL["input_schema"]["properties"]["harness"]["enum"]
        assert tuple(enum) == SUPPORTED_CONSULT_HARNESSES

    def test_constant_has_three_canonical_harnesses(self):
        from kernos.kernel.external_agents.tool import (
            SUPPORTED_CONSULT_HARNESSES,
        )
        assert "claude_code" in SUPPORTED_CONSULT_HARNESSES
        assert "codex" in SUPPORTED_CONSULT_HARNESSES
        assert "gemini" in SUPPORTED_CONSULT_HARNESSES


class TestConsultSchemaValidation:
    """2026-05-19 live-bug pin: agent called consult with
    harness='' (empty string). JSON schema accepted because string
    type allows empty. Now harness + question are minLength: 1 so
    schema-validation layer rejects empty before dispatch reaches
    the registry."""

    def test_harness_field_has_min_length_1(self):
        from kernos.kernel.external_agents.tool import CONSULT_TOOL
        props = CONSULT_TOOL["input_schema"]["properties"]
        assert props["harness"].get("minLength") == 1

    def test_question_field_has_min_length_1(self):
        from kernos.kernel.external_agents.tool import CONSULT_TOOL
        props = CONSULT_TOOL["input_schema"]["properties"]
        assert props["question"].get("minLength") == 1

    def test_harness_description_advertises_examples(self):
        """Description should name actual harnesses so the agent
        knows what to pass instead of guessing or leaving empty."""
        from kernos.kernel.external_agents.tool import CONSULT_TOOL
        props = CONSULT_TOOL["input_schema"]["properties"]
        desc = props["harness"]["description"]
        for name in ("claude_code", "codex", "gemini"):
            assert name in desc

    def test_harness_enum_locks_valid_values(self):
        """2026-05-20 founder push: 'How do we get kernos to freaking
        talk to codex and claude code'. The live agent kept fumbling
        with empty harness, wrong tool names (external_agent_consult.cc,
        codex_async_advisory). enum on the harness field hard-rejects
        bad values at schema-validation time, before the call dispatches.
        """
        from kernos.kernel.external_agents.tool import CONSULT_TOOL
        props = CONSULT_TOOL["input_schema"]["properties"]
        enum = props["harness"].get("enum")
        assert enum == ["claude_code", "codex", "gemini"], (
            f"harness enum should lock to the three supported "
            f"agent-callable harnesses; got {enum!r}"
        )

    def test_consult_description_names_specific_call_shape(self):
        """Tool-name hallucination guard: description must explicitly
        show the right call shape so the model has examples to
        anchor on instead of inventing names like
        'external_agent_consult.cc' or 'codex_async_advisory'."""
        from kernos.kernel.external_agents.tool import CONSULT_TOOL
        desc = CONSULT_TOOL["description"]
        # Concrete call shape
        assert 'consult(harness="codex"' in desc
        assert 'consult(harness="claude_code"' in desc
        # Negative examples (what NOT to do)
        assert "external_agent_consult.cc" in desc
        assert "codex_async_advisory" in desc


class TestSupportedTargets:
    def test_includes_three_canonical_names(self):
        assert "claude_code" in SUPPORTED_TARGETS
        assert "codex" in SUPPORTED_TARGETS
        assert "gemini" in SUPPORTED_TARGETS

    def test_supported_targets_are_strings(self):
        assert all(isinstance(t, str) for t in SUPPORTED_TARGETS)


# ===========================================================================
# Descendant reaping — closes the codex-acp orphan leak
# ===========================================================================


class TestCollectDescendants:
    """``_collect_descendants`` walks ``/proc/<pid>/task/<tid>/children``
    to enumerate the full process tree under a root PID. Required
    because ``npm exec`` calls ``setsid`` and the leaves escape our
    process group — killpg can't reach them.
    """

    def test_returns_self_descendants_via_subprocess(self):
        import subprocess
        from kernos.kernel.external_agents.acpx_adapter import (
            _collect_descendants,
        )

        # Spawn `sh -c "sleep 30 & sleep 30 & wait"` so we have a
        # deterministic descendant tree we can observe.
        proc = subprocess.Popen(
            ["sh", "-c", "sleep 30 & sleep 30 & wait"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            # Give the shell a moment to fork its children
            import time
            time.sleep(0.3)
            descendants = _collect_descendants(proc.pid)
            # Should see at least the two `sleep` children
            assert len(descendants) >= 2, (
                f"expected >=2 descendants, got {descendants}"
            )
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            # Clean up any straggler sleeps
            import os, signal
            for pid in descendants if 'descendants' in dir() else []:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    def test_returns_empty_for_nonexistent_pid(self):
        from kernos.kernel.external_agents.acpx_adapter import (
            _collect_descendants,
        )
        # A PID very unlikely to exist
        assert _collect_descendants(9_999_999) == []

    def test_returns_empty_for_leaf_process(self):
        import subprocess
        from kernos.kernel.external_agents.acpx_adapter import (
            _collect_descendants,
        )
        # `sleep` with no children → empty descendant set
        proc = subprocess.Popen(
            ["sleep", "30"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            import time
            time.sleep(0.1)
            assert _collect_descendants(proc.pid) == []
        finally:
            proc.terminate()
            proc.wait(timeout=5)


class TestKillTree:
    """``_kill_tree`` SIGKILLs every descendant of a root PID and
    returns the count signaled. Used in the dispatch teardown to
    reap codex-acp / claude-acp grandchildren that escaped the
    process group."""

    def test_kills_descendants_and_returns_count(self):
        import subprocess, time, os
        from kernos.kernel.external_agents.acpx_adapter import (
            _kill_tree, _collect_descendants,
        )
        # sh with two sleep grandchildren we want killed
        proc = subprocess.Popen(
            ["sh", "-c", "sleep 30 & sleep 30 & wait"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(0.3)
            descendants_before = _collect_descendants(proc.pid)
            assert len(descendants_before) >= 2
            killed = _kill_tree(proc.pid)
            assert killed >= 2
            # Give the kernel a moment to reap
            time.sleep(0.2)
            # Now confirm the descendants are gone
            for pid in descendants_before:
                # Either truly gone or zombie waiting for parent
                try:
                    # SIGCONT to test existence; ESRCH means gone
                    os.kill(pid, 0)
                    # Still there — must be a zombie (status Z)
                    with open(f"/proc/{pid}/status") as f:
                        state_line = next(
                            l for l in f if l.startswith("State:")
                        )
                    assert "Z" in state_line, (
                        f"PID {pid} survived kill: {state_line}"
                    )
                except (ProcessLookupError, OSError, FileNotFoundError):
                    pass  # gone, expected
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_returns_zero_for_leaf_pid(self):
        import subprocess, time
        from kernos.kernel.external_agents.acpx_adapter import _kill_tree
        proc = subprocess.Popen(
            ["sleep", "30"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(0.1)
            assert _kill_tree(proc.pid) == 0
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_handles_nonexistent_pid_gracefully(self):
        from kernos.kernel.external_agents.acpx_adapter import _kill_tree
        assert _kill_tree(9_999_999) == 0


# ===========================================================================
# _read_lines_unbounded — ACPX-DRAIN-OVERRUN-FIX-V1 (2026-05-24)
#
# Repro for the bug Kernos hit on 2026-05-24 during a spec alignment
# check: claude_code emitted a JSON-RPC event with inlined spec content
# that exceeded asyncio's default 64 KiB per-line limit, the stdout
# drain raised LimitOverrunError, drain_incomplete was set, and the
# substrate surfaced ConsultationFailed. The helper sidesteps the
# limit by reading bytes in chunks and splitting on b"\n" manually.
# ===========================================================================


class TestReadLinesUnbounded:
    @pytest.mark.asyncio
    async def test_yields_normal_lines_with_trailing_newline(self):
        """Short lines yielded one at a time, each with trailing \\n."""
        import asyncio
        from kernos.kernel.external_agents.acpx_adapter import (
            _read_lines_unbounded,
        )
        reader = asyncio.StreamReader()
        reader.feed_data(b"hello\nworld\n")
        reader.feed_eof()
        lines = [line async for line in _read_lines_unbounded(reader)]
        assert lines == [b"hello\n", b"world\n"]

    @pytest.mark.asyncio
    async def test_handles_line_over_64kib_without_raising(self):
        """The bug repro: a single line exceeding the default 64 KiB
        StreamReader limit. async-for-line raises LimitOverrunError;
        the helper handles it cleanly."""
        import asyncio
        from kernos.kernel.external_agents.acpx_adapter import (
            _read_lines_unbounded,
        )
        # 100 KiB of content followed by a newline → one giant line.
        giant_payload = b"X" * (100 * 1024)
        reader = asyncio.StreamReader()
        reader.feed_data(giant_payload + b"\n")
        reader.feed_eof()
        lines = [line async for line in _read_lines_unbounded(reader)]
        assert len(lines) == 1
        assert lines[0] == giant_payload + b"\n"
        assert len(lines[0]) == (100 * 1024) + 1

    @pytest.mark.asyncio
    async def test_handles_eof_mid_line_yields_residual(self):
        """If the stream ends without a trailing newline, the
        partial last line is still yielded so content isn't dropped."""
        import asyncio
        from kernos.kernel.external_agents.acpx_adapter import (
            _read_lines_unbounded,
        )
        reader = asyncio.StreamReader()
        reader.feed_data(b"complete\nresidual-no-newline")
        reader.feed_eof()
        lines = [line async for line in _read_lines_unbounded(reader)]
        assert lines == [b"complete\n", b"residual-no-newline"]

    @pytest.mark.asyncio
    async def test_handles_none_reader_returns_immediately(self):
        """None reader (e.g., process started with stdout=None)
        produces zero lines without raising."""
        from kernos.kernel.external_agents.acpx_adapter import (
            _read_lines_unbounded,
        )
        lines = [line async for line in _read_lines_unbounded(None)]
        assert lines == []

    @pytest.mark.asyncio
    async def test_handles_empty_stream(self):
        """EOF immediately with no bytes produces zero lines."""
        import asyncio
        from kernos.kernel.external_agents.acpx_adapter import (
            _read_lines_unbounded,
        )
        reader = asyncio.StreamReader()
        reader.feed_eof()
        lines = [line async for line in _read_lines_unbounded(reader)]
        assert lines == []

    @pytest.mark.asyncio
    async def test_line_split_across_multiple_read_chunks(self):
        """A line larger than chunk_size accumulates across multiple
        read() calls in the buffer before being yielded."""
        import asyncio
        from kernos.kernel.external_agents.acpx_adapter import (
            _read_lines_unbounded,
        )
        # Force small chunk_size to verify cross-chunk accumulation.
        reader = asyncio.StreamReader()
        reader.feed_data(b"abc" * 100 + b"\n")  # 300-byte line
        reader.feed_eof()
        lines = [
            line async for line in _read_lines_unbounded(
                reader, chunk_size=64,
            )
        ]
        assert lines == [b"abc" * 100 + b"\n"]

    @pytest.mark.asyncio
    async def test_mixed_small_and_giant_lines(self):
        """Realistic ACPX shape: many small JSON-RPC events
        interspersed with one giant agent_message_chunk that has
        inlined file contents."""
        import asyncio
        from kernos.kernel.external_agents.acpx_adapter import (
            _read_lines_unbounded,
        )
        small = b'{"jsonrpc":"2.0","method":"ping"}\n'
        giant = b"X" * (80 * 1024) + b"\n"
        reader = asyncio.StreamReader()
        reader.feed_data(small + giant + small + small)
        reader.feed_eof()
        lines = [line async for line in _read_lines_unbounded(reader)]
        assert lines == [small, giant, small, small]

    def test_spaced_harness_names_map_not_treated_as_prompt(self):
        # Codex P2: "claude code"/"gpt 5"/"gemini pro" have a space but ARE
        # harness names — squash separators and map them, don't spend a consult
        # on the text as a prompt.
        from kernos.kernel.external_agents.tool import validate_consult_input
        assert validate_consult_input({"harness": "claude code", "question": "x"}) == ("claude_code", "x")
        assert validate_consult_input({"harness": "gpt 5", "question": "x"}) == ("codex", "x")
        assert validate_consult_input({"harness": "gemini-pro", "question": "x"}) == ("gemini", "x")
        # ...and with no question they still resolve the harness, then surface the
        # clean missing-question error (not a junk prompt).
        r = validate_consult_input({"harness": "claude code"})
        assert isinstance(r, dict) and "question" in r["message"]

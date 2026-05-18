"""C7 wiring tests — agent-facing tool surface.

C7 of EXTERNAL-AGENT-CONSULTATION v1 wires the substrate to the
agent-visible tool registry. These tests verify the wiring without
spawning a real subprocess: registry membership, schema shape,
service singleton lifecycle, calling-context propagation, and
backend-param flow into ``execute_code``.

End-to-end live tests (KERNOS_LIVE_AGENT_TESTS=1) cover the actual
subprocess path; this suite is the "wiring exists and is correct"
unit layer.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from kernos.kernel.code_exec import EXECUTE_CODE_TOOL
from kernos.kernel.external_agents.errors import ReentrancyBlocked
from kernos.kernel.external_agents.harness import ConsultResult
from kernos.kernel.external_agents.reentrancy import (
    CallingContext,
    current_calling_context,
    set_calling_context,
    reset_calling_context,
)
from kernos.kernel.external_agents.tool import (
    CONSULT_TOOL,
    ExternalAgentService,
    get_service,
    reset_service_for_tests,
)


# ---------------------------------------------------------------------------
# Tool schema shape
# ---------------------------------------------------------------------------


class TestConsultToolSchema:
    def test_consult_tool_has_required_fields(self):
        assert CONSULT_TOOL["name"] == "consult"
        assert "description" in CONSULT_TOOL
        schema = CONSULT_TOOL["input_schema"]
        assert schema["type"] == "object"
        props = schema["properties"]
        assert "harness" in props
        assert "question" in props
        assert set(schema["required"]) == {"harness", "question"}

    def test_harness_param_is_free_form_string(self):
        # 2026-05-17 contract change: harness is no longer a hardcoded
        # enum on the schema. The registry is dynamic / operator-
        # extensible; the agent passes a free-form string validated at
        # dispatch time against HarnessRegistry. Unknown names return a
        # clear error listing currently registered harnesses, so the
        # agent self-discovers without the schema baking in a closed
        # list. Aider-blocking still happens at the registry level
        # (entry.consult_supported=False), not at the schema layer.
        harness_prop = (
            CONSULT_TOOL["input_schema"]["properties"]["harness"]
        )
        assert harness_prop["type"] == "string"
        assert "enum" not in harness_prop, (
            "harness param must NOT carry a hardcoded enum — registry "
            "is dynamic and the enum would block future-installed "
            "harnesses from being addressable from the agent surface"
        )

    def test_optional_fields_present(self):
        props = CONSULT_TOOL["input_schema"]["properties"]
        for key in ("context", "session_id", "workspace_dir",
                    "timeout_seconds"):
            assert key in props, f"missing {key} in consult schema"


class TestExecuteCodeBackendParam:
    def test_backend_field_in_schema(self):
        # AC8 wiring: backend must be reachable from the agent.
        props = EXECUTE_CODE_TOOL["input_schema"]["properties"]
        assert "backend" in props
        assert "enum" in props["backend"]
        assert "aider" in props["backend"]["enum"]
        assert "native" in props["backend"]["enum"]


# ---------------------------------------------------------------------------
# Service singleton lifecycle
# ---------------------------------------------------------------------------


class TestServiceSingleton:
    @pytest.fixture(autouse=True)
    async def _clean(self):
        await reset_service_for_tests()
        yield
        await reset_service_for_tests()

    async def test_get_service_returns_singleton(self, tmp_path):
        os.environ.pop("KERNOS_EXTERNAL_AGENT_ALLOWLIST", None)
        s1 = await get_service(data_dir=str(tmp_path))
        s2 = await get_service(data_dir=str(tmp_path))
        assert s1 is s2

    async def test_service_registers_default_harnesses(self, tmp_path):
        os.environ.pop("KERNOS_EXTERNAL_AGENT_ALLOWLIST", None)
        svc = await get_service(data_dir=str(tmp_path))
        consult_names = svc.registry.list_consult_harnesses()
        assert set(consult_names) == {"claude_code", "codex", "gemini"}
        build_names = svc.registry.list_build_harnesses()
        assert "aider" in build_names

    async def test_service_starts_consultation_log(self, tmp_path):
        os.environ.pop("KERNOS_EXTERNAL_AGENT_ALLOWLIST", None)
        svc = await get_service(data_dir=str(tmp_path))
        # Log is started — begin() must not raise about un-started DB.
        # Use a mock harness path: just check the log object is wired.
        assert svc.log is not None
        assert svc.orchestrator is not None

    async def test_allowlist_env_picked_up(self, tmp_path):
        os.environ["KERNOS_EXTERNAL_AGENT_ALLOWLIST"] = str(tmp_path)
        try:
            svc = await get_service(data_dir=str(tmp_path))
            policy = svc.orchestrator._policy
            assert len(policy.allowlist) == 1
            assert policy.allowlist[0] == tmp_path.resolve()
        finally:
            os.environ.pop("KERNOS_EXTERNAL_AGENT_ALLOWLIST", None)


# ---------------------------------------------------------------------------
# Calling-context wiring
# ---------------------------------------------------------------------------


class TestCallingContext:
    async def test_default_is_unknown(self):
        # Outside any wired entry point, contextvar default applies.
        # (Cannot assert UNKNOWN globally — other tests may have set
        # it. Just verify the API is reachable.)
        ctx = current_calling_context()
        assert isinstance(ctx, CallingContext)

    async def test_set_and_reset(self):
        token = set_calling_context(CallingContext.CONVERSATIONAL)
        try:
            assert current_calling_context() == CallingContext.CONVERSATIONAL
        finally:
            reset_calling_context(token)

    async def test_engine_wires_conversational(self, tmp_path):
        # TaskEngine.execute should set CONVERSATIONAL while reasoning
        # runs, so a consult call from within the agent's tool loop
        # passes the reentrancy gate.
        from kernos.kernel.engine import TaskEngine
        from kernos.kernel.task import Task, TaskType
        from kernos.kernel.reasoning import ReasoningRequest, ReasoningResult

        captured: dict = {}

        class _StubReasoning:
            async def reason(self, request):
                captured["ctx"] = current_calling_context()
                return ReasoningResult(
                    text="ok", model="stub", input_tokens=1,
                    output_tokens=1, estimated_cost_usd=0.0,
                    duration_ms=1, tool_iterations=0,
                )

        engine = TaskEngine(reasoning=_StubReasoning(), events=None)
        task = Task(
            id="t1", instance_id="i1",
            conversation_id="c1",
            type=TaskType.REACTIVE_SIMPLE,
            source="test",
        )
        request = ReasoningRequest(
            instance_id="i1",
            conversation_id="c1",
            system_prompt="",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            model="stub",
            trigger="user_message",
        )
        # Set an UNKNOWN context first so we can verify the engine
        # actually changes it (rather than reading whatever leaked
        # in from the test runner).
        outer_token = set_calling_context(CallingContext.UNKNOWN)
        try:
            await engine.execute(task, request)
            assert captured["ctx"] == CallingContext.CONVERSATIONAL
            # Context resets after engine.execute returns.
            assert current_calling_context() == CallingContext.UNKNOWN
        finally:
            reset_calling_context(outer_token)

    async def test_unknown_context_blocks_consult(self, tmp_path):
        # Sanity: outside an entry-point context, the consult guard
        # rejects with ReentrancyBlocked. This is what protects all
        # the non-conversational call sites.
        from kernos.kernel.external_agents.reentrancy import enter_consult
        token = set_calling_context(CallingContext.UNKNOWN)
        try:
            with pytest.raises(ReentrancyBlocked):
                enter_consult()
        finally:
            reset_calling_context(token)

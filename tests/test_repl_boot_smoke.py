"""Smoke tests for ``kernos.repl.build_dev_handler``.

The unit-test ``_make_handler`` helper bypasses the production boot
path entirely (mocks every store, every service, every registry).
That's appropriate for unit tests — but it leaves a class of
boot-time issues invisible to the test suite. ``build_dev_handler``
is the real boot used by both ``server.py`` (Discord) and
``repl.py`` (stdin); these smoke tests exercise it with a mock
provider so:

* CC catches boot-time issues that don't surface until a turn is
  actually processed (import cycles, dataclass misuse, missing
  service wiring, etc.) without spending real LLM tokens.
* The REPL the founder uses for soak is exercised in CI, so a
  regression that breaks the dev-soak path fails the build.

These tests also validate the load-bearing CCV1 invariant from
the **real boot** (not the mocked one): the substrate reaches the
model-call seam end-to-end through ``build_dev_handler``'s wiring.
A passing smoke test is structural protection that the production
boot path preserves substrate fidelity.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from kernos.kernel.reasoning import ContentBlock, ProviderResponse


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _resp(text: str = "ok") -> ProviderResponse:
    return ProviderResponse(
        content=[ContentBlock(type="text", text=text)],
        stop_reason="end_turn",
        input_tokens=10,
        output_tokens=20,
    )


def _make_chain_with_mock_provider() -> tuple:
    """Build a primary-chain entry whose Provider is a deterministic
    AsyncMock. Returns (chains_dict, mock_provider) so tests can
    inspect the captured provider call args."""
    from kernos.kernel.reasoning import Provider
    from kernos.providers.base import ChainEntry
    mock_provider = AsyncMock(spec=Provider)
    mock_provider.complete.return_value = _resp("ok")
    entry = ChainEntry(provider=mock_provider, model="claude-sonnet-4-6")
    chains = {"primary": [entry], "simple": [entry], "cheap": [entry]}
    return chains, mock_provider


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Each test gets a tmpdir and a clean env so the REPL boot
    doesn't reach into the real ./data, ./data-dev, ./secrets, etc.
    Tests await ``shutdown_dev_handler(handler)`` explicitly to
    drain the event-stream writer + close instance_db before
    pytest moves on; otherwise leaked tasks cascade-fail subsequent
    tests in the same session."""
    monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path / "data-dev"))
    monkeypatch.setenv("KERNOS_INSTANCE_ID", "repl:smoke")
    monkeypatch.setenv("KERNOS_SECRETS_DIR", str(tmp_path / "secrets-dev"))
    monkeypatch.setenv("KERNOS_STORE_BACKEND", "json")
    monkeypatch.setenv("KERNOS_USE_DECOUPLED_TURN_RUNNER", "1")
    return tmp_path


# ---------------------------------------------------------------------------
# Smoke: boot itself
# ---------------------------------------------------------------------------


async def test_build_dev_handler_returns_a_handler(isolated_env):
    """The boot completes without raising and returns a handler the
    caller can call ``.process()`` on. Catches import cycles, missing
    deps, signature drift in any of the construction sites."""
    from kernos.repl import build_dev_handler, shutdown_dev_handler

    chains, _mock_provider = _make_chain_with_mock_provider()
    with patch(
        "kernos.providers.chains.build_chains_from_env",
        return_value=(chains, None),
    ):
        handler = await build_dev_handler()
    try:
        assert handler is not None
        assert hasattr(handler, "process"), (
            "build_dev_handler must return a MessageHandler-shaped object "
            "with a .process() coroutine"
        )
        assert hasattr(handler, "reasoning"), (
            "MessageHandler must have a wired reasoning service after boot"
        )
        # _instance_db wiring is a CCV1-era requirement (provision phase
        # reads member_profile from this db); pin it.
        assert handler._instance_db is not None, (
            "build_dev_handler must wire _instance_db on the handler"
        )
    finally:
        await shutdown_dev_handler(handler)


async def test_build_dev_handler_wires_decoupled_turn_runner(isolated_env):
    """The CCV1 substrate-fidelity invariant lives on the decoupled
    path. Pin: build_dev_handler wires turn_runner_provider so
    ReasoningService routes through the per-turn TurnRunner factory
    when KERNOS_USE_DECOUPLED_TURN_RUNNER=1."""
    from kernos.repl import build_dev_handler, shutdown_dev_handler

    chains, _mock_provider = _make_chain_with_mock_provider()
    with patch(
        "kernos.providers.chains.build_chains_from_env",
        return_value=(chains, None),
    ):
        handler = await build_dev_handler(decoupled=True)
    try:
        # The reasoning service should have a turn_runner_provider closure
        # wired (this is the per-turn factory build_dev_handler constructs
        # mirroring server.py:_build_per_turn_runner).
        assert handler.reasoning._turn_runner_provider is not None, (
            "decoupled boot must wire turn_runner_provider on ReasoningService"
        )
    finally:
        await shutdown_dev_handler(handler)


# ---------------------------------------------------------------------------
# Smoke: a full turn through the boot reaches the substrate seam
# ---------------------------------------------------------------------------


async def test_dev_handler_processes_a_message_end_to_end(isolated_env):
    """The built handler can run handler.process(message) end-to-end
    against a mock provider. Catches turn-pipeline regressions in
    the real boot that the unit-test mock fixture wouldn't catch
    (e.g., a phase-import cycle, a missing _instance_db method)."""
    from kernos.messages.models import AuthLevel, NormalizedMessage
    from kernos.repl import build_dev_handler, shutdown_dev_handler

    chains, mock_provider = _make_chain_with_mock_provider()
    with patch(
        "kernos.providers.chains.build_chains_from_env",
        return_value=(chains, None),
    ):
        handler = await build_dev_handler(sender="smoke-tester")
    try:
        message = NormalizedMessage(
            content="Hello, smoke test.",
            sender="smoke-tester",
            sender_auth_level=AuthLevel.owner_verified,
            platform="repl",
            platform_capabilities=["text"],
            conversation_id="smoke-tester",
            timestamp=datetime.now(timezone.utc),
            instance_id="repl:smoke",
        )
        response = await handler.process(message)

        assert isinstance(response, str), "handler.process should return a string"
        assert mock_provider.complete.called, (
            "the boot must wire reasoning all the way through to the model "
            "provider — provider.complete should have been invoked"
        )
    finally:
        await shutdown_dev_handler(handler)


async def test_dev_handler_substrate_reaches_model_call(isolated_env):
    """End-to-end CCV1 substrate-fidelity smoke: under the real
    build_dev_handler boot, RULES + NOW + STATE substrate reaches the
    model-call seam on the decoupled path. This is a smoke version of
    the contract tests at tests/test_cognitive_context_contract.py
    that runs against the REAL production wiring (not the unit-test
    mocked _make_handler), catching any boot-time regression that
    would silently drop substrate again."""
    from kernos.kernel.template import PRIMARY_TEMPLATE
    from kernos.messages.models import AuthLevel, NormalizedMessage
    from kernos.repl import build_dev_handler, shutdown_dev_handler

    chains, mock_provider = _make_chain_with_mock_provider()
    with patch(
        "kernos.providers.chains.build_chains_from_env",
        return_value=(chains, None),
    ):
        handler = await build_dev_handler(sender="smoke-tester")
    try:
        message = NormalizedMessage(
            content="Test substrate fidelity through the real boot.",
            sender="smoke-tester",
            sender_auth_level=AuthLevel.owner_verified,
            platform="repl",
            platform_capabilities=["text"],
            conversation_id="smoke-tester",
            timestamp=datetime.now(timezone.utc),
            instance_id="repl:smoke",
        )
        await handler.process(message)

        # Find the user-reply call — the LAST call without output_schema
        # (filters out the message-analyzer call which uses schema-output).
        reasoning_calls = [
            c for c in mock_provider.complete.call_args_list
            if c.kwargs.get("output_schema") is None
        ]
        assert reasoning_calls, "expected at least one non-schema reasoning call"
        last = reasoning_calls[-1]
        system = last.kwargs.get("system", "")

        # Normalize list-of-dict (cache-aware) to plain string for substring search.
        if isinstance(system, list):
            sys_text = "\n".join(
                (b.get("text", "") if isinstance(b, dict) else str(b))
                for b in system
            )
        else:
            sys_text = str(system or "")

        # Substrate-fidelity floor: the operating_principles head reaches
        # the model. If this trips, the production boot path is silently
        # dropping substrate — the exact bug class CCV1 was created to
        # prevent.
        head = PRIMARY_TEMPLATE.operating_principles.strip().splitlines()[0].strip()
        assert head and head in sys_text, (
            f"CCV1 substrate-fidelity smoke failure: operating_principles "
            f"head ({head!r}) did not reach the model on the real "
            f"build_dev_handler boot path. system head: {sys_text[:400]!r}"
        )
    finally:
        await shutdown_dev_handler(handler)


# ---------------------------------------------------------------------------
# Smoke: legacy boot still works (for the soak's legacy-oracle run)
# ---------------------------------------------------------------------------


async def test_build_dev_handler_legacy_path_also_boots(isolated_env, monkeypatch):
    """The soak runbook runs each scenario on both paths (legacy
    oracle vs. decoupled). Pin: build_dev_handler with
    decoupled=False boots successfully too — it threads through the
    legacy reasoning loop instead of the per-turn TurnRunner."""
    from kernos.repl import build_dev_handler, shutdown_dev_handler
    monkeypatch.delenv("KERNOS_USE_DECOUPLED_TURN_RUNNER", raising=False)

    chains, _mock_provider = _make_chain_with_mock_provider()
    with patch(
        "kernos.providers.chains.build_chains_from_env",
        return_value=(chains, None),
    ):
        handler = await build_dev_handler(decoupled=False)
    try:
        assert handler is not None
        # The handler is functional regardless of which path; legacy
        # path uses _reason_with_chain instead of the turn_runner_provider.
        assert handler.reasoning is not None
    finally:
        await shutdown_dev_handler(handler)

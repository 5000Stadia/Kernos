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
* The REPL the operator uses for soak is exercised in CI, so a
  regression that breaks the dev-soak path fails the build.

These tests also validate the load-bearing CCV1 invariant from
the **real boot** (not the mocked one): the substrate reaches the
model-call seam end-to-end through ``build_dev_handler``'s wiring.
A passing smoke test is structural protection that the production
boot path preserves substrate fidelity.
"""

from __future__ import annotations

import os
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



# ---------------------------------------------------------------------------
# Codex C5-review BLOCKER fold pins
# ---------------------------------------------------------------------------



async def test_select_member_returns_repl_identity_with_repl_platform(isolated_env):
    """Codex C5-review BLOCKER (Q3) pin: select_member returns a
    ReplIdentity whose ``platform`` is "repl" (matches what
    _build_message uses) AND whose ``channel_id`` is registered
    under platform="repl" in instance_db, so handler._resolve_member
    finds the right member and the abuse-prevention guard doesn't
    fire."""
    from kernos.repl import (
        ReplIdentity,
        build_dev_handler,
        select_member,
        shutdown_dev_handler,
    )

    chains, _mock_provider = _make_chain_with_mock_provider()
    with patch(
        "kernos.providers.chains.build_chains_from_env",
        return_value=(chains, None),
    ):
        handler = await build_dev_handler(sender="operator")
    try:
        # Single-member auto-selection path. ensure_owner ran at boot
        # because the instance had no members yet, so the operator is
        # registered with a repl channel.
        identity = await select_member(handler, explicit_sender=None)
        assert isinstance(identity, ReplIdentity)
        assert identity.platform == "repl", (
            "selected channel must use platform='repl' so the REPL's "
            "_build_message platform field matches"
        )
        # The channel id must resolve via instance_db on platform=repl.
        member = await handler._instance_db.get_member_by_channel(
            "repl", identity.channel_id,
        )
        assert member is not None, (
            "select_member's chosen channel_id must be registered "
            "under platform='repl' so handler._resolve_member finds "
            "the right member"
        )
    finally:
        await shutdown_dev_handler(handler)


async def test_shutdown_stops_evaluators_via_evaluators_dict(isolated_env, monkeypatch):
    """Codex C5-review BLOCKER (Q2) pin: shutdown_dev_handler must
    stop awareness evaluators via the ``handler._evaluators`` dict
    (the live attribute), not the singular ``handler._evaluator``
    (which doesn't exist). Without this fix, awareness tasks
    started lazily during turns leak into subsequent test runs and
    cascade-fail the suite."""
    from kernos.repl import build_dev_handler, shutdown_dev_handler

    chains, _mock_provider = _make_chain_with_mock_provider()
    with patch(
        "kernos.providers.chains.build_chains_from_env",
        return_value=(chains, None),
    ):
        handler = await build_dev_handler()

    # Inject a mock evaluator into the dict the way the real handler
    # does. The shutdown helper must iterate _evaluators.values() and
    # call .stop() on each.
    stopped: list[str] = []

    class _MockEval:
        def __init__(self, name): self.name = name
        async def stop(self): stopped.append(self.name)

    handler._evaluators = {
        "instance-A": _MockEval("A"),
        "instance-B": _MockEval("B"),
    }

    await shutdown_dev_handler(handler)

    assert sorted(stopped) == ["A", "B"], (
        f"shutdown_dev_handler must stop every evaluator in "
        f"handler._evaluators (the live attribute) — got stopped="
        f"{stopped!r}. The bug Codex caught: previous code checked "
        f"handler._evaluator (singular, doesn't exist) so live "
        f"awareness tasks leaked between sessions."
    )
    assert handler._evaluators == {}, (
        "shutdown_dev_handler must clear the evaluators dict so a "
        "subsequent boot starts fresh"
    )

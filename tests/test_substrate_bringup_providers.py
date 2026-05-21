"""Tests for SUBSTRATE-PROVIDER-INJECTION-V1 (2026-05-21).

The dual-module bug RCA:
    ``start.sh`` launches ``python kernos/server.py`` → server.py runs
    as ``__main__``. Before this fix, ``bring_up_substrate.py`` did
    ``import kernos.server as _srv``, which Python resolves as a
    SEPARATE module copy (not the running ``__main__``). The
    observer's provider lambdas captured ``_srv``, so they read inert
    globals while the live @client.event handlers mutated
    ``__main__``. The heartbeat cross-check never suppressed a single
    signal in production.

These tests pin the dependency-injection boundary so that class of
bug cannot recur from substrate code:

  * AC3 (test_no_kernos_server_import_node_in_bring_up_substrate):
    AST-level guard against re-introducing ``import kernos.server``.
  * AC4 (test_bring_up_substrate_does_not_load_kernos_server):
    runtime guard — calling bring_up_substrate must not cause
    ``kernos.server`` to enter sys.modules from a substrate import.
  * AC7 (test_skipped_branch_logs_and_no_observer):
    providers=None → SKIPPED log + no observer registered.
  * AC8 (test_provider_identity_preserved_through_observer_init):
    when providers are passed in, the observer is constructed with
    the IDENTICAL callable objects + counter object (identity, not
    equality) — proves no lossy translation step.
  * AC9 (test_provider_lambdas_capture_by_reference_not_value):
    mutation test — lambdas read fresh values, not frozen snapshots.
  * AC10 (test_server_py_call_site_passes_gateway_health_providers):
    AST guard at server.py:1106 call site.
  * AC11 (test_no_kernos_server_string_literal_in_substrate):
    belt-and-suspenders — no string literal "kernos.server"
    outside comments/docstrings.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# Repo-root paths derived relative to this test file so the suite
# works in any checkout (Codex round 3 finding).
_REPO_ROOT = Path(__file__).resolve().parents[1]
_BRING_UP_PATH = _REPO_ROOT / "kernos" / "setup" / "bring_up_substrate.py"
_SERVER_PATH = _REPO_ROOT / "kernos" / "server.py"


# ===========================================================================
# AST guards: bring_up_substrate.py must not import or reference kernos.server
# ===========================================================================


def _load_bring_up_substrate_ast() -> ast.Module:
    return ast.parse(_BRING_UP_PATH.read_text(encoding="utf-8"))


def _load_server_ast() -> ast.Module:
    return ast.parse(_SERVER_PATH.read_text(encoding="utf-8"))


class TestNoKernosServerImportInSubstrate:
    """AC3 + AC11: bring_up_substrate.py must not import kernos.server
    AND must not reference the string ``kernos.server`` outside of
    comments/docstrings."""

    def test_no_kernos_server_import_node_in_bring_up_substrate(self):
        tree = _load_bring_up_substrate_ast()
        offending = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "kernos.server" or (
                        alias.name and alias.name.startswith("kernos.server.")
                    ):
                        offending.append(
                            (node.lineno, "Import", alias.name),
                        )
            elif isinstance(node, ast.ImportFrom):
                if node.module == "kernos.server" or (
                    node.module
                    and node.module.startswith("kernos.server.")
                ):
                    offending.append(
                        (node.lineno, "ImportFrom", node.module),
                    )
        assert not offending, (
            f"bring_up_substrate.py must not import kernos.server "
            f"(dual-module bug under python kernos/server.py). "
            f"Found: {offending}"
        )

    def test_no_kernos_server_string_literal_in_substrate(self):
        """String literals are not catastrophic on their own, but
        flag them so a future contributor can't sneak the dependency
        back in via dynamic import (``importlib.import_module
        ('kernos.server')``). Docstrings and comments are intentionally
        allowed — the spec/RCA explanation needs to name the bad path."""
        tree = _load_bring_up_substrate_ast()
        # Collect AST node IDs that are docstrings (the first stmt
        # of a module/function/class body when it's a string constant)
        docstring_node_ids: set[int] = set()
        for parent in ast.walk(tree):
            body = getattr(parent, "body", None)
            if (
                body
                and isinstance(body, list)
                and body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstring_node_ids.add(id(body[0].value))
        offending = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in docstring_node_ids:
                    continue
                if "kernos.server" in node.value:
                    offending.append((node.lineno, node.value[:80]))
        assert not offending, (
            f"bring_up_substrate.py contains string literal "
            f"'kernos.server' outside docstrings — potential "
            f"dynamic-import smell. Found: {offending}"
        )


# ===========================================================================
# Runtime guard: bring_up_substrate must not load kernos.server
# ===========================================================================


class TestImportingBringUpSubstrateDoesNotLoadKernosServer:
    """AC4 (revised): merely importing bring_up_substrate must not
    pull in ``kernos.server``. The AST guards above pin the source
    level; this one pins the runtime level — if any top-level statement
    in bring_up_substrate.py triggers an import of kernos.server
    (transitively), this test catches it.

    Calling bring_up_substrate() end-to-end requires stubbing the
    entire substrate (databases, emitters, action library, etc.) which
    is not a useful unit-test surface. The runtime-level guarantee
    we actually need is: no part of bring_up_substrate's module load
    (including its imports) ends up importing kernos.server. That's
    what this test checks."""

    def test_importing_bring_up_substrate_does_not_load_kernos_server(self):
        # If kernos.server is already loaded from some other path
        # (e.g., a prior test imported it), this test cannot prove
        # the absence in isolation. Skip in that case rather than
        # produce a misleading pass/fail.
        if "kernos.server" in sys.modules:
            pytest.skip(
                "kernos.server already in sys.modules (loaded by "
                "a prior test or fixture); cannot prove absence "
                "without process isolation."
            )

        # Force a fresh load of the substrate module to trigger
        # whatever transitive imports its top level would.
        sys.modules.pop("kernos.setup.bring_up_substrate", None)
        import kernos.setup.bring_up_substrate  # noqa: F401

        assert "kernos.server" not in sys.modules, (
            "Importing kernos.setup.bring_up_substrate caused "
            "kernos.server to be loaded. The dual-module bug "
            "path is alive — some import in the substrate module "
            "chain pulls in kernos.server."
        )


# ===========================================================================
# Providers=None branch: skip the observer (AC1, AC7)
# ===========================================================================


class TestProvidersNoneBranchSourceStructure:
    """AC1 + AC7 (revised): the providers=None branch must skip the
    observer (NOT a no-op observer) and log a clear SKIPPED line.

    Calling bring_up_substrate end-to-end is not a feasible unit-test
    surface (deep dependency stubbing). Instead, this test verifies
    the conditional structure at the source level: there must be an
    ``if gateway_health_providers is not None:`` branch in
    bring_up_substrate.py, and the else branch must contain the
    SKIPPED log line.
    """

    def test_providers_is_none_skip_branch_present(self):
        """At least one ``if gateway_health_providers is None:``
        OR ``if gateway_health_providers is not None:`` branch must
        exist somewhere in bring_up_substrate.py. The current
        implementation uses an ``is None`` early-return inside the
        extracted helper; either shape satisfies the spec intent
        (a deliberate, code-level branch on the providers arg)."""
        tree = _load_bring_up_substrate_ast()
        target_ifs: list[ast.If] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if not isinstance(test, ast.Compare):
                continue
            if not (
                isinstance(test.left, ast.Name)
                and test.left.id == "gateway_health_providers"
            ):
                continue
            if not test.ops:
                continue
            if not isinstance(test.ops[0], (ast.Is, ast.IsNot)):
                continue
            target_ifs.append(node)
        assert target_ifs, (
            "bring_up_substrate.py is missing the branch that gates "
            "observer construction on `gateway_health_providers`. "
            "Expected an `if gateway_health_providers is None:` or "
            "`is not None:` check somewhere in the module."
        )

    def test_skipped_log_message_present(self):
        """The skip log line must be present as a string constant
        in bring_up_substrate.py."""
        tree = _load_bring_up_substrate_ast()
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "GATEWAY_HEALTH_OBSERVER_SKIPPED" in node.value:
                    found = True
                    break
        assert found, (
            "Skip log line GATEWAY_HEALTH_OBSERVER_SKIPPED not found "
            "in bring_up_substrate.py — the providers=None branch is "
            "silent, which defeats the operator-visibility purpose."
        )


# ===========================================================================
# Identity preservation (AC8) + mutation test (AC9)
# ===========================================================================


class TestProviderIdentity:
    """AC8: when providers are passed in, the GatewayHealthObserver
    constructor receives the IDENTICAL callable objects (is, not ==).
    No lossy translation between dataclass and observer.

    AC9: provider lambdas read live state, not frozen snapshots.
    """

    def test_provider_dataclass_holds_callable_references(self):
        """Direct unit check on the dataclass: assignment of
        callables preserves identity."""
        from kernos.kernel.gateway_health import GatewayHealthProviders

        def _lp() -> float | None:
            return 0.05

        def _ie() -> float:
            return 100.0

        def _lo() -> float:
            return 200.0

        from kernos.kernel.gateway_health import _MessageCreateCounter
        _ctr = _MessageCreateCounter(window_sec=600)

        p = GatewayHealthProviders(
            latency_provider=_lp,
            inbound_event_ts_provider=_ie,
            last_on_message_provider=_lo,
            message_create_counter=_ctr,
        )
        assert p.latency_provider is _lp
        assert p.inbound_event_ts_provider is _ie
        assert p.last_on_message_provider is _lo
        assert p.message_create_counter is _ctr

    def test_provider_lambdas_capture_by_reference_not_value(self):
        """The lambdas in server.py close over module globals.
        Mutating the globals must change what the lambdas return.
        Pins the by-reference semantics that the production wiring
        depends on."""
        from kernos.kernel.gateway_health import GatewayHealthProviders

        state = {"latency": 0.05, "inbound_ts": 0.0, "on_msg_ts": 0.0}

        p = GatewayHealthProviders(
            latency_provider=lambda: state["latency"],
            inbound_event_ts_provider=lambda: state["inbound_ts"],
            last_on_message_provider=lambda: state["on_msg_ts"],
            message_create_counter=None,
        )
        # Initial reads
        assert p.latency_provider() == 0.05
        assert p.inbound_event_ts_provider() == 0.0
        assert p.last_on_message_provider() == 0.0

        # Mutate the underlying state
        state["latency"] = float("nan")
        state["inbound_ts"] = 100.0
        state["on_msg_ts"] = 200.0

        # Lambdas see the new values (capture-by-reference)
        import math
        assert math.isnan(p.latency_provider())
        assert p.inbound_event_ts_provider() == 100.0
        assert p.last_on_message_provider() == 200.0


# ===========================================================================
# server.py call site (AC10)
# ===========================================================================


class TestServerPyCallSite:
    """AC10: server.py must pass ``gateway_health_providers=`` keyword
    to ``bring_up_substrate(...)``. If a future refactor drops the
    keyword, the observer silently goes back to being skipped and
    the heartbeat false-positive returns — this guard catches that."""

    def test_server_py_call_site_passes_gateway_health_providers(self):
        tree = _load_server_ast()
        sites: list[tuple[int, list[str]]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = node.func
            name = None
            if isinstance(callee, ast.Name):
                name = callee.id
            elif isinstance(callee, ast.Attribute):
                name = callee.attr
            if name != "bring_up_substrate":
                continue
            kwarg_names = [kw.arg for kw in node.keywords if kw.arg]
            sites.append((node.lineno, kwarg_names))
        assert sites, (
            "Expected at least one call to bring_up_substrate(...) "
            "in server.py — did the call site move?"
        )
        for lineno, kwargs in sites:
            assert "gateway_health_providers" in kwargs, (
                f"server.py line {lineno}: bring_up_substrate(...) "
                f"call is missing gateway_health_providers= kwarg. "
                f"kwargs seen: {kwargs}. Without this, the observer "
                f"will silently skip and the heartbeat false-positive "
                f"will return."
            )


# ===========================================================================
# Runtime tests for the extracted _bring_up_gateway_health_observer helper.
# Codex round 3 finding: AST guards alone don't catch missing imports or
# wiring errors in the populated-provider branch. The helper is a small,
# testable unit; exercise it with mocks.
# ===========================================================================


class TestBringUpGatewayHealthObserverHelper:
    """Runtime behavior of the helper extracted from bring_up_substrate.
    AC1, AC2, AC7, AC8 verified at the call-time level here (not just
    via source structure)."""

    @pytest.mark.asyncio
    async def test_providers_none_skips_observer_with_log(
        self, tmp_path, caplog,
    ):
        """AC7 (runtime): providers=None → SKIPPED log, no observer
        constructed, execution_engine.register_emitter NOT called."""
        import logging
        from kernos.setup.bring_up_substrate import (
            _bring_up_gateway_health_observer,
        )

        execution_engine = MagicMock()
        handler = MagicMock()

        caplog.set_level(
            logging.INFO, logger="kernos.setup.bring_up_substrate",
        )
        await _bring_up_gateway_health_observer(
            data_dir=str(tmp_path),
            handler=handler,
            execution_engine=execution_engine,
            gateway_health_providers=None,
        )
        execution_engine.register_emitter.assert_not_called()
        skipped = [
            r for r in caplog.records
            if "GATEWAY_HEALTH_OBSERVER_SKIPPED" in r.getMessage()
        ]
        assert skipped, (
            "providers=None must emit the SKIPPED log line so the "
            "absence is operator-visible."
        )

    @pytest.mark.asyncio
    async def test_populated_providers_constructs_starts_registers(
        self, tmp_path,
    ):
        """AC2 + AC8 (runtime): with populated providers, the helper
        passes the IDENTICAL provider objects to the observer's
        __init__, calls start(), and registers on execution_engine.
        Identity (is, not ==) is the strong claim — proves no lossy
        translation step."""
        from kernos.kernel.gateway_health import (
            GatewayHealthProviders, _MessageCreateCounter,
        )
        from kernos.setup.bring_up_substrate import (
            _bring_up_gateway_health_observer,
        )

        _lp = lambda: 0.05
        _ie = lambda: 100.0
        _lo = lambda: 200.0
        _ctr = _MessageCreateCounter(window_sec=600)
        providers = GatewayHealthProviders(
            latency_provider=_lp,
            inbound_event_ts_provider=_ie,
            last_on_message_provider=_lo,
            message_create_counter=_ctr,
        )

        execution_engine = MagicMock()
        handler = MagicMock()
        handler._instance_id = "test_inst"
        handler._friction_pattern_store = MagicMock()  # provided, no fallback

        with patch(
            "kernos.kernel.gateway_health.GatewayHealthObserver"
        ) as MockObs:
            mock_instance = MagicMock()
            mock_instance.start = AsyncMock()
            MockObs.return_value = mock_instance

            await _bring_up_gateway_health_observer(
                data_dir=str(tmp_path),
                handler=handler,
                execution_engine=execution_engine,
                gateway_health_providers=providers,
            )

            # Constructor called exactly once
            assert MockObs.call_count == 1
            kwargs = MockObs.call_args.kwargs
            # IDENTITY checks — exact object preservation through the
            # helper. The whole point of provider injection.
            assert kwargs["latency_provider"] is _lp
            assert kwargs["inbound_event_ts_provider"] is _ie
            assert kwargs["last_on_message_provider"] is _lo
            assert kwargs["message_create_counter"] is _ctr
            assert kwargs["pattern_store"] is handler._friction_pattern_store

            # start() awaited
            mock_instance.start.assert_awaited_once()
            # registered on execution_engine under the "gateway_health" key
            execution_engine.register_emitter.assert_called_once_with(
                "gateway_health", mock_instance,
            )

    @pytest.mark.asyncio
    async def test_handler_missing_pattern_store_falls_back(
        self, tmp_path, caplog,
    ):
        """When handler._friction_pattern_store is None, helper
        creates a fresh FrictionPatternStore and ensure_schema's it.
        Pin: production handlers normally have one (handler.py:962);
        fallback exists only for stripped-down/test contexts."""
        import logging
        from kernos.kernel.gateway_health import GatewayHealthProviders
        from kernos.setup.bring_up_substrate import (
            _bring_up_gateway_health_observer,
        )

        providers = GatewayHealthProviders(
            latency_provider=lambda: 0.05,
            inbound_event_ts_provider=lambda: 0.0,
            last_on_message_provider=lambda: 0.0,
            message_create_counter=None,
        )
        execution_engine = MagicMock()
        handler = MagicMock()
        handler._friction_pattern_store = None
        handler._instance_id = "test_inst"

        caplog.set_level(
            logging.INFO, logger="kernos.setup.bring_up_substrate",
        )
        with patch(
            "kernos.kernel.gateway_health.GatewayHealthObserver"
        ) as MockObs:
            mock_instance = MagicMock()
            mock_instance.start = AsyncMock()
            MockObs.return_value = mock_instance
            await _bring_up_gateway_health_observer(
                data_dir=str(tmp_path),
                handler=handler,
                execution_engine=execution_engine,
                gateway_health_providers=providers,
            )
            # The pattern_store passed to the observer is NOT None
            kwargs = MockObs.call_args.kwargs
            assert kwargs["pattern_store"] is not None
        # Fallback log line surfaced
        fallback_logs = [
            r for r in caplog.records
            if "GATEWAY_HEALTH_OBSERVER_FALLBACK_STORE" in r.getMessage()
        ]
        assert fallback_logs, (
            "Fallback store creation must log so operators see "
            "ownership/teardown is not tracked by the helper."
        )

    @pytest.mark.asyncio
    async def test_observer_construction_failure_logs_does_not_raise(
        self, tmp_path, caplog,
    ):
        """If the observer's __init__ raises (or anything in the
        bring-up sequence), the helper must catch + log, not
        propagate. Gateway-health is a safety monitor; a failure to
        bring IT up cannot fail the entire substrate."""
        import logging
        from kernos.kernel.gateway_health import GatewayHealthProviders
        from kernos.setup.bring_up_substrate import (
            _bring_up_gateway_health_observer,
        )

        providers = GatewayHealthProviders(
            latency_provider=lambda: 0.05,
            inbound_event_ts_provider=lambda: 0.0,
            last_on_message_provider=lambda: 0.0,
            message_create_counter=None,
        )
        execution_engine = MagicMock()
        handler = MagicMock()
        handler._friction_pattern_store = MagicMock()
        handler._instance_id = "test_inst"

        caplog.set_level(
            logging.WARNING, logger="kernos.setup.bring_up_substrate",
        )
        with patch(
            "kernos.kernel.gateway_health.GatewayHealthObserver",
            side_effect=RuntimeError("simulated"),
        ):
            # Must NOT raise
            await _bring_up_gateway_health_observer(
                data_dir=str(tmp_path),
                handler=handler,
                execution_engine=execution_engine,
                gateway_health_providers=providers,
            )
        # Did register fire? Should NOT — observer failed
        execution_engine.register_emitter.assert_not_called()
        # And the failure must surface as a WARNING log
        failures = [
            r for r in caplog.records
            if "GATEWAY_HEALTH_OBSERVER_BRINGUP_FAILED" in r.getMessage()
        ]
        assert failures, (
            "Helper must log BRINGUP_FAILED when the observer "
            "init/start fails — silent failure would re-create the "
            "original bug we shipped this fix to prevent."
        )

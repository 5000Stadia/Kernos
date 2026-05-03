"""Tests for LLM-SETUP-AND-FALLBACK.

Covers:
  * Storage-backend abstraction — single-key write/read/remove + has_secret.
  * Storage-backend switching — the four switch paths (keychain ↔ hardened,
    hardened ↔ plaintext, plaintext ↔ keychain) with cleanup-after-verify
    ordering per the design review's implementation hazard.
  * Provider registry — seven entries, all fields populated.
  * Benchmark snapshot reader — setup-time-only surface, returns dict.
  * Chain config IO — add / remove / set-model in place.
  * Startup health check — binary config read, no network, no LLM.
  * LLMChainExhausted — raised by _call_chain when every entry fails;
    handler delivers the pre-rendered failure message instead of an LLM reply.

**Zero-LLM-call:** None of these tests make an LLM call. They validate the
setup machinery itself, which is the whole point of the invariant.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from kernos.setup.benchmark_snapshot import (
    BenchmarkSnapshotError,
    load_snapshot,
    recommended_models,
)
from kernos.setup.chain_config_io import (
    ChainEntrySpec,
    add_provider_to_chains,
    configured_providers,
    load_chain_config,
    remove_provider_from_chains,
    save_chain_config,
    set_chain_model_in_config,
)
from kernos.setup.health_check import check_llm_chain_health
from kernos.setup.provider_registry import (
    REGISTRY,
    ProviderEntry,
    get_provider,
    list_providers,
)
from kernos.setup.storage_backend import (
    HardenedEnvBackend,
    KeychainBackend,
    PlaintextEnvBackend,
    StorageBackendSwitchAborted,
    StorageBackendUnavailable,
    VALID_BACKENDS,
    active_backend_name,
    detect_default_backend,
    get_backend,
    set_active_backend_name,
    switch_storage_backend,
)


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------


class TestProviderRegistry:
    def test_seven_providers(self):
        assert len(list_providers()) == 7
        ids = {p.provider_id for p in list_providers()}
        assert ids == {
            "anthropic", "openai", "google", "groq", "xai", "openrouter", "ollama",
        }

    def test_ollama_is_local_no_key(self):
        ollama = get_provider("ollama")
        assert ollama is not None
        assert ollama.is_local_only is True
        assert ollama.requires_key is False

    def test_all_remote_providers_require_key(self):
        for pid, entry in REGISTRY.items():
            if pid == "ollama":
                continue
            assert entry.requires_key is True, f"{pid} must require a key"
            assert entry.key_env_var, f"{pid} must declare a key env var"

    def test_unknown_provider_returns_none(self):
        assert get_provider("does-not-exist") is None


# ---------------------------------------------------------------------------
# Benchmark snapshot — setup-time only surface
# ---------------------------------------------------------------------------


class TestBenchmarkSnapshot:
    def test_loads_real_snapshot(self):
        data = load_snapshot()
        assert "providers" in data

    def test_anthropic_has_recommendations(self):
        recs = recommended_models("anthropic")
        assert "primary" in recs
        assert "cheap" in recs

    def test_unknown_provider_returns_empty(self):
        recs = recommended_models("does-not-exist")
        assert recs == {}

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(BenchmarkSnapshotError):
            load_snapshot(tmp_path / "missing.json")


# ---------------------------------------------------------------------------
# Chain config IO
# ---------------------------------------------------------------------------


class TestChainConfigIO:
    def test_empty_config_loads_empty(self, tmp_path):
        cfg = load_chain_config(tmp_path / "missing.yml")
        assert cfg == {}

    def test_add_provider_fills_three_chains(self):
        cfg = {}
        new = add_provider_to_chains(
            cfg, provider_id="anthropic",
            primary_model="claude-opus-4-7",
            cheap_model="claude-haiku-4-5",
        )
        assert "primary" in new and "cheap" in new and "simple" in new
        assert new["primary"][0].model == "claude-opus-4-7"
        assert new["cheap"][0].model == "claude-haiku-4-5"
        # simple inherits from cheap:
        assert new["simple"][0].model == "claude-haiku-4-5"

    def test_set_chain_model_updates_existing(self):
        cfg = add_provider_to_chains(
            {}, provider_id="anthropic",
            primary_model="claude-opus-4-7", cheap_model="claude-haiku-4-5",
        )
        new = set_chain_model_in_config(
            cfg, chain="primary", provider_id="anthropic",
            model="claude-sonnet-4-6",
        )
        assert new["primary"][0].model == "claude-sonnet-4-6"

    def test_set_chain_model_appends_new_provider(self):
        cfg = {"primary": [ChainEntrySpec(provider="anthropic", model="claude-opus-4-7")]}
        new = set_chain_model_in_config(
            cfg, chain="primary", provider_id="openai", model="gpt-5.3",
        )
        assert len(new["primary"]) == 2
        assert new["primary"][1].provider == "openai"

    def test_remove_provider_drops_everywhere(self):
        cfg = add_provider_to_chains(
            {}, provider_id="anthropic",
            primary_model="claude-opus-4-7", cheap_model="claude-haiku-4-5",
        )
        cfg = add_provider_to_chains(
            cfg, provider_id="openai",
            primary_model="gpt-5.3", cheap_model="gpt-5.3-mini",
        )
        new = remove_provider_from_chains(cfg, "anthropic")
        for chain_entries in new.values():
            for entry in chain_entries:
                assert entry.provider != "anthropic"

    def test_round_trip(self, tmp_path):
        path = tmp_path / "llm_chains.yml"
        cfg = add_provider_to_chains(
            {}, provider_id="anthropic",
            primary_model="claude-opus-4-7", cheap_model="claude-haiku-4-5",
        )
        save_chain_config(cfg, path=path)
        loaded = load_chain_config(path)
        assert loaded.keys() == cfg.keys()
        assert loaded["primary"][0].provider == "anthropic"


# ---------------------------------------------------------------------------
# Storage backend — single-key primitives
# ---------------------------------------------------------------------------


class TestHardenedBackend:
    def test_write_read_remove(self, tmp_path):
        env_path = tmp_path / ".env"
        backend = HardenedEnvBackend(env_path=env_path)
        backend.write_secret("TEST_KEY", "value_1")
        assert backend.has_secret("TEST_KEY")
        assert backend.read_secret("TEST_KEY") == "value_1"
        backend.remove_secret("TEST_KEY")
        assert not backend.has_secret("TEST_KEY")

    def test_file_mode_is_0600(self, tmp_path):
        env_path = tmp_path / ".env"
        backend = HardenedEnvBackend(env_path=env_path)
        backend.write_secret("TEST_KEY", "value_1")
        mode = env_path.stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"


class TestPlaintextBackend:
    def test_write_read_remove(self, tmp_path):
        env_path = tmp_path / ".env"
        backend = PlaintextEnvBackend(env_path=env_path)
        backend.write_secret("TEST_KEY", "value_1")
        assert backend.read_secret("TEST_KEY") == "value_1"


# ---------------------------------------------------------------------------
# Storage backend — switch cleanup (the design review's implementation hazard)
#
# Four switch paths, each asserting: (a) secrets land on target, (b) the old
# backend no longer holds them, (c) on abort, the old backend is untouched.
# ---------------------------------------------------------------------------


def _make_file_backend(kind: str, tmp_path: Path):
    env_path = tmp_path / f".env.{kind}"
    if kind == "hardened":
        return HardenedEnvBackend(env_path=env_path)
    return PlaintextEnvBackend(env_path=env_path)


class TestStorageBackendSwitch:
    """the design review's hazard: backend switching must be a cleanup operation.

    Each switch path is tested in isolation. Keychain is mocked so the tests
    run on any host (CI, laptops without Secret Service, etc.).
    """

    MANAGED = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]

    def _configure(
        self,
        tmp_path: Path,
        current_kind: str,
        *,
        seed_values: dict[str, str] | None = None,
    ):
        """Put the system into a state where ``current_kind`` is the active backend.

        Returns ``(config_path, current_backend)``.
        """
        config_path = tmp_path / "storage_backend.yml"
        current_backend = _make_file_backend(current_kind, tmp_path)
        for var, val in (seed_values or {}).items():
            current_backend.write_secret(var, val)
        set_active_backend_name(
            "env_hardened" if current_kind == "hardened" else "env_plaintext",
            config_path,
        )
        return config_path, current_backend

    def test_hardened_to_plaintext_migrates_and_cleans(self, tmp_path, monkeypatch):
        seed = {"ANTHROPIC_API_KEY": "sk-a", "OPENAI_API_KEY": "sk-o"}
        config_path, current = self._configure(tmp_path, "hardened", seed_values=seed)

        # Point the module-level path helpers at tmp_path versions.
        target = _make_file_backend("plaintext", tmp_path)

        def _fake_get_backend(name):
            if name == "env_hardened":
                return current
            if name == "env_plaintext":
                return target
            raise AssertionError(f"unexpected backend {name}")

        monkeypatch.setattr("kernos.setup.storage_backend.get_backend", _fake_get_backend)

        switch_storage_backend("env_plaintext", self.MANAGED, config_path=config_path)

        # Target has the secrets.
        assert target.read_secret("ANTHROPIC_API_KEY") == "sk-a"
        assert target.read_secret("OPENAI_API_KEY") == "sk-o"
        # Old backend no longer holds them.
        assert current.read_secret("ANTHROPIC_API_KEY") is None
        assert current.read_secret("OPENAI_API_KEY") is None
        # Active backend name persisted.
        assert active_backend_name(config_path) == "env_plaintext"

    def test_plaintext_to_hardened_migrates_and_cleans(self, tmp_path, monkeypatch):
        seed = {"ANTHROPIC_API_KEY": "sk-a", "OPENAI_API_KEY": "sk-o"}
        config_path, current = self._configure(tmp_path, "plaintext", seed_values=seed)
        target = _make_file_backend("hardened", tmp_path)

        def _fake(name):
            return target if name == "env_hardened" else current

        monkeypatch.setattr("kernos.setup.storage_backend.get_backend", _fake)
        switch_storage_backend("env_hardened", self.MANAGED, config_path=config_path)

        assert target.read_secret("ANTHROPIC_API_KEY") == "sk-a"
        assert current.read_secret("ANTHROPIC_API_KEY") is None
        assert active_backend_name(config_path) == "env_hardened"

    def test_keychain_to_hardened_migrates_and_cleans(self, tmp_path, monkeypatch):
        # In-memory stand-in for the keychain.
        keyring_store: dict[tuple[str, str], str] = {
            ("kernos", "ANTHROPIC_API_KEY"): "sk-a",
            ("kernos", "OPENAI_API_KEY"): "sk-o",
        }

        class FakeKeychain:
            name = "keychain"
            def is_available(self): return True
            def write_secret(self, k, v): keyring_store[("kernos", k)] = v
            def read_secret(self, k): return keyring_store.get(("kernos", k))
            def remove_secret(self, k): keyring_store.pop(("kernos", k), None)
            def has_secret(self, k): return ("kernos", k) in keyring_store

        target = _make_file_backend("hardened", tmp_path)
        fake_keychain = FakeKeychain()
        config_path = tmp_path / "storage_backend.yml"
        set_active_backend_name("keychain", config_path)

        def _fake(name):
            return fake_keychain if name == "keychain" else target

        monkeypatch.setattr("kernos.setup.storage_backend.get_backend", _fake)
        switch_storage_backend("env_hardened", self.MANAGED, config_path=config_path)

        assert target.read_secret("ANTHROPIC_API_KEY") == "sk-a"
        assert fake_keychain.read_secret("ANTHROPIC_API_KEY") is None
        assert active_backend_name(config_path) == "env_hardened"

    def test_hardened_to_keychain_migrates_and_cleans(self, tmp_path, monkeypatch):
        seed = {"ANTHROPIC_API_KEY": "sk-a"}
        config_path, current = self._configure(tmp_path, "hardened", seed_values=seed)
        keyring_store: dict[tuple[str, str], str] = {}

        class FakeKeychain:
            name = "keychain"
            def is_available(self): return True
            def write_secret(self, k, v): keyring_store[("kernos", k)] = v
            def read_secret(self, k): return keyring_store.get(("kernos", k))
            def remove_secret(self, k): keyring_store.pop(("kernos", k), None)
            def has_secret(self, k): return ("kernos", k) in keyring_store

        fake_keychain = FakeKeychain()

        def _fake(name):
            return fake_keychain if name == "keychain" else current

        monkeypatch.setattr("kernos.setup.storage_backend.get_backend", _fake)
        switch_storage_backend("keychain", self.MANAGED, config_path=config_path)

        assert fake_keychain.read_secret("ANTHROPIC_API_KEY") == "sk-a"
        assert current.read_secret("ANTHROPIC_API_KEY") is None
        assert active_backend_name(config_path) == "keychain"

    def test_switch_aborts_and_leaves_old_backend_untouched(self, tmp_path, monkeypatch):
        """Read-back fails → old backend must be unchanged, config unchanged."""
        seed = {"ANTHROPIC_API_KEY": "sk-a"}
        config_path, current = self._configure(tmp_path, "hardened", seed_values=seed)

        class BrokenBackend:
            """Accepts writes but returns the wrong value on read — simulates corruption."""
            name = "env_plaintext"
            def is_available(self): return True
            def write_secret(self, k, v): pass      # silently drop
            def read_secret(self, k): return "WRONG"
            def remove_secret(self, k): pass
            def has_secret(self, k): return True

        broken = BrokenBackend()

        def _fake(name):
            return broken if name == "env_plaintext" else current

        monkeypatch.setattr("kernos.setup.storage_backend.get_backend", _fake)

        with pytest.raises(StorageBackendSwitchAborted):
            switch_storage_backend("env_plaintext", self.MANAGED, config_path=config_path)

        # Old backend still holds the secret; active name unchanged.
        assert current.read_secret("ANTHROPIC_API_KEY") == "sk-a"
        assert active_backend_name(config_path) == "env_hardened"

    def test_first_time_set_no_migration(self, tmp_path, monkeypatch):
        """No active backend yet → set-only, no copy/verify path."""
        config_path = tmp_path / "storage_backend.yml"
        assert active_backend_name(config_path) is None

        target = _make_file_backend("hardened", tmp_path)

        def _fake(name):
            return target

        monkeypatch.setattr("kernos.setup.storage_backend.get_backend", _fake)
        switch_storage_backend("env_hardened", self.MANAGED, config_path=config_path)
        assert active_backend_name(config_path) == "env_hardened"


# ---------------------------------------------------------------------------
# Startup health check — binary config read
# ---------------------------------------------------------------------------


class TestHealthCheck:
    """Runtime-source-of-truth dry-run — no YAML, no storage-backend gating.

    ``check_llm_chain_health`` now delegates to
    :func:`kernos.providers.chains.can_build_chains_from_env`, which asks
    the runtime chain builder whether it would resolve. See
    ``chain_config_io`` docstring for why the YAML stopped being the gate.
    """

    def _clear_env(self, monkeypatch):
        """Scrub LLM env vars so each test starts from a known state."""
        for name in (
            "KERNOS_LLM_PROVIDER", "KERNOS_LLM_FALLBACK",
            "ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_TOKEN",
            "OPENAI_CODEX_ACCESS_TOKEN", "OPENAI_CODEX_REFRESH_TOKEN",
            "OPENAI_CODEX_EXPIRES", "OPENAI_CODEX_ACCOUNT_ID",
            "OPENAI_CODEX_CREDS_PATH",
            "OPENCLAW_AUTH_PROFILES_PATH",
        ):
            monkeypatch.delenv(name, raising=False)

    def test_empty_primary_provider_fails(self, monkeypatch, tmp_path):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("KERNOS_LLM_PROVIDER", "")
        # Force anthropic creds path to hit no-home fallback
        monkeypatch.setenv("HOME", str(tmp_path))
        result = check_llm_chain_health()
        assert result.ok is False
        # Empty string falls through the default to "anthropic"; without
        # any anthropic credential the check reports the credential gap.
        assert "anthropic" in result.reason.lower() or "provider" in result.reason.lower()

    def test_unknown_primary_provider_fails(self, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("KERNOS_LLM_PROVIDER", "bogus-provider")
        result = check_llm_chain_health()
        assert result.ok is False
        assert "bogus-provider" in result.reason

    def test_anthropic_primary_with_api_key_passes(self, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("KERNOS_LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
        result = check_llm_chain_health()
        assert result.ok is True, result.reason
        assert result.primary_spec == "anthropic"
        assert "anthropic" in result.resolved

    def test_anthropic_primary_without_credential_fails(self, monkeypatch, tmp_path):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("KERNOS_LLM_PROVIDER", "anthropic")
        # Point HOME at an empty dir so Claude CLI creds resolver misses.
        monkeypatch.setenv("HOME", str(tmp_path))
        result = check_llm_chain_health()
        assert result.ok is False
        assert "anthropic" in result.reason.lower()

    def test_ollama_primary_needs_no_credential(self, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("KERNOS_LLM_PROVIDER", "ollama")
        result = check_llm_chain_health()
        assert result.ok is True, result.reason
        assert result.primary_spec == "ollama"

    def test_openai_codex_primary_with_env_creds_passes(self, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("KERNOS_LLM_PROVIDER", "openai-codex")
        monkeypatch.setenv("OPENAI_CODEX_ACCESS_TOKEN", "access-x")
        monkeypatch.setenv("OPENAI_CODEX_REFRESH_TOKEN", "refresh-x")
        monkeypatch.setenv("OPENAI_CODEX_EXPIRES", "0")
        monkeypatch.setenv("OPENAI_CODEX_ACCOUNT_ID", "acct-x")
        result = check_llm_chain_health()
        assert result.ok is True, result.reason
        assert result.primary_spec == "openai-codex"

    def test_openai_codex_without_creds_fails(self, monkeypatch, tmp_path):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("KERNOS_LLM_PROVIDER", "openai-codex")
        # Redirect the codex creds-file path at a nonexistent location.
        monkeypatch.setenv("OPENAI_CODEX_CREDS_PATH", str(tmp_path / "missing.json"))
        result = check_llm_chain_health()
        assert result.ok is False
        assert "openai-codex" in result.reason

    def test_fallback_failure_does_not_block_primary_success(self, monkeypatch, tmp_path):
        """Fallback probing mirrors runtime tolerance — skipped, not fatal."""
        self._clear_env(monkeypatch)
        monkeypatch.setenv("KERNOS_LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        # Fallback: a provider spec with no credentials configured.
        monkeypatch.setenv("KERNOS_LLM_FALLBACK", "openai-codex")
        monkeypatch.setenv("OPENAI_CODEX_CREDS_PATH", str(tmp_path / "nope.json"))
        result = check_llm_chain_health()
        assert result.ok is True
        assert "anthropic" in result.resolved
        # The failed fallback is reported without blocking.
        assert any(spec == "openai-codex" for spec, _ in result.unresolved)

    def test_dry_run_does_not_instantiate_providers(self, monkeypatch):
        """Side-effect invariant: no Provider subclass constructed.

        We patch each provider class's ``__init__`` and assert zero
        calls. If a future contributor accidentally routes the dry-run
        through ``_instantiate_provider``, this test catches it.
        """
        self._clear_env(monkeypatch)
        monkeypatch.setenv("KERNOS_LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        from kernos.providers import anthropic_provider, codex_provider, ollama_provider

        constructor_calls: list[str] = []

        def _trap_anthropic(self, *a, **kw):
            constructor_calls.append("anthropic")
            raise AssertionError("AnthropicProvider must not be constructed in dry-run")

        def _trap_codex(self, *a, **kw):
            constructor_calls.append("codex")
            raise AssertionError("OpenAICodexProvider must not be constructed in dry-run")

        def _trap_ollama(self, *a, **kw):
            constructor_calls.append("ollama")
            raise AssertionError("OllamaProvider must not be constructed in dry-run")

        monkeypatch.setattr(
            anthropic_provider.AnthropicProvider, "__init__", _trap_anthropic,
        )
        monkeypatch.setattr(
            codex_provider.OpenAICodexProvider, "__init__", _trap_codex,
        )
        monkeypatch.setattr(
            ollama_provider.OllamaProvider, "__init__", _trap_ollama,
        )

        result = check_llm_chain_health()
        assert result.ok is True, result.reason
        assert constructor_calls == [], (
            f"dry-run instantiated a provider: {constructor_calls}"
        )


# ---------------------------------------------------------------------------
# LLMChainExhausted + handler message rendering
# ---------------------------------------------------------------------------


class TestChainExhaustion:
    def test_exception_carries_attempts(self):
        from kernos.kernel.exceptions import LLMChainExhausted

        exc = LLMChainExhausted(
            "primary",
            [("anthropic", "claude-opus-4-7", "auth"), ("openai", "gpt-5.3", "timeout")],
        )
        assert exc.chain_name == "primary"
        assert len(exc.attempts) == 2

    def test_pre_rendered_message_names_chain(self):
        from kernos.kernel.exceptions import LLMChainExhausted
        from kernos.messages.handler import _render_chain_exhaustion_message

        exc = LLMChainExhausted("primary", [])
        msg = _render_chain_exhaustion_message(exc)
        assert "primary" in msg
        assert "kernos setup llm" in msg

    @pytest.mark.asyncio
    async def test_call_chain_raises_llm_chain_exhausted_when_all_fail(self):
        """Core contract: _call_chain raises LLMChainExhausted on full failure."""
        from kernos.kernel.exceptions import (
            LLMChainExhausted,
            ReasoningProviderError,
        )
        from kernos.providers.base import ChainEntry

        class FailProvider:
            provider_name = "fail"
            async def complete(self, **kw):
                raise ReasoningProviderError("simulated provider failure")

        # Build a minimal ReasoningService-like object for _call_chain.
        # We only need: self._chains, self._trace, self._handler.
        from kernos.kernel.reasoning import ReasoningService

        svc = ReasoningService.__new__(ReasoningService)
        svc._chains = {
            "primary": [
                ChainEntry(provider=FailProvider(), model="m1"),
                ChainEntry(provider=FailProvider(), model="m2"),
            ]
        }
        svc._trace = lambda *a, **kw: None
        svc._handler = None
        # Pre-flight skip support — empty catalog disables skip; both
        # entries route normally and the fail-after-call path still
        # raises LLMChainExhausted.
        svc._catalog_cards = {}
        svc._unknown_model_warned = set()

        with pytest.raises(LLMChainExhausted) as excinfo:
            await svc._call_chain(
                "primary", "sys", [], [], max_tokens=10,
            )
        assert excinfo.value.chain_name == "primary"
        assert len(excinfo.value.attempts) == 2

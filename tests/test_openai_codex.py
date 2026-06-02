"""Tests for OpenAI Codex OAuth provider and credential resolution.

Tests the chatgpt.com/backend-api/codex/responses path, NOT api.openai.com.
"""
import base64
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kernos.kernel.credentials import (
    OpenAICodexCredential,
    _decode_jwt_account_id,
    resolve_openai_codex_credential,
)
from kernos.kernel.reasoning import (
    ContentBlock,
    OpenAICodexProvider,
    ProviderResponse,
)


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------


def _make_jwt(account_id: str = "acct_test123") -> str:
    """Create a minimal JWT with the expected claim structure."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(json.dumps({
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
        },
        "exp": int(time.time()) + 86400,
    }).encode()).rstrip(b"=")
    sig = base64.urlsafe_b64encode(b"fakesig").rstrip(b"=")
    return f"{header.decode()}.{payload.decode()}.{sig.decode()}"


class TestJWTAccountId:
    def test_extracts_account_id(self):
        jwt = _make_jwt("acct_abc123")
        assert _decode_jwt_account_id(jwt) == "acct_abc123"

    def test_raises_on_missing_claim(self):
        header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).rstrip(b"=")
        payload = base64.urlsafe_b64encode(json.dumps({"sub": "user"}).encode()).rstrip(b"=")
        sig = base64.urlsafe_b64encode(b"sig").rstrip(b"=")
        bad_jwt = f"{header.decode()}.{payload.decode()}.{sig.decode()}"
        with pytest.raises(ValueError, match="accountId"):
            _decode_jwt_account_id(bad_jwt)

    def test_raises_on_invalid_jwt(self):
        with pytest.raises(ValueError):
            _decode_jwt_account_id("not-a-jwt")


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------


class TestCodexCredentialFromEnv:
    def test_resolves_from_env(self, monkeypatch):
        jwt = _make_jwt("acct_env")
        monkeypatch.setenv("OPENAI_CODEX_ACCESS_TOKEN", jwt)
        monkeypatch.setenv("OPENAI_CODEX_REFRESH_TOKEN", "refresh_xxx")
        monkeypatch.setenv("OPENAI_CODEX_EXPIRES", str(int(time.time() * 1000) + 86400000))
        monkeypatch.setenv("OPENAI_CODEX_ACCOUNT_ID", "acct_env")

        creds = resolve_openai_codex_credential()
        assert creds["access"] == jwt
        assert creds["refresh"] == "refresh_xxx"
        assert creds["accountId"] == "acct_env"

    def test_extracts_account_from_jwt_when_not_in_env(self, monkeypatch):
        jwt = _make_jwt("acct_from_jwt")
        monkeypatch.setenv("OPENAI_CODEX_ACCESS_TOKEN", jwt)
        monkeypatch.setenv("OPENAI_CODEX_REFRESH_TOKEN", "refresh_xxx")
        monkeypatch.delenv("OPENAI_CODEX_ACCOUNT_ID", raising=False)

        creds = resolve_openai_codex_credential()
        assert creds["accountId"] == "acct_from_jwt"


class TestCodexCredentialFromFile:
    def test_resolves_from_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OPENAI_CODEX_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("OPENAI_CODEX_REFRESH_TOKEN", raising=False)

        jwt = _make_jwt("acct_file")
        creds_file = tmp_path / "openai-codex.json"
        creds_file.write_text(json.dumps({
            "access": jwt,
            "refresh": "refresh_file",
            "expires": int(time.time() * 1000) + 86400000,
            "accountId": "acct_file",
        }))
        monkeypatch.setenv("OPENAI_CODEX_CREDS_PATH", str(creds_file))

        creds = resolve_openai_codex_credential()
        assert creds["access"] == jwt
        assert creds["accountId"] == "acct_file"

    def test_raises_when_no_source(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OPENAI_CODEX_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("OPENAI_CODEX_REFRESH_TOKEN", raising=False)
        monkeypatch.setenv("OPENAI_CODEX_CREDS_PATH", str(tmp_path / "nonexistent.json"))
        # Isolate from any real ~/.codex/auth.json on the host so the
        # CLI-auth fallback can't satisfy the resolver.
        monkeypatch.setenv(
            "KERNOS_CODEX_CLI_AUTH_PATH",
            str(tmp_path / "no-cli-auth.json"),
        )

        with pytest.raises(ValueError, match="No OpenAI Codex credentials"):
            resolve_openai_codex_credential()


# ---------------------------------------------------------------------------
# Provider: input translation (Anthropic → Responses API)
# ---------------------------------------------------------------------------


class TestCodexInputTranslation:
    """_translate_input converts Anthropic messages to Responses API input items."""

    def test_plain_user_message(self):
        items = OpenAICodexProvider._translate_input([
            {"role": "user", "content": "Hello"},
        ])
        assert len(items) == 1
        assert items[0]["type"] == "message"
        assert items[0]["role"] == "user"
        assert items[0]["content"][0]["type"] == "input_text"
        assert items[0]["content"][0]["text"] == "Hello"

    def test_plain_assistant_message(self):
        items = OpenAICodexProvider._translate_input([
            {"role": "assistant", "content": "Hi there"},
        ])
        assert len(items) == 1
        assert items[0]["type"] == "message"
        assert items[0]["role"] == "assistant"
        assert items[0]["content"][0]["type"] == "output_text"

    def test_tool_use_blocks(self):
        items = OpenAICodexProvider._translate_input([
            {"role": "assistant", "content": [
                {"type": "text", "text": "Let me check."},
                {"type": "tool_use", "id": "tc_1", "name": "list-events", "input": {"date": "2026-01-01"}},
            ]},
        ])
        # Should produce: text message + function_call item
        text_items = [i for i in items if i["type"] == "message"]
        call_items = [i for i in items if i["type"] == "function_call"]
        assert len(text_items) == 1
        assert text_items[0]["content"][0]["text"] == "Let me check."
        assert len(call_items) == 1
        assert call_items[0]["call_id"] == "tc_1"
        assert call_items[0]["name"] == "list-events"
        assert json.loads(call_items[0]["arguments"]) == {"date": "2026-01-01"}

    def test_tool_result_blocks(self):
        items = OpenAICodexProvider._translate_input([
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tc_1", "content": "Meeting at 10am"},
            ]},
        ])
        assert len(items) == 1
        assert items[0]["type"] == "function_call_output"
        assert items[0]["call_id"] == "tc_1"
        assert items[0]["output"] == "Meeting at 10am"


# ---------------------------------------------------------------------------
# Provider: tool translation
# ---------------------------------------------------------------------------


class TestCodexToolTranslation:
    def test_translates_anthropic_format(self):
        tools = [
            {"name": "list-events", "description": "List events", "input_schema": {
                "type": "object", "properties": {"date": {"type": "string"}},
            }},
        ]
        oai = OpenAICodexProvider._translate_tools(tools)
        assert len(oai) == 1
        assert oai[0]["type"] == "function"
        assert oai[0]["name"] == "list-events"
        assert "date" in oai[0]["parameters"]["properties"]


# ---------------------------------------------------------------------------
# Provider: response parsing (Responses API format)
# ---------------------------------------------------------------------------


class TestCodexResponseParsing:
    def test_parses_text_response(self):
        data = {
            "status": "completed",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hello!"}],
            }],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        resp = OpenAICodexProvider._parse_response(data)
        assert resp.stop_reason == "end_turn"
        assert len(resp.content) == 1
        assert resp.content[0].text == "Hello!"
        assert resp.input_tokens == 10

    def test_parses_tool_call_response(self):
        data = {
            "status": "completed",
            "output": [{
                "type": "function_call",
                "call_id": "call_1",
                "name": "create-event",
                "arguments": '{"title": "Meeting"}',
            }],
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }
        resp = OpenAICodexProvider._parse_response(data)
        assert resp.stop_reason == "tool_use"
        assert len(resp.content) == 1
        assert resp.content[0].type == "tool_use"
        assert resp.content[0].name == "create-event"
        assert resp.content[0].input == {"title": "Meeting"}
        assert resp.content[0].id == "call_1"

    def test_parses_mixed_text_and_tool(self):
        data = {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Creating event."}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_2",
                    "name": "create-event",
                    "arguments": '{"title": "Test"}',
                },
            ],
            "usage": {"input_tokens": 50, "output_tokens": 30},
        }
        resp = OpenAICodexProvider._parse_response(data)
        assert resp.stop_reason == "tool_use"
        assert len(resp.content) == 2
        assert resp.content[0].type == "text"
        assert resp.content[1].type == "tool_use"

    def test_handles_empty_output(self):
        resp = OpenAICodexProvider._parse_response({"output": [], "status": "completed"})
        assert resp.stop_reason == "end_turn"
        assert resp.content[0].text == ""

    def test_incomplete_status_maps_to_max_tokens(self):
        data = {
            "status": "incomplete",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "Partial"}]}],
            "usage": {},
        }
        resp = OpenAICodexProvider._parse_response(data)
        assert resp.stop_reason == "max_tokens"


# ---------------------------------------------------------------------------
# Provider: request headers
# ---------------------------------------------------------------------------


class TestCodexHeaders:
    def test_headers_include_required_fields(self):
        cred = OpenAICodexCredential(
            access="token_abc", refresh="ref", expires=0, accountId="acct_123",
        )
        provider = OpenAICodexProvider(credential=cred)
        headers = provider._headers()
        assert headers["Authorization"] == "Bearer token_abc"
        assert headers["chatgpt-account-id"] == "acct_123"
        assert headers["originator"] == "pi"
        # OS-aware UA matching openclaw's shape: "pi (<system> <release>; <machine>)".
        assert headers["User-Agent"].startswith("pi (")
        assert headers["OpenAI-Beta"] == "responses=experimental"
        # Without a session_id, the session-correlation headers are absent.
        assert "session_id" not in headers
        assert "x-client-request-id" not in headers

    def test_headers_include_session_correlation_when_provided(self):
        cred = OpenAICodexCredential(
            access="token_abc", refresh="ref", expires=0, accountId="acct_123",
        )
        provider = OpenAICodexProvider(credential=cred)
        headers = provider._headers(session_id="conv-42")
        assert headers["session_id"] == "conv-42"
        assert headers["x-client-request-id"] == "conv-42"


# ---------------------------------------------------------------------------
# Provider: URL resolution
# ---------------------------------------------------------------------------


class TestCodexURL:
    def test_default_url(self):
        cred = OpenAICodexCredential(access="x", refresh="r", expires=0, accountId="a")
        provider = OpenAICodexProvider(credential=cred)
        url = provider._resolve_url()
        assert url == "https://chatgpt.com/backend-api/codex/responses"

    def test_custom_base_url(self, monkeypatch):
        monkeypatch.setenv("OPENAI_CODEX_BASE_URL", "https://custom.example.com/api")
        cred = OpenAICodexCredential(access="x", refresh="r", expires=0, accountId="a")
        provider = OpenAICodexProvider(credential=cred)
        url = provider._resolve_url()
        assert url == "https://custom.example.com/api/codex/responses"

    def test_url_already_has_codex_responses(self):
        cred = OpenAICodexCredential(access="x", refresh="r", expires=0, accountId="a")
        provider = OpenAICodexProvider(credential=cred)
        provider._base_url = "https://chatgpt.com/backend-api/codex/responses"
        assert provider._resolve_url() == "https://chatgpt.com/backend-api/codex/responses"


# ---------------------------------------------------------------------------
# Provider: model defaults
# ---------------------------------------------------------------------------


class TestCodexModelDefaults:
    def test_default_model(self):
        cred = OpenAICodexCredential(access="x", refresh="r", expires=0, accountId="a")
        provider = OpenAICodexProvider(credential=cred)
        assert provider.main_model
        assert provider.cheap_model

    def test_custom_model(self):
        cred = OpenAICodexCredential(access="x", refresh="r", expires=0, accountId="a")
        provider = OpenAICodexProvider(credential=cred, model="gpt-5")
        assert provider.main_model == "gpt-5"


# ---------------------------------------------------------------------------
# Provider: NOT using chat/completions
# ---------------------------------------------------------------------------


class TestCodexNotChatCompletions:
    """Verify the provider does NOT hit api.openai.com/v1/chat/completions."""

    def test_url_is_not_chat_completions(self):
        cred = OpenAICodexCredential(access="x", refresh="r", expires=0, accountId="a")
        provider = OpenAICodexProvider(credential=cred)
        url = provider._resolve_url()
        assert "chat/completions" not in url
        assert "chatgpt.com/backend-api" in url
        assert "codex/responses" in url

    def test_request_body_uses_responses_format(self):
        """The body should use 'instructions' and 'input', not 'messages'."""
        # This is verified by the _translate_input method producing Responses items
        items = OpenAICodexProvider._translate_input([
            {"role": "user", "content": "Test"},
        ])
        # Responses API uses typed items, not chat messages
        assert items[0]["type"] == "message"
        assert items[0]["content"][0]["type"] == "input_text"


# ---------------------------------------------------------------------------
# Provider: wire-shape repair fields
# ---------------------------------------------------------------------------


class TestCodexWireShape:
    """Verify the body fields added in CODEX-WIRE-SHAPE-REPAIR (2026-04-25)."""

    @staticmethod
    def _stub_provider(monkeypatch, captured: dict):
        """Build a provider whose http stream captures the body and returns a stub SSE."""
        cred = OpenAICodexCredential(
            access="tok", refresh="ref", expires=0, accountId="acct",
        )
        provider = OpenAICodexProvider(credential=cred)

        async def fake_ensure_valid_token():
            return None

        provider._ensure_valid_token = fake_ensure_valid_token  # type: ignore[assignment]

        from contextlib import asynccontextmanager

        class FakeResp:
            status_code = 200

            async def aread(self):
                return b""

            @property
            def text(self):
                return ""

        class FakeHttp:
            @asynccontextmanager
            async def stream(self_, method, url, *, headers, json):  # noqa: N805
                captured["method"] = method
                captured["url"] = url
                captured["headers"] = dict(headers)
                captured["body"] = json
                yield FakeResp()

        async def fake_ensure_http():
            return FakeHttp()

        async def fake_collect(resp):
            return {"status": "completed", "output": [], "usage": {"input_tokens": 0, "output_tokens": 0}}

        provider._ensure_http = fake_ensure_http  # type: ignore[assignment]
        provider._collect_sse_response = fake_collect  # type: ignore[assignment]
        return provider

    async def test_body_includes_prompt_cache_key_when_conversation_id_set(self, monkeypatch):
        captured: dict = {}
        provider = self._stub_provider(monkeypatch, captured)
        await provider.complete(
            model="gpt-5.5",
            system="rules",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            max_tokens=1024,
            conversation_id="conv-xyz",
        )
        assert captured["body"]["prompt_cache_key"] == "conv-xyz"
        assert captured["headers"]["session_id"] == "conv-xyz"
        assert captured["headers"]["x-client-request-id"] == "conv-xyz"

    async def test_body_omits_prompt_cache_key_when_conversation_id_blank(self, monkeypatch):
        captured: dict = {}
        provider = self._stub_provider(monkeypatch, captured)
        await provider.complete(
            model="gpt-5.5",
            system="rules",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            max_tokens=1024,
        )
        assert "prompt_cache_key" not in captured["body"]
        assert "session_id" not in captured["headers"]

    async def test_body_includes_reasoning_for_gpt5_models(self, monkeypatch):
        captured: dict = {}
        provider = self._stub_provider(monkeypatch, captured)
        await provider.complete(
            model="gpt-5.5",
            system="rules",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            max_tokens=1024,
        )
        assert captured["body"]["reasoning"] == {"effort": "medium", "summary": "auto"}

    async def test_body_omits_reasoning_for_non_gpt5_models(self, monkeypatch):
        captured: dict = {}
        provider = self._stub_provider(monkeypatch, captured)
        await provider.complete(
            model="o3-mini",
            system="rules",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            max_tokens=1024,
        )
        assert "reasoning" not in captured["body"]

    async def test_body_includes_reasoning_encrypted_content(self, monkeypatch):
        captured: dict = {}
        provider = self._stub_provider(monkeypatch, captured)
        await provider.complete(
            model="gpt-5.5",
            system="rules",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            max_tokens=1024,
        )
        assert captured["body"]["include"] == ["reasoning.encrypted_content"]

    async def test_body_sets_text_verbosity_for_freeform_responses(self, monkeypatch):
        captured: dict = {}
        provider = self._stub_provider(monkeypatch, captured)
        await provider.complete(
            model="gpt-5.5",
            system="rules",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            max_tokens=1024,
        )
        assert captured["body"]["text"] == {"verbosity": "medium"}

    async def test_body_tools_carry_explicit_strict_null(self, monkeypatch):
        """Pin: every translated tool MUST include ``strict: None`` as a
        top-level key. Live-replay 2026-05-02 confirmed that the Codex
        consumer backend treats a missing ``strict`` key differently from
        ``strict: null`` on payloads with real tool schemas: without the
        explicit null, ~47KB calls reliably mid-stream-fail with
        ``server_error`` (0/3 pass rate), with the null they pass (5/5).
        OpenClaw's installed transport uses the same explicit-null pattern.
        """
        captured: dict = {}
        provider = self._stub_provider(monkeypatch, captured)
        tools = [
            {"name": "tool_a", "description": "A",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "tool_b", "description": "B",
             "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}},
        ]
        await provider.complete(
            model="gpt-5.5",
            system="rules",
            messages=[{"role": "user", "content": "hi"}],
            tools=tools,
            max_tokens=1024,
        )
        sent_tools = captured["body"]["tools"]
        for t in sent_tools:
            assert "strict" in t, (
                f"Every tool MUST have a 'strict' key (set to None). "
                f"Missing on tool: {t.get('name')!r}. "
                f"This is the wire-shape trigger for the Codex consumer "
                f"backend's mid-stream server_error on >40KB payloads with "
                f"real tool schemas. Live-replay 2026-05-02 pinned this."
            )
            assert t["strict"] is None, (
                f"Tool {t.get('name')!r} has strict={t.get('strict')!r}; "
                f"must be exactly None"
            )

    async def test_body_defaults_tool_choice_to_auto(self, monkeypatch):
        captured: dict = {}
        provider = self._stub_provider(monkeypatch, captured)
        await provider.complete(
            model="gpt-5.5",
            system="rules",
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"name": "finalize", "description": "F",
                    "input_schema": {"type": "object", "properties": {}}}],
            max_tokens=1024,
        )
        assert captured["body"]["tool_choice"] == "auto"

    async def test_body_forwards_required_tool_choice(self, monkeypatch):
        captured: dict = {}
        provider = self._stub_provider(monkeypatch, captured)
        await provider.complete(
            model="gpt-5.5",
            system="rules",
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"name": "finalize", "description": "F",
                    "input_schema": {"type": "object", "properties": {}}}],
            max_tokens=1024,
            tool_choice="required",
        )
        assert captured["body"]["tool_choice"] == "required"

    async def test_body_uses_schema_format_when_output_schema_provided(self, monkeypatch):
        captured: dict = {}
        provider = self._stub_provider(monkeypatch, captured)
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        await provider.complete(
            model="gpt-5.5",
            system="rules",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            max_tokens=1024,
            output_schema=schema,
        )
        # Schema mode wins; verbosity is not set when constrained decoding is on.
        assert captured["body"]["text"]["format"]["type"] == "json_schema"
        assert "verbosity" not in captured["body"]["text"]


# ---------------------------------------------------------------------------
# Auto-resync from Codex CLI auth (`codex login` bridge)
# ---------------------------------------------------------------------------


class TestCodexCliAutoResync:
    """When the user runs `codex login`, OpenAI rotates the refresh
    token and writes the new tokens to ~/.codex/auth.json. Kernos
    keeps a separate creds file at .credentials/openai-codex.json
    that does NOT auto-sync. Result pre-fix: every Codex call returned
    HTTP 401 until the operator manually regenerated the file. These
    tests pin the auto-resync path that bridges the two."""

    def _write_cli_auth(self, path, *, access_jwt: str, refresh: str,
                        account_id: str = "acct_cli") -> None:
        """Write a ~/.codex/auth.json-shaped file."""
        path.write_text(json.dumps({
            "auth_mode": "chatgpt",
            "tokens": {
                "access_token": access_jwt,
                "refresh_token": refresh,
                "account_id": account_id,
            },
            "last_refresh": "2026-05-07T00:00:00.000000Z",
        }))

    def test_resolve_picks_up_fresh_cli_auth_on_boot(
        self, tmp_path, monkeypatch,
    ):
        """When ~/.codex/auth.json is newer than Kernos's creds AND
        carries different tokens, resolve auto-syncs the new tokens
        into Kernos's creds file before reading."""
        monkeypatch.delenv("OPENAI_CODEX_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("OPENAI_CODEX_REFRESH_TOKEN", raising=False)

        # Stale Kernos creds (older mtime, old tokens).
        stale_jwt = _make_jwt("acct_stale")
        creds_file = tmp_path / "openai-codex.json"
        creds_file.write_text(json.dumps({
            "access": stale_jwt,
            "refresh": "stale_refresh",
            "expires": int(time.time() * 1000) + 86400000,
            "accountId": "acct_stale",
        }))
        # Make Kernos creds noticeably old.
        import os as _os
        old_time = time.time() - 86400
        _os.utime(str(creds_file), (old_time, old_time))

        # Fresh CLI auth (newer mtime, new tokens).
        fresh_jwt = _make_jwt("acct_fresh")
        cli_auth = tmp_path / "auth.json"
        self._write_cli_auth(
            cli_auth, access_jwt=fresh_jwt, refresh="fresh_refresh",
            account_id="acct_fresh",
        )

        monkeypatch.setenv("OPENAI_CODEX_CREDS_PATH", str(creds_file))
        monkeypatch.setenv("KERNOS_CODEX_CLI_AUTH_PATH", str(cli_auth))

        creds = resolve_openai_codex_credential()
        # Resolved credentials are the fresh ones from the CLI auth.
        assert creds["access"] == fresh_jwt
        assert creds["refresh"] == "fresh_refresh"
        assert creds["accountId"] == "acct_fresh"
        # And the Kernos creds file was rewritten so subsequent boots
        # don't repeat the resync work.
        persisted = json.loads(creds_file.read_text())
        assert persisted["access"] == fresh_jwt

    def test_resolve_keeps_kernos_creds_when_cli_auth_is_older(
        self, tmp_path, monkeypatch,
    ):
        """If Kernos's creds are at least as fresh as the CLI auth,
        no resync happens — avoids overwriting newer tokens with
        older ones (e.g. if Kernos refreshed via OAuth recently)."""
        monkeypatch.delenv("OPENAI_CODEX_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("OPENAI_CODEX_REFRESH_TOKEN", raising=False)

        kernos_jwt = _make_jwt("acct_kernos")
        creds_file = tmp_path / "openai-codex.json"
        creds_file.write_text(json.dumps({
            "access": kernos_jwt,
            "refresh": "kernos_refresh",
            "expires": int(time.time() * 1000) + 86400000,
            "accountId": "acct_kernos",
        }))

        # Older CLI auth.
        old_jwt = _make_jwt("acct_old")
        cli_auth = tmp_path / "auth.json"
        self._write_cli_auth(
            cli_auth, access_jwt=old_jwt, refresh="old_refresh",
            account_id="acct_old",
        )
        import os as _os
        old_time = time.time() - 86400
        _os.utime(str(cli_auth), (old_time, old_time))

        monkeypatch.setenv("OPENAI_CODEX_CREDS_PATH", str(creds_file))
        monkeypatch.setenv("KERNOS_CODEX_CLI_AUTH_PATH", str(cli_auth))

        creds = resolve_openai_codex_credential()
        assert creds["access"] == kernos_jwt
        # Kernos creds file is unchanged.
        persisted = json.loads(creds_file.read_text())
        assert persisted["access"] == kernos_jwt

    def test_resolve_hydrates_from_cli_auth_when_creds_missing(
        self, tmp_path, monkeypatch,
    ):
        """Last-resort path: Kernos creds file doesn't exist yet,
        but ~/.codex/auth.json does. Resolve hydrates from the CLI
        auth and writes the creds file (so next boot is fast)."""
        monkeypatch.delenv("OPENAI_CODEX_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("OPENAI_CODEX_REFRESH_TOKEN", raising=False)

        fresh_jwt = _make_jwt("acct_hydrate")
        cli_auth = tmp_path / "auth.json"
        self._write_cli_auth(
            cli_auth, access_jwt=fresh_jwt, refresh="hydrate_refresh",
            account_id="acct_hydrate",
        )

        creds_file = tmp_path / "openai-codex.json"
        # Note: NO creds_file.write_text — file does not exist.
        monkeypatch.setenv("OPENAI_CODEX_CREDS_PATH", str(creds_file))
        monkeypatch.setenv("KERNOS_CODEX_CLI_AUTH_PATH", str(cli_auth))

        creds = resolve_openai_codex_credential()
        assert creds["access"] == fresh_jwt
        # Hydration persisted creds for next boot.
        assert creds_file.exists()

    def test_refresh_recovers_from_401_via_cli_auth(
        self, tmp_path, monkeypatch,
    ):
        """When the OAuth refresh returns 401 (rotated refresh token),
        the refresh path reads ~/.codex/auth.json. If it carries
        different tokens, those are persisted and returned without
        re-running OAuth — the CLI auth's access token is fresh from
        the recent login."""
        import urllib.error
        from kernos.kernel.credentials import refresh_openai_codex_credential

        # Stale Kernos creds carrying the rotated-out refresh token.
        stale_jwt = _make_jwt("acct_stale")
        stale_creds = OpenAICodexCredential(
            access=stale_jwt,
            refresh="stale_refresh_rotated_out",
            expires=int(time.time() * 1000) - 1000,  # expired
            accountId="acct_stale",
        )

        # Fresh CLI auth.
        fresh_jwt = _make_jwt("acct_fresh")
        cli_auth = tmp_path / "auth.json"
        self._write_cli_auth(
            cli_auth, access_jwt=fresh_jwt,
            refresh="fresh_cli_refresh", account_id="acct_fresh",
        )

        creds_file = tmp_path / "openai-codex.json"
        monkeypatch.setenv("OPENAI_CODEX_CREDS_PATH", str(creds_file))
        monkeypatch.setenv("KERNOS_CODEX_CLI_AUTH_PATH", str(cli_auth))

        # Mock urlopen to raise 401 — exactly what OpenAI returns when
        # the refresh token is no longer valid.
        def _raise_401(*_a, **_kw):
            raise urllib.error.HTTPError(
                url="", code=401, msg="Unauthorized", hdrs={}, fp=None,
            )

        with patch("urllib.request.urlopen", side_effect=_raise_401):
            recovered = pytest.run(
                refresh_openai_codex_credential(stale_creds),
            ) if False else None  # placeholder; real call below
        # The above pattern isn't async-friendly; do it directly:
        import asyncio
        with patch("urllib.request.urlopen", side_effect=_raise_401):
            recovered = asyncio.get_event_loop().run_until_complete(
                refresh_openai_codex_credential(stale_creds),
            ) if False else asyncio.run(
                refresh_openai_codex_credential(stale_creds),
            )

        assert recovered["access"] == fresh_jwt
        assert recovered["refresh"] == "fresh_cli_refresh"
        # Kernos creds file was updated by the recovery path.
        persisted = json.loads(creds_file.read_text())
        assert persisted["access"] == fresh_jwt

    def test_refresh_raises_when_401_and_no_cli_auth(
        self, tmp_path, monkeypatch,
    ):
        """If OAuth returns 401 AND no usable CLI auth exists, the
        refresh path raises ReasoningConnectionError — preserves the
        original loud-fail behavior so the operator sees the issue."""
        import urllib.error
        from kernos.kernel.credentials import refresh_openai_codex_credential
        from kernos.kernel.reasoning import ReasoningConnectionError

        creds = OpenAICodexCredential(
            access=_make_jwt("acct"),
            refresh="any",
            expires=int(time.time() * 1000) - 1000,
            accountId="acct",
        )

        creds_file = tmp_path / "openai-codex.json"
        monkeypatch.setenv("OPENAI_CODEX_CREDS_PATH", str(creds_file))
        monkeypatch.setenv(
            "KERNOS_CODEX_CLI_AUTH_PATH",
            str(tmp_path / "no-cli-auth.json"),
        )

        def _raise_401(*_a, **_kw):
            raise urllib.error.HTTPError(
                url="", code=401, msg="Unauthorized", hdrs={}, fp=None,
            )

        import asyncio
        with patch("urllib.request.urlopen", side_effect=_raise_401):
            with pytest.raises(ReasoningConnectionError, match="refresh failed"):
                asyncio.run(refresh_openai_codex_credential(creds))

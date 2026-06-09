"""Agent-facing ``consult`` tool — schema + service factory.

C7 wiring: the orchestrator + harnesses + log already exist; this
module exposes the agent surface so the primary agent can actually
call them. Mirrors the shape of ``EXECUTE_CODE_TOOL`` in
``code_exec.py``: a tool schema dict (consumed by the assemble
phase) plus a small singleton helper that builds a registered
orchestrator on first use and reuses it thereafter.

The service is process-singleton because the consultation_log
opens a sqlite connection on ``start()`` and must not be opened
twice. Engine bring-up (server.py, app.py, etc.) calls
:func:`get_service` once during init; later turns reuse the same
instance via the ``data_dir`` it was constructed with.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from kernos.kernel.external_agents.consultation_log import ConsultationLog
from kernos.kernel.external_agents.harnesses.aider import AiderHarness
from kernos.kernel.external_agents.harnesses.claude_code import (
    ClaudeCodeHarness,
)
from kernos.kernel.external_agents.harnesses.codex import CodexHarness
from kernos.kernel.external_agents.harnesses.gemini import GeminiHarness
from kernos.kernel.external_agents.orchestrator import (
    ConsultationOrchestrator,
    WorkspacePolicy,
)
from kernos.kernel.external_agents.registry import HarnessRegistry

logger = logging.getLogger(__name__)


# Single source of truth — schema enum, handler validator, and error
# messages all read from here. Adding a harness means appending to
# this tuple and registering the harness in _build_default_registry.
SUPPORTED_CONSULT_HARNESSES: tuple[str, ...] = (
    "claude_code", "codex", "gemini",
)

# Forgiving harness resolution (v1 self-test Test 16): the model reaches for
# near-miss names and sometimes parks the prompt in `harness`. Map common
# aliases to the canonical enum; an unrecognized harness with a real question
# falls back to the default rather than hard-rejecting the whole consult.
_DEFAULT_CONSULT_HARNESS = "codex"
_CONSULT_HARNESS_ALIASES: dict[str, str] = {
    "claude": "claude_code", "claude-code": "claude_code", "claudecode": "claude_code",
    "cc": "claude_code", "anthropic": "claude_code",
    "openai": "codex", "gpt": "codex", "gpt-5": "codex", "gpt5": "codex",
    "chatgpt": "codex", "oai": "codex", "cdx": "codex",
    "google": "gemini", "bard": "gemini", "gemini-pro": "gemini",
}


def _canonical_harness(value: str) -> str:
    """Resolve a harness name to its canonical enum value, or "" if unknown."""
    v = (value or "").strip().lower()
    if v in SUPPORTED_CONSULT_HARNESSES:
        return v
    return _CONSULT_HARNESS_ALIASES.get(v, "")


CONSULT_TOOL = {
    "name": "consult",
    "description": (
        "TWO REQUIRED ARGS: `harness` (string, one of "
        "claude_code/codex/gemini) AND `question` (string, the "
        "prompt for the external agent). Both must be non-empty. "
        "If you supply only `harness` without `question`, the "
        "tool returns InvalidConsultCall. Common mistake: using "
        "`prompt` instead of `question` — the handler accepts "
        "`prompt` as a single alias, but the canonical field name "
        "is `question`. If both `question` and `prompt` are "
        "supplied with different non-empty values, the handler "
        "returns InvalidConsultCall rather than guessing.\n\n"
        "Concrete examples:\n"
        "  consult(harness=\"codex\", question=\"Review the diff at HEAD\")\n"
        "  consult(harness=\"claude_code\", question=\"Write a 2-line poem\")\n"
        "\n"
        "Calling with a wrong tool name (e.g. "
        "external_agent_consult.cc, consult_codex, "
        "codex_async_advisory) returns 'tool not registered' — "
        "there is exactly ONE tool here and its name is `consult`.\n\n"
        "Decision rule: need the answer before your next step → "
        "`consult` (blocks in-turn). Can keep working → "
        "`ask_coding_session` (returns request_id; poll later). "
        "Same external CLIs, same ACPX substrate; only the blocking "
        "model differs.\n\n"
        "Dispatches synchronously through the ACPX adapter (Agent "
        "Client Protocol) to a fresh CLI invocation. The external "
        "agent reasons, uses its own tools (grep, read, run, etc.), "
        "and returns free-text. General-purpose: implement, "
        "refactor, debug, architect, review, test, document, "
        "explore, explain, generate, experiment — whatever the "
        "harness can do.\n\n"
        "When to use: high uncertainty about an unfamiliar area, "
        "large search space, independent review, expensive "
        "reasoning, or a perspective you don't have. When NOT to "
        "use: anything you can complete locally faster than "
        "describing + waiting + integrating; simple grep/read "
        "lookups; routine work where the answer is obvious. Final "
        "user-facing composition stays local — `consult` informs "
        "your answer, doesn't replace it.\n\n"
        "Filesystem effects: the external agent has read AND WRITE "
        "access to the repo by default (it can edit files, run "
        "commands, etc.). Treat its actions as effects in the "
        "world, not just advisory text. If you want advisory-only, "
        "ask for that explicitly in your prompt.\n\n"
        "Threading: pass the same `session_id` across calls to "
        "preserve the external agent's conversation context (the "
        "ACPX named-session keeps its turn history). Each "
        "invocation spawns a fresh process — any file changes it "
        "made persist on disk, but in-process scratch state does "
        "not.\n\n"
        "Inline relevant snippets, prior analysis, or links in the "
        "`context` field to scope the agent's investigation.\n\n"
        "Backend note: Aider is BUILD-only and not reachable via "
        "this tool or `ask_coding_session` — use `execute_code` "
        "with `backend='aider'` for task-shaped CLI work."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "harness": {
                "type": "string",
                "minLength": 1,
                "enum": list(SUPPORTED_CONSULT_HARNESSES),
                "description": (
                    "REQUIRED, must be one of the enum values: "
                    "'claude_code', 'codex', 'gemini'. The schema "
                    "validator hard-rejects any other value including "
                    "the empty string, missing field, and common "
                    "near-misses ('cc', 'claude', 'gpt'). If you "
                    "need a harness not in the enum, talk to the "
                    "operator about adding it — don't invent a name. "
                    "Pick based on what fits the work: these are "
                    "general-purpose agentic CLIs; differences are in "
                    "training/style/availability rather than fixed "
                    "task scope. Threading support varies by harness."
                ),
            },
            "question": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "REQUIRED, non-empty. The prompt / question to ask."
                ),
            },
            "context": {
                "type": "string",
                "description": (
                    "Optional context to inline with the prompt — "
                    "code snippets, prior analysis, etc."
                ),
            },
            "session_id": {
                "type": "string",
                "description": (
                    "Optional. Pass the same value across calls to "
                    "thread a multi-turn consultation. Empty for a "
                    "fresh single-turn ask."
                ),
            },
            "workspace_dir": {
                "type": "string",
                "description": (
                    "Optional path the harness can read. Defaults to "
                    "the Kernos repo root."
                ),
            },
            "timeout_seconds": {
                "type": "integer",
                "description": (
                    "Per-call timeout. Default 600, max 1800."
                ),
            },
        },
        "required": ["harness", "question"],
    },
}


# ----- input validation (pure helper, testable without handler) ----


def _coerce_str(value: Any) -> str:
    """Return ``value`` as a stripped string if it is a string, else "".
    Codex caught the original ``.strip()`` directly on the get-result
    which crashed when the agent passed an int, list, or None for
    ``question``/``harness``."""
    if isinstance(value, str):
        return value.strip()
    return ""


def validate_consult_input(
    tool_input: dict | None,
) -> tuple[str, str] | dict:
    """Validate the agent's ``consult`` tool input.

    Returns ``(harness, question)`` on success, or an
    ``InvalidConsultCall`` error dict (ready to ``json.dumps``).

    Accepts a single alias: ``prompt`` for ``question``. Earlier
    drafts added ``text``/``target``/``agent`` but those were
    speculative — drop and surface clean errors instead. If both
    canonical and alias supply different non-empty values, refuse
    rather than guess which the agent meant.

    Pure function: no I/O, no global state, takes a dict and
    returns a value. Behavior tests call this directly with input
    dicts and assert on outputs, replacing the prior static-source
    tests that only proved error strings literally existed in the
    file.
    """
    if not isinstance(tool_input, dict):
        # Codex audit 2026-05-20: ``tool_input or {}`` only catches
        # None — a string or int would crash on the ``.get()`` call.
        # Treat any non-dict (None included) as an empty mapping and
        # let the missing-harness branch produce the canonical error.
        tool_input = {}
    _harness = _coerce_str(tool_input.get("harness"))
    _question = _coerce_str(tool_input.get("question"))
    _prompt = _coerce_str(tool_input.get("prompt"))

    # Alias conflict — both supplied with different non-empty values
    if _question and _prompt and _question != _prompt:
        return {
            "error": "InvalidConsultCall",
            "message": (
                "consult received both `question` and `prompt` with "
                "different non-empty values — refusing to guess. "
                "Pass only `question` (canonical) OR only `prompt` "
                "(alias)."
            ),
        }
    if not _question:
        _question = _prompt

    # --- Forgiving harness recovery (v1 self-test Test 16) ----------------
    # Two recoveries, both requiring CLEAR signal (a genuinely empty / short
    # typo'd harness still surfaces the clean error below — that's intentional):
    #   1. near-miss names (claude→claude_code, gpt→codex, CODEX→codex, …)
    #   2. the model parked the PROMPT in `harness` (long text / has spaces) —
    #      recover it as the question (swap when the name is in `question`,
    #      else default the harness).
    _canon = _canonical_harness(_harness)
    if not _canon and _harness:
        _looks_like_prompt = len(_harness) > 40 or " " in _harness
        _swapped = _canonical_harness(_question)
        if _looks_like_prompt and _swapped:
            _question, _canon = _harness, _swapped          # fields swapped
        elif _looks_like_prompt and not _question:
            _question, _canon = _harness, _DEFAULT_CONSULT_HARNESS  # prompt-in-harness
        # else: short unrecognized harness → leave _canon empty → clean error
    if _canon:
        _harness = _canon

    if not _harness:
        return {
            "error": "InvalidConsultCall",
            "message": (
                "consult requires non-empty `harness`. Valid values: "
                + ", ".join(SUPPORTED_CONSULT_HARNESSES)
                + '. Example: consult(harness="codex", '
                'question="...")'
            ),
        }
    if _harness not in SUPPORTED_CONSULT_HARNESSES:
        return {
            "error": "InvalidConsultCall",
            "message": (
                f"consult harness={_harness!r} is not registered. "
                f"Valid: " + ", ".join(SUPPORTED_CONSULT_HARNESSES)
                + "."
            ),
        }
    if not _question:
        return {
            "error": "InvalidConsultCall",
            "message": (
                "consult requires non-empty `question` (alias: "
                "`prompt`). Pass the prompt you want sent to the "
                "external agent. Example: "
                f'consult(harness="{_harness}", '
                'question="Reply with: hello")'
            ),
        }
    return (_harness, _question)


# ----- service singleton -------------------------------------------

_service_lock = asyncio.Lock()
_service: "ExternalAgentService | None" = None


class ExternalAgentService:
    """Process-singleton wrapping the orchestrator + log.

    Engine bring-up calls :func:`get_service` to obtain the instance;
    the first call triggers ``start()`` which opens the sqlite
    connection backing ``consultation_log``. Subsequent calls reuse
    the same instance.
    """

    def __init__(
        self,
        *,
        data_dir: str,
        registry: HarnessRegistry,
        workspace_policy: WorkspacePolicy | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._registry = registry
        self._log = ConsultationLog()
        self._orchestrator = ConsultationOrchestrator(
            registry=registry,
            log=self._log,
            workspace_policy=workspace_policy,
        )
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        await self._log.start(self._data_dir)
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        await self._log.stop()
        self._started = False

    @property
    def orchestrator(self) -> ConsultationOrchestrator:
        return self._orchestrator

    @property
    def registry(self) -> HarnessRegistry:
        return self._registry

    @property
    def log(self) -> ConsultationLog:
        return self._log

    @property
    def data_dir(self) -> str:
        return self._data_dir


def _build_default_registry(*, data_dir: str) -> HarnessRegistry:
    """Register the four shipped harnesses with their mode flags.
    claude_code / codex / gemini support consult; aider supports
    build only (its CLI is task-shaped). Gemini history is rooted
    under ``<data_dir>/consultations`` so threading state lives
    alongside other instance-scoped state instead of /tmp."""
    registry = HarnessRegistry()
    registry.register(
        ClaudeCodeHarness(),
        consult_supported=True, build_supported=False,
    )
    registry.register(
        CodexHarness(),
        consult_supported=True, build_supported=False,
    )
    registry.register(
        GeminiHarness(
            history_root=Path(data_dir) / "consultations",
        ),
        consult_supported=True, build_supported=False,
    )
    registry.register(
        AiderHarness(),
        consult_supported=False, build_supported=True,
    )
    return registry


async def get_service(
    *, data_dir: str | None = None,
) -> ExternalAgentService:
    """Return the process-wide service, constructing + starting it
    on first call. ``data_dir`` only matters on first call; later
    callers pass ``None`` and get the same instance.

    Repo-root workspace allowlist is enforced when
    ``KERNOS_EXTERNAL_AGENT_ALLOWLIST`` is set (colon-separated
    paths). Default: no allowlist (any workspace_dir accepted).
    """
    global _service
    async with _service_lock:
        if _service is not None:
            return _service
        resolved_data = data_dir or os.getenv(
            "KERNOS_DATA_DIR", "./data",
        )
        allowlist_env = os.getenv(
            "KERNOS_EXTERNAL_AGENT_ALLOWLIST", "",
        )
        if allowlist_env:
            paths = tuple(
                Path(p).expanduser().resolve()
                for p in allowlist_env.split(":")
                if p.strip()
            )
            policy = WorkspacePolicy(allowlist=paths)
        else:
            policy = None
        service = ExternalAgentService(
            data_dir=resolved_data,
            registry=_build_default_registry(data_dir=resolved_data),
            workspace_policy=policy,
        )
        await service.start()
        _service = service
        logger.info(
            "EXTERNAL_AGENT_SERVICE_STARTED data_dir=%s allowlist=%s",
            resolved_data,
            len(allowlist_env.split(":")) if allowlist_env else 0,
        )
        return _service


async def reset_service_for_tests() -> None:
    """Test helper: stop and clear the singleton so the next
    ``get_service`` call rebuilds. Production callers must not use
    this — tearing down the log mid-turn would lose audit rows."""
    global _service
    async with _service_lock:
        if _service is not None:
            await _service.stop()
            _service = None


__all__ = [
    "CONSULT_TOOL",
    "SUPPORTED_CONSULT_HARNESSES",
    "ExternalAgentService",
    "get_service",
    "reset_service_for_tests",
    "validate_consult_input",
]

"""Per-harness implementations: claude_code, codex, gemini, aider."""
from __future__ import annotations

from kernos.kernel.external_agents.harnesses.aider import AiderHarness
from kernos.kernel.external_agents.harnesses.claude_code import (
    ClaudeCodeHarness,
)
from kernos.kernel.external_agents.harnesses.codex import CodexHarness
from kernos.kernel.external_agents.harnesses.gemini import GeminiHarness


__all__ = [
    "AiderHarness",
    "ClaudeCodeHarness",
    "CodexHarness",
    "GeminiHarness",
]

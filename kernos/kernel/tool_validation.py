"""Authoring-pattern validation for workshop tools.

Per the the design review-revised spec, register_tool inspects the tool's
implementation source for patterns that bypass the runtime-context
accessor. The tool's runtime context is invocation-scoped (Section 7
of the spec): the per-member data directory, credentials accessor,
member identifier, and space context all derive from the invoking
member at call time. Tools that hardcode any of those at registration
time would leak data across members.

This module catches the obvious red flags via regex pattern match
against the source. It is heuristic — true static analysis would
require AST inspection — but covers the cases that matter most:
hardcoded absolute paths, raw filesystem access bypassing the
runtime-context API, hardcoded member or instance identifiers, and
direct reads of secret environment variables.

Force-register flag (the design review edit 5): a tool author can pass force=True
to bypass these authoring-pattern checks. Force-registered tools are
logged as overrides and surface only to the author. They do NOT
bypass member isolation — runtime enforcement (the four checks in
C5) still applies. Force is for the legitimate edge cases (a tool
deliberately reading from a fixed shared file, for example) without
leaking unsafe authoring patterns to the wider member surface.

Rejection messages use the AppData analogy concretely so the author
sees the structural argument, not a cryptic regex match.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Validation result shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationFinding:
    """One authoring-pattern issue caught by the validator."""

    code: str            # short identifier, e.g. "hardcoded_absolute_path"
    line: int            # 1-indexed line number in the source
    snippet: str         # the offending text, truncated to ~120 chars
    explanation: str     # operator-readable message with AppData analogy


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of running the authoring-pattern check on a source file."""

    findings: tuple[ValidationFinding, ...]

    @property
    def is_clean(self) -> bool:
        return not self.findings

    def render(self) -> str:
        """Multiline message naming each finding for the author."""
        if self.is_clean:
            return "tool implementation passed authoring-pattern validation"
        lines = [
            f"tool implementation failed authoring-pattern validation "
            f"({len(self.findings)} issue(s)):",
            "",
        ]
        for f in self.findings:
            lines.append(f"  line {f.line} [{f.code}]: {f.snippet}")
            lines.append(f"    {f.explanation}")
            lines.append("")
        lines.append(
            "This is the equivalent of an app writing to System32 instead "
            "of AppData: the tool reaches outside its per-member sandbox. "
            "Use the runtime-context accessors (context.data_dir, "
            "context.credentials, context.member_id) instead. If a "
            "specific finding is intentional and unavoidable, register "
            "with force=True; force-registered tools surface only to "
            "the author and runtime enforcement still applies."
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------


# Patterns to scan for. Each is a (code, regex, explanation) triple.
# Patterns are intentionally conservative — they aim to catch obvious
# red flags without false-positive-ing legitimate uses. The regex is
# applied per-line.

_HARDCODED_ABSOLUTE_PATH_RE = re.compile(
    r"""(?xi)            # verbose, case-insensitive
    (
        ['"]/(?:home|root|var|etc|opt|usr|tmp|data)/[^'"\s]+['"]
        |
        ['"][A-Z]:\\\\[^'"\s]+['"]               # Windows drive letter
    )
    """
)

_BARE_OPEN_RE = re.compile(
    r"""(?x)
    \bopen\s*\(\s*['"]/[^'"\s]+['"]              # open("/something")
    """
)

_INSTANCE_ID_LITERAL_RE = re.compile(
    r"""(?xi)
    ['"](?:discord|sms|telegram|cli)\s*[:_]\s*[a-z0-9_+-]+['"]
    """
)

_MEMBER_ID_LITERAL_RE = re.compile(
    r"""(?xi)
    ['"](?:mem_|member_|member-)[a-z0-9_-]+['"]
    """
)

_SECRET_ENV_RE = re.compile(
    r"""(?xi)
    \bos\.environ
    (?:
        \.get\s*\(\s*['"]                # os.environ.get("...")
        |
        \s*\[\s*['"]                     # os.environ["..."]
    )
    (?:KERNOS_CREDENTIAL_KEY|ANTHROPIC_API_KEY|OPENAI_API_KEY|
        OPENAI_CODEX|GOOGLE_OAUTH|VOYAGE_API_KEY|BRAVE_API_KEY|
        OLLAMA_API_KEY|TWILIO_AUTH_TOKEN|DISCORD_BOT_TOKEN|
        TELEGRAM_BOT_TOKEN)['"]
    """
)


_PATTERN_TABLE = (
    (
        "hardcoded_absolute_path",
        _HARDCODED_ABSOLUTE_PATH_RE,
        "Absolute filesystem path is hardcoded. Per-member tool data "
        "lives under context.data_dir and is invocation-scoped; pinning "
        "to a fixed host path leaks across members.",
    ),
    (
        "bare_open_absolute",
        _BARE_OPEN_RE,
        "open() called against a hardcoded absolute path. Use "
        "context.data_dir / <relative> so the file lands in the "
        "invoking member's per-tool directory.",
    ),
    (
        "instance_id_literal",
        _INSTANCE_ID_LITERAL_RE,
        "Instance identifier is hardcoded (e.g. 'discord:12345'). The "
        "invoking instance is supplied via context; pinning a literal "
        "binds the tool to a single install and bypasses per-member "
        "scoping.",
    ),
    (
        "member_id_literal",
        _MEMBER_ID_LITERAL_RE,
        "Member identifier is hardcoded (e.g. 'mem_alice'). Use "
        "context.member_id; pinning to a literal makes the tool see "
        "another member's data on every invocation.",
    ),
    (
        "secret_env_read",
        _SECRET_ENV_RE,
        "Tool reads a Kernos-itself or service secret from the "
        "process environment directly. Tools must use "
        "context.credentials.get(service_id) to obtain "
        "member-scoped tokens; reading raw env vars bypasses the "
        "credentials primitive's scoping and audit hooks.",
    ),
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _split_lines_with_index(source: str) -> list[tuple[int, str]]:
    """Return [(line_no_1_indexed, line_content), ...] excluding pure comments."""
    out: list[tuple[int, str]] = []
    for i, raw in enumerate(source.splitlines(), start=1):
        # Skip lines that are entirely a Python comment (after leading
        # whitespace). We deliberately do NOT strip in-line comments
        # because a comment with a hardcoded path can still be a smell
        # but is much less load-bearing; tuning this if we need to.
        stripped = raw.lstrip()
        if stripped.startswith("#"):
            continue
        out.append((i, raw))
    return out


def validate_tool_source(source: str) -> ValidationResult:
    """Scan tool implementation source for unsafe authoring patterns.

    Returns a ValidationResult; callers branch on `is_clean` and
    decide whether to accept registration or require force.
    """
    findings: list[ValidationFinding] = []
    for line_no, line in _split_lines_with_index(source):
        for code, regex, explanation in _PATTERN_TABLE:
            match = regex.search(line)
            if not match:
                continue
            snippet = line.strip()
            if len(snippet) > 120:
                snippet = snippet[:117] + "..."
            findings.append(ValidationFinding(
                code=code,
                line=line_no,
                snippet=snippet,
                explanation=explanation,
            ))
            # First match per line wins; don't double-report the same
            # line under multiple patterns.
            break
    return ValidationResult(findings=tuple(findings))


def validate_tool_file(path: Path) -> ValidationResult:
    """Read a .py file and scan it for authoring-pattern issues."""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return ValidationResult(findings=(
            ValidationFinding(
                code="unreadable_source",
                line=0,
                snippet=str(path),
                explanation=f"Could not read source file: {exc}",
            ),
        ))
    return validate_tool_source(source)

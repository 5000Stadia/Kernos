"""Tests for the cohort error_summary sanitizer.

Covers Section 5b of the COHORT-FAN-OUT-RUNNER spec (the design review edit #7):
truncate, strip token-like patterns, strip Authorization/Bearer/API-
key headers, strip credential directory paths, no stack traces in
output. Conservative redaction — false positives over false
negatives.
"""

from __future__ import annotations

import traceback

import pytest

from kernos.kernel.cohorts.redaction import (
    DEFAULT_TRUNCATE_AT,
    sanitize,
    sanitize_exception,
)


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


def test_sanitize_truncates_to_default_cap():
    long_input = "a" * 1000
    out = sanitize(long_input)
    assert len(out) <= DEFAULT_TRUNCATE_AT


def test_sanitize_respects_explicit_cap():
    out = sanitize("a" * 100, truncate_at=20)
    assert len(out) <= 20


def test_sanitize_does_not_truncate_short_input():
    out = sanitize("brief error message")
    assert out == "brief error message"


# ---------------------------------------------------------------------------
# Token-shaped patterns
# ---------------------------------------------------------------------------


def test_sanitize_strips_sk_key():
    out = sanitize("API call failed with key sk-abcdef0123456789xyz")
    assert "sk-abcdef" not in out
    assert "[REDACTED]" in out


def test_sanitize_strips_slack_token():
    out = sanitize("connection failed: token=xoxb-12345-abcdef-foobar")
    assert "xoxb-" not in out
    assert "[REDACTED]" in out


def test_sanitize_strips_github_token():
    out = sanitize("auth error: ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    assert "ghp_" not in out
    assert "[REDACTED]" in out


def test_sanitize_strips_google_oauth_token():
    out = sanitize("expired ya29.a0Aaekm-superduperlongtokenhere")
    assert "ya29." not in out


def test_sanitize_strips_jwt():
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NSJ9.signature_part_xyz"
    out = sanitize(f"got bad jwt: {jwt}")
    assert "eyJhbGc" not in out


def test_sanitize_strips_generic_long_alphanumeric():
    out = sanitize("connection: 0123456789abcdef0123456789abcdef0123")
    assert "0123456789abcdef0123" not in out


def test_sanitize_preserves_short_alphanumeric_tokens():
    """Short identifiers (versions, ids) shouldn't be stripped."""
    out = sanitize("HTTP 500 from build abc123")
    assert "abc123" in out
    assert "HTTP" in out


# ---------------------------------------------------------------------------
# Header-shaped patterns
# ---------------------------------------------------------------------------


def test_sanitize_strips_authorization_header():
    out = sanitize("Authorization: Bearer secret_value_here")
    assert "secret_value_here" not in out
    assert "Authorization:" not in out


def test_sanitize_strips_bearer_inline():
    out = sanitize("sent Bearer mytokenhere with request")
    assert "mytokenhere" not in out


def test_sanitize_strips_api_key_header():
    out = sanitize("X-API-Key: 12345-abcdef-67890")
    assert "12345-abcdef-67890" not in out
    out2 = sanitize("api_key=plaintextvaluehere")
    assert "plaintextvaluehere" not in out2


# ---------------------------------------------------------------------------
# Credential directory paths
# ---------------------------------------------------------------------------


def test_sanitize_strips_kernos_credential_path():
    out = sanitize(
        "could not read /home/user/.config/kernos/credentials/notion.json"
    )
    assert ".config/kernos/credentials" not in out


def test_sanitize_strips_aws_credential_path():
    out = sanitize("denied access to /Users/alice/.aws/credentials")
    assert ".aws/credentials" not in out


def test_sanitize_strips_ssh_path():
    out = sanitize("loaded /home/user/.ssh/id_ed25519")
    assert ".ssh/" not in out


# ---------------------------------------------------------------------------
# Stack traces
# ---------------------------------------------------------------------------


def test_sanitize_strips_python_traceback_tail():
    formatted = (
        "Some prefix message\n"
        "Traceback (most recent call last):\n"
        '  File "/home/user/Kernos/kernos/x.py", line 1, in <module>\n'
        "    raise RuntimeError('boom')\n"
        "RuntimeError: boom"
    )
    out = sanitize(formatted)
    assert "Traceback" not in out
    assert "File \"" not in out


def test_sanitize_strips_inline_frame_lines():
    formatted = (
        'TimeoutError: deadline\n  File "/tmp/inner.py", line 42, in run\n'
        "    await something()"
    )
    out = sanitize(formatted)
    assert "File \"" not in out
    assert "TimeoutError" in out


# ---------------------------------------------------------------------------
# sanitize_exception
# ---------------------------------------------------------------------------


def test_sanitize_exception_includes_class_and_message():
    try:
        raise TimeoutError("cohort exceeded 500ms")
    except TimeoutError as exc:
        out = sanitize_exception(exc)
    assert "TimeoutError" in out
    assert "exceeded 500ms" in out


def test_sanitize_exception_redacts_token_in_message():
    try:
        raise RuntimeError("auth failed for sk-abcdefghij1234567890")
    except RuntimeError as exc:
        out = sanitize_exception(exc)
    assert "sk-abcdef" not in out
    assert "RuntimeError" in out


def test_sanitize_exception_does_not_include_traceback_frames():
    """Even if format_exc() were leaked, sanitize would catch it.
    Verify the helper itself never invokes format_exc."""
    try:
        raise ValueError("boom")
    except ValueError as exc:
        out = sanitize_exception(exc)
        # Compare against what format_exc would have emitted.
        formatted = traceback.format_exc()
    assert "  File \"" not in out
    assert "Traceback" not in out
    # Sanity: format_exc would have returned frames.
    assert "Traceback" in formatted


# ---------------------------------------------------------------------------
# Conservative defaults
# ---------------------------------------------------------------------------


def test_sanitize_coerces_non_string_input():
    out = sanitize(12345)  # type: ignore[arg-type]
    assert isinstance(out, str)


def test_sanitize_handles_empty_string():
    assert sanitize("") == ""


def test_sanitize_strips_multiple_patterns_in_one_input():
    """Combined: token + header + path + traceback all in one
    string. After sanitization, none of the sensitive substrings
    survive."""
    dirty = (
        "Authorization: Bearer xoxb-1234-abcdef-foobar at "
        "/home/u/.config/kernos/credentials/x.json\n"
        "Traceback (most recent call last):\n  File \"x.py\", line 1"
    )
    out = sanitize(dirty)
    assert "xoxb-" not in out
    assert "Authorization:" not in out
    assert "credentials/x.json" not in out
    assert "Traceback" not in out

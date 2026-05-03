"""Envelope validation for ``partial_spec_json``.

WDP C2. Substrate-level validation only — Compiler / CRB owns
semantic validation ("would this descriptor compile?"). Four
checks:

  1. Must be a JSON object (dict, not array / scalar / None).
  2. Size cap: 64 KB serialised.
  3. No executable-blob patterns in keys or values.
  4. No secret-keyword patterns in keys (with non-echo on the
     value side per AC #9).

Pattern lists below are the **canonical chosen list** for v1
(the design review edit v1 → v2 — illustrative-not-exhaustive). The spec
declared the lists as elegance-latitude starting points; we
document the canonical set here. New patterns can be added
through normal code review without amending the spec.

**Secret-non-echo pin (AC #9):** when validation fails on a
secret-pattern match, the raised ``DraftEnvelopeInvalid``
identifies the offending KEY only — never the value, never a
substring of the value. Defense against accidentally dumping
credentials into logs.
"""
from __future__ import annotations

import json
import re
from typing import Any

from kernos.kernel.drafts.errors import DraftEnvelopeInvalid


# Maximum size of the serialised JSON. 64 KB per spec.
MAX_PAYLOAD_BYTES = 64 * 1024


# Executable-blob patterns. Match against keys (literal) and
# string values (regex). Bytes-typed values are rejected
# unconditionally.
EXECUTABLE_KEY_NAMES = frozenset({
    "eval", "exec", "__import__", "__class__", "__globals__",
    "__builtins__", "compile", "subprocess",
})

EXECUTABLE_VALUE_PATTERNS = (
    re.compile(r"\b__import__\s*\("),
    re.compile(r"\beval\s*\("),
    re.compile(r"\bexec\s*\("),
    re.compile(r"\bcompile\s*\("),
    re.compile(r"\bsubprocess\.[A-Za-z]+\("),
)


# Secret-keyword patterns matched against KEY names. Codex
# consolidated review caught that plain substring matching is too
# aggressive — ``user_secret_question`` (a benign description
# field) would fire on ``secret``. Python's ``\b`` doesn't help
# here because ``_`` counts as a word char, so the word-boundary
# regex still matches inside underscore-separated tokens.
#
# The fix: match by structural position. Three categories:
#
#   WHOLE_KEY_TOKENS — the entire (lowercased) key equals the
#       token. Catches ``secret``, ``password``, ``api_key``.
#   SUFFIX_TOKENS — the key ends with ``_<token>``. Catches
#       ``my_password``, ``user_api_key``, ``deploy_secret``.
#   PREFIX_TOKENS — the key starts with ``<token>_``. Catches
#       ``password_hash``, ``api_key_id``, ``secret_value``.
#
# Benign passes: ``user_secret_question`` (secret is in the
# middle), ``passcode_label`` (different word), ``api_keyboard``
# (no underscore separator after api_key).

WHOLE_KEY_TOKENS = frozenset({
    "secret", "password", "passwd",
    "api_key", "apikey",
    "private_key", "privatekey",
    "auth_token", "access_token", "bearer_token",
    "credential", "credentials",
})

SUFFIX_TOKENS = (
    "_secret", "_password", "_passwd",
    "_api_key", "_apikey",
    "_private_key", "_privatekey",
    "_auth_token", "_access_token", "_bearer_token",
    "_credential", "_credentials",
)

PREFIX_TOKENS = (
    "secret_", "password_", "passwd_",
    "api_key_", "apikey_",
    "private_key_", "privatekey_",
    "auth_token_", "access_token_", "bearer_token_",
    "credential_", "credentials_",
)


# Backward-compat reference: callers that imported the old
# tuple still get the canonical token list. Internal logic now
# uses the three categories above.
SECRET_KEY_SUBSTRINGS = tuple(WHOLE_KEY_TOKENS)


def _key_matches_secret(key: str) -> bool:
    """Return True if ``key`` looks like a credential-carrying
    field name. False for benign descriptors that merely contain
    secret words as substrings."""
    lowered = key.lower()
    if lowered in WHOLE_KEY_TOKENS:
        return True
    if any(lowered.endswith(s) for s in SUFFIX_TOKENS):
        return True
    if any(lowered.startswith(s) for s in PREFIX_TOKENS):
        return True
    return False

SECRET_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"-----BEGIN OPENSSH PRIVATE KEY-----"),
    re.compile(r"\bBearer\s+ey[A-Za-z0-9_\-]+\."),  # JWT-shaped
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]+"),  # Slack-shaped
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),  # GitHub PAT
)


def validate_envelope(payload: Any) -> None:
    """Run the four envelope checks on a candidate payload.

    Raises ``DraftEnvelopeInvalid`` on any failure. Per AC #9 the
    error message identifies only the offending KEY — never the
    value when a secret-pattern match fires.
    """
    # Check 1: must be a dict.
    if not isinstance(payload, dict):
        raise DraftEnvelopeInvalid(
            f"partial_spec_json must be a JSON object (dict); "
            f"got {type(payload).__name__}"
        )
    # Check 2: size cap. We re-serialise to measure the canonical
    # form rather than trusting the caller's whitespace.
    try:
        serialised = json.dumps(payload)
    except (TypeError, ValueError) as exc:
        raise DraftEnvelopeInvalid(
            f"partial_spec_json contains values that cannot be "
            f"JSON-serialised: {exc}"
        ) from exc
    if len(serialised.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise DraftEnvelopeInvalid(
            f"partial_spec_json exceeds {MAX_PAYLOAD_BYTES}-byte limit "
            f"(got {len(serialised.encode('utf-8'))} bytes)"
        )
    # Check 3 + 4 walk the payload tree once.
    _walk_and_check(payload, path="$")


def _walk_and_check(value: Any, *, path: str) -> None:
    """Recursive descent over the JSON payload tree. Raises on
    executable-blob or secret-keyword matches."""
    if isinstance(value, bytes):
        raise DraftEnvelopeInvalid(
            f"partial_spec_json contains a bytes-typed value at "
            f"{path} — bytes payloads are rejected"
        )
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise DraftEnvelopeInvalid(
                    f"partial_spec_json contains non-string key "
                    f"{type(k).__name__} at {path}"
                )
            _check_key(k, path=path)
            _walk_and_check(v, path=f"{path}.{k}")
        return
    if isinstance(value, list):
        for idx, item in enumerate(value):
            _walk_and_check(item, path=f"{path}[{idx}]")
        return
    if isinstance(value, str):
        _check_value_string(value, path=path)
        return
    # Numbers / bools / None pass through.


def _check_key(key: str, *, path: str) -> None:
    """Reject keys that match executable-blob or secret-keyword
    patterns. For secret keys we want the key NAMED in the error
    so operators can find and rename it; that's distinct from
    secret VALUES (which we never echo).

    Secret-key matching uses word-boundary regexes (Codex
    consolidated review iteration) so ``user_secret_question``
    doesn't fire on ``secret`` substring. ``api_key``,
    ``apiKey``, ``API_KEY``, ``my_password`` still match because
    the token sits at a real boundary.
    """
    lowered = key.lower()
    if lowered in EXECUTABLE_KEY_NAMES:
        raise DraftEnvelopeInvalid(
            f"partial_spec_json contains executable-blob key "
            f"{key!r} at {path}"
        )
    if _key_matches_secret(key):
        raise DraftEnvelopeInvalid(
            f"partial_spec_json contains secret-shaped key "
            f"{key!r} at {path} — drafts must not carry "
            f"credential material"
        )


def _check_value_string(value: str, *, path: str) -> None:
    """Reject string values matching executable-blob or secret
    patterns. AC #9 pin: secret-pattern matches identify the
    offending KEY (the path) only — the value is NEVER echoed,
    not in the error message and not in any logged trace."""
    for pattern in EXECUTABLE_VALUE_PATTERNS:
        if pattern.search(value):
            raise DraftEnvelopeInvalid(
                f"partial_spec_json contains executable-blob "
                f"pattern at {path}"
            )
    for pattern in SECRET_VALUE_PATTERNS:
        if pattern.search(value):
            # AC #9 secret-non-echo: the value never appears in
            # the message. The path identifies the offending key
            # for diagnosis without leaking credentials.
            raise DraftEnvelopeInvalid(
                f"partial_spec_json contains a secret-shaped "
                f"value at {path} — drafts must not carry "
                f"credential material"
            )


__all__ = [
    "EXECUTABLE_KEY_NAMES",
    "EXECUTABLE_VALUE_PATTERNS",
    "MAX_PAYLOAD_BYTES",
    "PREFIX_TOKENS",
    "SECRET_KEY_SUBSTRINGS",
    "SECRET_VALUE_PATTERNS",
    "SUFFIX_TOKENS",
    "WHOLE_KEY_TOKENS",
    "validate_envelope",
]

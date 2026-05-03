"""Deterministic sanitizer for synthetic CohortOutput error_summary.

Per the COHORT-FAN-OUT-RUNNER spec, Section 5b (the design review edit #7):

  - Truncate to a configurable cap (default 500 chars)
  - Strip token-like patterns (long alphanumeric, sk-*, xoxb-*, JWT,
    bearer tokens)
  - Strip Authorization / Bearer / API-key headers in stringified
    exception content
  - Strip file paths under known credential directories
  - No stack traces in any CohortOutput field

The sanitizer is conservative — false positives (over-redacting
innocent text) are preferable to false negatives (leaking
credentials). Operator-only audit logs may carry full stack traces
elsewhere; this function never produces them in CohortOutput
surface text.
"""

from __future__ import annotations

import re


# Configurable defaults; spec gives 500 as the default char cap.
DEFAULT_TRUNCATE_AT = 500

# Conservative token shapes. Order matters: more-specific patterns
# first so generic alphanumeric runs don't gobble shapes we want to
# tag distinctly. Each pattern is replaced with `[REDACTED]` in
# place; we don't try to preserve "this had a token" attribution
# beyond the redaction marker.
_TOKEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Stripe-style "sk-…" or "pk-…" keys.
    ("sk_pk_key", re.compile(r"\b(?:sk|pk)-[a-zA-Z0-9_-]{8,}\b")),
    # Slack tokens.
    (
        "slack_token",
        re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
    ),
    # GitHub-style tokens.
    (
        "github_token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    ),
    # Google OAuth access tokens.
    (
        "google_oauth_token",
        re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}\b"),
    ),
    # JWT shape (three base64-url segments separated by dots).
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]+=*\.[A-Za-z0-9_-]+=*\.[A-Za-z0-9_-]+=*\b"
        ),
    ),
    # Generic long alphanumeric runs that look like tokens (32+ chars
    # of [A-Za-z0-9_-]). Conservative — long strings of this shape in
    # exception text are far more likely to be tokens than not.
    (
        "generic_long_token",
        re.compile(r"\b[A-Za-z0-9_-]{32,}\b"),
    ),
]

# Header-shaped patterns. These look for common authorization-header
# spellings. We strip the entire header line (key + value) so the
# redacted output doesn't even hint at which auth scheme leaked.
_HEADER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "authorization_header",
        re.compile(
            r"(?i)\bAuthorization:\s*\S.*",
        ),
    ),
    (
        "bearer_inline",
        re.compile(r"(?i)\bBearer\s+[^\s\"',]+"),
    ),
    (
        "api_key_header",
        re.compile(
            r"(?i)\b(?:X-)?(?:API|APP)[_-]?KEY\s*[:=]\s*\S+",
        ),
    ),
]

# Credential directory paths. Conservative substring approach — if
# any of these appears, we strip the matching path-shaped fragment.
# The fragment ends at whitespace, quote, or end-of-string.
_CRED_DIR_FRAGMENTS = (
    ".config/kernos/credentials",
    ".kernos/keys",
    ".kernos/credentials",
    "/credentials.json",
    "/.aws/credentials",
    "/.netrc",
    "/.ssh/",
)
_CRED_PATH_PATTERN = re.compile(
    r"(?:[^\s\"',]+(?:"
    + "|".join(re.escape(f) for f in _CRED_DIR_FRAGMENTS)
    + r")[^\s\"',]*)",
    re.IGNORECASE,
)

# Stack-trace markers Python raises typically include. Stripping the
# tail of the message at any of these markers is the simplest way to
# guarantee no Python traceback content lands in CohortOutput.
_TRACEBACK_MARKERS = (
    "Traceback (most recent call last):",
    "  File \"",  # any frame line
    "\n  at ",  # JS-style frames in case a cohort raises wrapped JS errors
)


# Marker text we substitute. Keeping it short keeps truncation budget
# available for the actual message portion.
_REDACTED = "[REDACTED]"


def sanitize(text: str, *, truncate_at: int = DEFAULT_TRUNCATE_AT) -> str:
    """Sanitize a string for inclusion in CohortOutput.error_summary.

    Order of operations:
      1. Strip stack-trace tails first (so traceback content never
         survives later passes).
      2. Strip header-shaped patterns (Authorization, Bearer, API-key).
      3. Strip credential directory paths.
      4. Strip token-shaped patterns (specific shapes first, generic
         long runs last).
      5. Truncate.

    The result is always a string of length ≤ truncate_at. The
    function never raises; if input isn't a string it's coerced.
    """
    if not isinstance(text, str):
        text = str(text)

    # 1. Truncate stack traces. We cut at the earliest marker so the
    # string ends before any frame content.
    cut = len(text)
    for marker in _TRACEBACK_MARKERS:
        idx = text.find(marker)
        if idx >= 0 and idx < cut:
            cut = idx
    text = text[:cut].rstrip()

    # 2. Header-shaped patterns. We match header lines first because
    # generic token redaction would otherwise eat just the value and
    # leave the header key behind, which is a partial leak.
    for _name, pat in _HEADER_PATTERNS:
        text = pat.sub(_REDACTED, text)

    # 3. Credential directory paths.
    text = _CRED_PATH_PATTERN.sub(_REDACTED, text)

    # 4. Token-shaped patterns.
    for _name, pat in _TOKEN_PATTERNS:
        text = pat.sub(_REDACTED, text)

    # 5. Truncate to the configured cap.
    if truncate_at >= 0 and len(text) > truncate_at:
        text = text[: max(0, truncate_at - 1)] + "…"

    return text


def sanitize_exception(exc: BaseException, *, truncate_at: int = DEFAULT_TRUNCATE_AT) -> str:
    """Sanitize an exception's message for CohortOutput.error_summary.

    We deliberately stringify only the exception's args / message —
    NOT `traceback.format_exc()` — so stack frames cannot leak even
    if the redaction patterns regress. If the exception's class name
    is informative (e.g., `TimeoutError`, `KeyError`), it's prefixed.
    """
    cls = type(exc).__name__
    msg = str(exc) if str(exc) else cls
    combined = f"{cls}: {msg}" if msg != cls else cls
    return sanitize(combined, truncate_at=truncate_at)


__all__ = [
    "DEFAULT_TRUNCATE_AT",
    "sanitize",
    "sanitize_exception",
]

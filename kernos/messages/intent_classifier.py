"""Local heuristic intent classifier for the assemble phase's tool surfacer.

Regex / keyword matching; no LLM call. Substrate-internal — used by
the surfacer to bias the catalog scan's ranking toward tools whose
declared effect class matches the user's apparent intent.

POSTURE-SURFACING-CALIBRATION-V1 (2026-05-22).
"""
from __future__ import annotations

import re

# Intent → keyword patterns. A single message can match multiple
# intents; the surfacer treats the resulting set as a relevance
# hint, not a hard filter. Empty set means no signal → surfacer
# falls back to its prior behavior.
_INTENT_PATTERNS: dict[str, re.Pattern[str]] = {
    "write": re.compile(
        r"\b(write|create|add|save|post|edit|update|make|put|set)\b",
        re.IGNORECASE,
    ),
    "delete": re.compile(
        r"\b(delete|remove|drop|archive|cancel|clear)\b",
        re.IGNORECASE,
    ),
    "send": re.compile(
        r"\b(send|email|message|ping|notify|text|dm)\b",
        re.IGNORECASE,
    ),
    "spend": re.compile(
        r"\b(buy|pay|order|purchase|spend|charge)\b",
        re.IGNORECASE,
    ),
    "schedule": re.compile(
        r"\b(schedule|remind|tomorrow|later|appointment|book|"
        r"next\s+(?:week|monday|tuesday|wednesday|thursday|friday|"
        r"saturday|sunday))\b",
        re.IGNORECASE,
    ),
    "read": re.compile(
        r"\b(read|show|list|what|view|find|search|look\s+up)\b",
        re.IGNORECASE,
    ),
}

VALID_INTENTS: frozenset[str] = frozenset(_INTENT_PATTERNS.keys())


def classify_intent(user_message: str) -> set[str]:
    """Classify a user message into 0+ intent labels.

    Args:
        user_message: The raw user message text.

    Returns:
        Set of intent strings drawn from VALID_INTENTS.
        Empty set means no signal — surfacer should fall
        back to its prior (intent-unaware) behavior.
    """
    text = (user_message or "").strip()
    if not text:
        return set()
    return {label for label, pat in _INTENT_PATTERNS.items() if pat.search(text)}

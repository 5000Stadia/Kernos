# POSTURE-SURFACING-CALIBRATION-V1

**Date:** 2026-05-22
**Status:** Draft for review (sub-spec of `KERNOS-DEFAULT-POSTURE-V1`)
**Scope:** Three changes to the assemble-phase tool surfacer
  (`kernos/messages/phases/assemble.py:484+`):

  1. **Local intent classifier** — substrate-internal regex
     matcher; classifies the user message into a small effect set.
  2. **Deterministic ranking boost** — pass intent set to the
     catalog-scan prompt so tools matching the intent get
     boosted in the LLM rank.
  3. **`canvas_create` + `page_write` co-surfacing pair** —
     when one is in the pinned/active zone and the conversation
     has recent canvas context, the other is auto-promoted.
  4. **`tool.withheld_from_surface` event** — emitted on
     eviction or filter, giving operators an attributable
     friction signal.

**Estimated size:** ~100 LOC source + ~120 LOC tests.

## Why this spec exists

Per `KERNOS-DEFAULT-POSTURE-V1` (commit `f2a0d59`, locked
GREEN) D4 + D6: tool surfacing currently lacks an intent
signal and emits no withhold receipts. The canvas-test gap
("I can read the canvas but can't write to it") surfaced
when the model had `canvas_read` in its active zone but
`page_write` was evicted for budget reasons — silently,
with no diagnostic trail.

The two-pronged fix: bias the ranker toward tools matching the
user's stated intent (D4) AND emit structured events when
tools get withheld (D6) so the friction is detectable post-hoc.

## Deferred to follow-up sub-specs

Per `[[v1-operational-verification-scope-discipline]]`:

- **D4 part 2: per-member surfacing hook** — DEFERRED.
  - (1) load-bearing? NO — current behavior is "no
    per-member differentiation"; no concrete policy demands
    it yet.
  - (2) v1-manually-inspectable? NO — needs concrete
    policies to test.
  - (3) operational-evidence informs design? YES.
  - → Future sub-spec `POSTURE-PER-MEMBER-SURFACING-V1`
    when per-member policy demand emerges.

- **D5: `inspect_tool_availability` + per-turn snapshot** —
  DEFERRED to its own sub-spec `POSTURE-PREFLIGHT-V1`.
  - Justification: D5 introduces a new kernel tool + a
    per-turn snapshot data structure with non-trivial
    invalidation semantics (re-built per assemble, queried
    by the agent mid-turn). That deserves its own spec +
    Codex review round rather than being bolted onto this
    spec's smaller-blast-radius changes.

This sub-spec's scope is the changes whose v1 value is
load-bearing AND whose design is locked enough to ship
without further deliberation.

## Current state

The surfacer in `assemble.py:484-701` runs three tiers:

1. **Tier 1 pinned** (`ALWAYS_PINNED` set in
   `kernos/kernel/tool_catalog.py:51`) — unconditional.
2. **Tier 1 system-space additions** — kernel tools when
   in System space.
3. **Tier 2 catalog scan** (`assemble.py:633+`) — LLM call
   ranking the catalog against the user message.
4. **Active zone fill** — token-budget-constrained pull
   from candidates.

The catalog scan's system prompt is generic: "Given the
user's message, select which additional tools from the
catalog are needed." There is no intent classification +
no signal to the LLM about what KIND of action the user
wants.

Eviction happens at the active-zone fill loop
(`assemble.py:679-685`); evictions are logged at INFO
(`TOOL_EVICT: evicted=...`) but emit no event.

## Design

### 1. Local intent classifier

New module: `kernos/messages/intent_classifier.py`.

```python
"""Local heuristic intent classifier for the assemble
phase's tool surfacer. Regex / keyword matching; no LLM
call. ~50 LOC.

POSTURE-SURFACING-CALIBRATION-V1 (2026-05-22).
"""
from __future__ import annotations
import re

# Intent → keyword patterns. Order matters — first hit wins
# per-intent, but a message can carry multiple intents.
_INTENT_PATTERNS: dict[str, re.Pattern[str]] = {
    "write":    re.compile(r"\b(write|create|add|save|post|edit|update|make|put|set)\b", re.I),
    "delete":   re.compile(r"\b(delete|remove|drop|archive|cancel|clear)\b", re.I),
    "send":     re.compile(r"\b(send|email|message|ping|notify|text|dm)\b", re.I),
    "spend":    re.compile(r"\b(buy|pay|order|purchase|spend|charge)\b", re.I),
    "schedule": re.compile(r"\b(schedule|remind|tomorrow|later|next week|next monday|next tuesday|next wednesday|next thursday|next friday|appointment|book)\b", re.I),
    "read":     re.compile(r"\b(read|show|list|what|view|find|search|look up)\b", re.I),
}

def classify_intent(user_message: str) -> set[str]:
    """Classify the user's message into 0+ intent labels.

    Returns:
        Set of intent strings drawn from {"read", "write",
        "delete", "send", "spend", "schedule"}. Empty set
        means no signal — surfacer falls back to current
        behavior.
    """
    text = (user_message or "").strip()
    if not text:
        return set()
    hits = {label for label, pat in _INTENT_PATTERNS.items() if pat.search(text)}
    return hits
```

### 2. Ranking boost

In `assemble.py:633-668`, before the catalog-scan LLM call,
classify intent and pass it to the system prompt:

```python
from kernos.messages.intent_classifier import classify_intent

intents = classify_intent(_msg_text)
intent_hint = ""
if intents:
    intent_hint = (
        f"\n\nThe user's intent appears to be: "
        f"{', '.join(sorted(intents))}. Prefer tools whose "
        f"declared effect class matches one of these intents."
    )

scan_result = await handler.reasoning.complete_simple(
    system_prompt=(
        "Given the user's message, select which additional tools from the catalog "
        "are needed. Only select tools directly relevant. Return empty array if "
        "the loaded tools are sufficient.\n\n"
        f"Already loaded: {sorted(_added)}"
        + intent_hint
    ),
    ...
)
```

No re-ranking, no new LLM call — just an extra system-prompt
sentence informing the existing scan. Cost-neutral.

### 3. Co-surfacing pair: canvas_create + page_write

Module-level constant in `tool_catalog.py`:

```python
# POSTURE-SURFACING-CALIBRATION-V1 (2026-05-22): when one of
# the tools in a pair is in the active zone AND the
# conversation has recent canvas context, auto-promote the
# other. Closes the "I can read but can't write" surface gap
# without per-tool eviction handling.
_CO_SURFACING_PAIRS: list[tuple[str, str]] = [
    ("canvas_create", "page_write"),
    ("canvas_read",   "page_write"),
]
```

In `assemble.py`, after the catalog-scan loop fills
`candidates` but before the active-zone fill:

```python
from kernos.kernel.tool_catalog import _CO_SURFACING_PAIRS

# POSTURE-SURFACING-CALIBRATION-V1: co-surface canvas+page
_active_names = {schema.get("name") for schema, _ in candidates}
for a, b in _CO_SURFACING_PAIRS:
    if a in _active_names and b not in _added:
        b_schema = _kernel_tool_map.get(b) or handler.registry.get_tool_schema(b)
        if b_schema and _add_tool(b_schema):
            candidates.append((b_schema, 0))
            logger.info("TOOL_CO_SURFACING: paired %s → %s", a, b)
    if b in _active_names and a not in _added:
        a_schema = _kernel_tool_map.get(a) or handler.registry.get_tool_schema(a)
        if a_schema and _add_tool(a_schema):
            candidates.append((a_schema, 0))
            logger.info("TOOL_CO_SURFACING: paired %s → %s", b, a)
```

### 4. `tool.withheld_from_surface` event

New event type in `kernos/kernel/event_types.py`:

```python
TOOL_WITHHELD_FROM_SURFACE = "tool.withheld_from_surface"
```

Emit at the eviction site (`assemble.py:679-685`):

```python
for schema, priority in candidates:
    tokens = _schema_tokens(schema)
    if _active_tokens + tokens <= active_budget:
        active_tools.append(schema)
        _active_tokens += tokens
    else:
        _evicted.append(schema.get("name", "?"))
        try:
            await emit_event(
                handler.events,
                EventType.TOOL_WITHHELD_FROM_SURFACE,
                instance_id, active_space_id,
                payload={
                    "tool_name": schema.get("name", ""),
                    "reason": "evicted_for_budget",
                    "tier_attempted": "active",
                    "turn_id": ctx.turn_id if hasattr(ctx, "turn_id") else "",
                },
            )
        except Exception:
            pass  # event emission is best-effort per kernel convention
```

Also emit when the disabled-service filter drops a tool
(`assemble.py:534+`) with `reason="disabled_service"`.

## Acceptance criteria

| AC | Description |
|---|---|
| AC1 | `classify_intent("write a page about X")` returns set containing `"write"`. |
| AC2 | `classify_intent("delete that file")` returns set containing `"delete"`. |
| AC3 | `classify_intent("schedule a reminder for tomorrow")` returns set containing `"schedule"`. |
| AC4 | `classify_intent("send an email to mom")` returns set containing `"send"`. |
| AC5 | `classify_intent("")` returns empty set. |
| AC6 | `classify_intent("hello there")` (no intent keywords) returns empty set. |
| AC7 | A user message with both write + delete keywords returns both labels. |
| AC8 | Case insensitivity — `WRITE` matches the same pattern as `write`. |
| AC9 | The catalog-scan system prompt includes the intent_hint sentence when intents are detected; omits it when no intents detected. |
| AC10 | Co-surfacing: when `canvas_create` is in the candidates list, `page_write` is auto-added before active-zone fill. |
| AC11 | Co-surfacing: when `canvas_read` is in candidates, `page_write` is auto-added. |
| AC12 | Co-surfacing is no-op when the pair-mate is missing from the registry (defensive). |
| AC13 | `TOOL_WITHHELD_FROM_SURFACE` event fires when a tool is evicted for budget pressure (payload contains tool_name + reason + tier_attempted). |
| AC14 | `TOOL_WITHHELD_FROM_SURFACE` event fires when the disabled-service filter drops a tool (reason="disabled_service"). |
| AC15 | Event emission is best-effort — a stub event stream that raises on emit doesn't break the assemble phase. |
| AC16 | No regressions on existing surfacing tests. |

## Soak gate

1. **Automated**: ACs pin classifier + co-surfacing + event
   shape.
2. **Operator soak**: in System space, send "Create a canvas
   called 'Plans' and write 'hello' to a page in it." Verify:
   - Intent classifier picks `{"write", "create"}` (write
     pattern matches both).
   - Catalog scan includes the intent hint in its prompt.
   - Both `canvas_create` and `page_write` appear in the
     active tool list.
   - No `TOOL_WITHHELD_FROM_SURFACE` event fires (budget
     fits).
3. **Eviction soak**: artificially low `KERNOS_TOOL_TOKEN_BUDGET`
   (e.g. `500`) → verify `TOOL_WITHHELD_FROM_SURFACE` events
   appear in the event stream for evicted tools.

## Out of scope

- `inspect_tool_availability` kernel tool + per-turn
  snapshot → `POSTURE-PREFLIGHT-V1` (follow-up).
- Per-member surfacing hook + per-member policy →
  `POSTURE-PER-MEMBER-SURFACING-V1` (follow-up when demand
  emerges).
- `request_tool` extension to support kernel/stock/workspace
  promotion → reserved for a future spec.
- `/posture` slash command → `POSTURE-CONFIGURATION-V1`.

## Risks

- **Risk:** Intent classifier is a regex matcher — will
  miss intents expressed in non-keyword ways ("can you make
  this happen?", "I want X").
  - **Mitigation:** Empty intent set → no-op (preserves
    current behavior). Classifier only ADDS signal, never
    blocks. False negatives degrade gracefully.

- **Risk:** Co-surfacing pair could promote `page_write`
  when there's no canvas context (just because `canvas_read`
  happened to rank).
  - **Mitigation:** v1 ships unconditional pair-promote.
    The "recent canvas context" check from POSTURE-V1's
    spec is reserved for a future refinement; ship the
    simpler logic now, observe whether spurious promotions
    happen, refine if needed.

- **Risk:** `TOOL_WITHHELD_FROM_SURFACE` event volume
  could be high under aggressive eviction.
  - **Mitigation:** It's per-eviction at active-zone-fill
    time, which runs once per turn. Maximum cardinality =
    catalog_size per turn. Acceptable.

## Dependencies

- `POSTURE-SEEDED-COVENANTS-V1` (commit `d27d11c`) — landed.
- `POSTURE-GATE-CLASSIFICATION-V1` (commit `e73cb8f`) — landed.
- `POSTURE-EVALUATION-MODES-V1` (commit `4e3458b`) — landed.
- Blocks: nothing in the POSTURE-V1 arc directly. The
  `POSTURE-PREFLIGHT-V1` follow-up depends on this spec's
  surfacing-snapshot infrastructure being in place.

## Migration

No state migration. Intent classifier is stateless. Co-
surfacing dict is module-level constant. Event type is
additive (won't break existing consumers).

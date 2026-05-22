# Covenants

Covenants are behavioral rules that guide agent actions. They define what the agent must do, must not do, prefers, and when to escalate. Every tenant starts with a profile-selected set of default rules (5, 7, or 9 depending on `KERNOS_POSTURE_PROFILE`). Users add rules through natural conversation.

## How Covenants Are Created

**Covenants are automatically captured by the kernel.** When the user gives a behavioral instruction mid-conversation — "never email my ex", "always confirm before spending money", "don't schedule meetings before 9am" — the Tier 2 extractor detects it and creates a `CovenantRule` record. The agent does NOT need to create rules manually.

This is the sole creation path. The `manage_covenants` tool is for viewing, editing, and removing existing rules — not creating new ones.

## Rule Structure

Each `CovenantRule` (`kernos/kernel/state.py`) has:

- **rule_type** — `must` (required behavior), `must_not` (prohibited), `preference` (soft guidance), `escalation` (when to ask)
- **description** — natural language description of the rule
- **capability** — which tool/capability this rule applies to (or `"general"`)
- **enforcement_tier** — `silent`, `notify`, `confirm`, or `block`
- **context_space** — `None` for global rules, or scoped to a specific space
- **source** — `default` (system), `user_stated`, or `evolved`
- **layer** — `principle` (hard boundary) or `practice` (flexible guidance)

## Default Rules — Posture Profile

Per `POSTURE-SEEDED-COVENANTS-V1` (2026-05-22), the default
covenant seed is profile-selectable via the
`KERNOS_POSTURE_PROFILE` env var. Three profiles:

- `minimal` (DEFAULT) — 5 rules: spirit + privacy-belongs-to-sharer + don't-delete-without-asking + escalation + self-update-notice. The behavior-neutral posture.
- `standard` — 7 rules: minimal + confirm-spending + show-drafts-to-3rd-parties.
- `strict` — 9 rules: standard + match-depth-preference + never-send-3rd-party-contacts-unless-owner-initiated. Reproduces the pre-POSTURE-V1 default exactly.

Existing instances are NOT auto-rebased when the env changes;
the seed runs only on first-boot of a NEW instance. Reset
existing instances via the `/posture reset-covenants <profile>`
slash command (lands in `POSTURE-CONFIGURATION-V1`).

The canonical rule descriptions live in `kernos/kernel/state.py`
as module-level constants (`_DESC_*`). All profile lists
reference these by name so the wording lives in one place.

## Post-Write Validation

After every rule creation, `validate_covenant_set()` fires a single Haiku call checking the full set for:

- **SUPERSEDE** — a newer rule replaces an older one on the same topic (the user changed their mind). The older rule is retired automatically. This is the most common resolution for apparent conflicts.
- **MERGE** — auto-resolves duplicate rules (supersedes the older one)
- **CONFLICT** — genuinely ambiguous contradictions surface as a whisper once. If unresolved after 3 validation runs, the older rule is auto-superseded.
- **REWRITE** — auto-improves poorly worded rules

## manage_covenants Tool

| Field | Value |
|-------|-------|
| Effect | soft_write (except "list" action which is read) |
| Actions | `list` — show active rules grouped by type |
| | `remove` — soft-remove a rule (sets active=false) |
| | `update` — create new rule superseding old one (audit trail preserved) |

If you're unsure whether a rule was captured, use `list` to check. Don't try to create rules — the kernel handles that.

## Instruction Classification

The Tier 2 extractor classifies instructions into two types:

- **behavioral_constraint** → becomes a `CovenantRule` (enforced by dispatch gate)
- **automation_rule** → becomes a `standing_order` knowledge entry (not yet enforced by triggers)

## Code Locations

| Component | Path |
|-----------|------|
| CovenantRule dataclass | `kernos/kernel/state.py` |
| NL Contract Parser | `kernos/kernel/contract_parser.py` |
| Covenant validation | `kernos/kernel/covenant_manager.py` |
| MANAGE_COVENANTS_TOOL | `kernos/kernel/covenant_manager.py` |
| Gate enforcement | `kernos/kernel/reasoning.py` (_gate_tool_call) |

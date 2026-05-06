# Dispatch Gate

The dispatch gate guards every write/action tool call before execution. Read operations bypass it. Writes go through three steps: token check, permission override, model evaluation.

The gate logic lives in `kernos/kernel/gate.py`. Earlier code paths kept it inline in `reasoning.py`; that's been extracted. `ReasoningService` calls into the gate via `_get_gate()`.

## How it works

When the agent calls a tool classified as `soft_write` or `hard_write`, the gate fires.

### Step 1: Token check

Programmatic approval tokens for confirmed pending actions. If a valid, unused token matches the tool call (set on the `ApprovalToken` registry), the gate passes immediately. This is the mechanism for "user said yes after a CONFIRM": the handler replays the original tool call carrying the approval token, the gate sees the token, and the action runs.

### Step 2: Permission override

Fast dictionary lookup against the instance profile's `permission_overrides`. If the capability has an `"always-allow"` entry, the gate is bypassed. Permission overrides are mechanical — they are NOT in the covenant text the gate model reads. They're operator-set knobs for capabilities whose own UX already gates the user.

### Step 3: Model evaluation

One cheap-tier LLM call sees:

- Recent user messages (the trailing context)
- The agent's reasoning that led to this tool call
- The proposed tool call with arguments
- Active covenant rules (must / should / never)

The model returns one of four verdicts:

- **APPROVE** — the action directly fulfills what the user asked for in their conversation, OR a standing covenant clearly authorizes it.
- **CONFIRM** — the action was NOT requested by the user (the agent is acting proactively or filling in details). The user should confirm before it runs. The agent's response carries a `[CONFIRM:N]` tag and the action becomes a `PendingAction`.
- **CONFLICT: <rule text>** — the user asked for it, but a `must_not` covenant blocks it. The agent surfaces the conflict to the user in the response.
- **CLARIFY** — the user's request is ambiguous; it could mean multiple things and the agent should ask which one before acting.

(Legacy verdict synonyms `EXPLICIT` and `AUTHORIZED` are still accepted as APPROVE-equivalents during the model parser; older eval rubrics still emit them. New code emits APPROVE.)

The key principle: **reactive actions that serve the user's request → APPROVE.** The user's conversational intent is the authorization. CONFIRM is reserved for proactive moves where the user hasn't asked. When in doubt between APPROVE and CONFIRM, the gate prefers CONFIRM.

## What happens when the gate doesn't approve

When the gate returns CONFIRM or CONFLICT or CLARIFY:

1. The action becomes a `PendingAction` stored on the reasoning service for the (instance, member, space).
2. The agent's response includes a `[CONFIRM:N]` tag (e.g., `[CONFIRM:1]`); the user-facing surface renders it as a confirmation prompt.
3. If the user replies confirming, the handler replays the tool call carrying an approval token — the gate's Step 1 token check passes and the action runs.
4. Pending actions expire after 1 hour (configurable via `KERNOS_PENDING_TTL_S`).

## Effect classification

Every kernel tool is classified by `gate.py:classify_tool_effect`. Read tools bypass the gate; soft_write and hard_write run through it.

| Effect | Gate behavior | Examples |
|---|---|---|
| read | Bypass (no gate) | `remember`, `list_files`, `read_file`, `read_soul`, `read_source`, `request_reference`, `request_tool`, `inspect_state`, `dismiss_whisper`, `canvas_list`, `page_read`, `page_list`, `page_search` |
| soft_write | Gate evaluates | `write_file`, `delete_file`, `update_soul`, `manage_covenants`, `store_reference`, `create_reference_collection`, the four reference recovery primitives, `page_write`, `canvas_preference_extract`, `canvas_preference_confirm`, evaluate (browser JS), `request_space_action`, `consult` |
| hard_write | Gate evaluates with higher scrutiny | `create-event`, `send-email`, `delete-event`, `canvas_create`, accept-parcel |

Action-aware tools (`manage_covenants`, `manage_capabilities`, `manage_channels`, `manage_members`, `manage_plan`, `manage_schedule`, `manage_workspace`) classify per-action: `list` actions are `read`; mutations are `soft_write`.

Unknown tools default to `hard_write` (safe default).

## Hallucination detection + retry

When the agent claims to have used a tool but no tool was actually called (iterations=0 + tool-claiming language in the response text), the system detects this and retries with a corrective system message. If the retry succeeds (honest response or actual tool call), the corrected response is used. If both attempts fabricate, the user sees a graceful error.

## Per-tool denial limit

The gate tracks consecutive denials per tool name in a per-turn counter. If the same tool gets denied N times in a row (default 3, configurable via `KERNOS_GATE_DENIAL_LIMIT`), further calls short-circuit so the agent doesn't loop trying to push through a CONFLICT.

## Code locations

| Component | Path |
|---|---|
| Gate logic | `kernos/kernel/gate.py` (`DispatchGate`) |
| Effect classification | `kernos/kernel/gate.py` (`classify_tool_effect`) |
| `GateResult`, `PendingAction`, `ApprovalToken` | `kernos/kernel/gate.py` + `kernos/kernel/reasoning.py` |
| Hallucination retry | `kernos/kernel/reasoning.py` |
| Confirmation replay | `kernos/messages/handler.py` |
| Permission overrides storage | `kernos/kernel/state.py` (`InstanceProfile.permission_overrides`) |
| Reasoning-service delegation | `kernos/kernel/reasoning.py` (`_get_gate`, `_classify_tool_effect`, `_gate_tool_call`) |

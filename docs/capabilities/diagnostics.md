# Capability: Diagnostics

The diagnostic tool surface — runtime tracing, friction observation, spec submission, LLM chain inspection. These are reach-for-when-something's-off tools, not steady-state tools.

## Tools

| Tool | Effect | Purpose |
|---|---|---|
| `read_runtime_trace` | read | Inspect the per-turn runtime trace — what cohorts ran, what tools dispatched, what events emitted. |
| `diagnose_issue` | read | Surface an issue you've noticed (a stuck plan, a tool surfacing wrong, a missed friction signal). |
| `propose_fix` | soft_write | Propose a fix to a known issue. Lands as a structured artifact for operator review. |
| `submit_spec` | soft_write | Escalate to a spec-shaped artifact when a fix needs architecture work. |
| `set_chain_model` | soft_write | Switch the active LLM provider chain or override the (provider, model) head for the current (instance, member, space). |
| `diagnose_llm_chain` | read | Inspect the active provider chain — which providers, which models, which entry succeeded last, fallback history. |
| `diagnose_messenger` | read | Inspect the messenger pipeline — disclosure gate decisions, surface decisions, pending relational messages. |

## Runtime trace + diagnose_issue

Every turn produces a runtime trace via the trace collector (Improvement Loop T2). The trace records cohort runs, tool dispatches, event emissions, gate decisions, surfaced messages, friction signals.

`read_runtime_trace` lets you inspect a specific turn's trace by turn_id (or the most recent trace if no id is provided). Use it when:

- The user reports something didn't happen and you want to see what the substrate actually did.
- You're investigating a friction signal you noticed in your awareness block.
- You're about to call `diagnose_issue` and want concrete evidence first.

`diagnose_issue` records your observation: what you noticed, where in the trace it shows up, what you expected vs. observed.

## propose_fix → submit_spec escalation path

When a diagnostic observation matures into something actionable:

- **Small, mechanical fix** → `propose_fix` with the structured patch description. Lands in operator inbox.
- **Architectural / multi-file / requires design** → `submit_spec` with the spec body. Lands in architect's queue.

These are admin-tier tools — they create artifacts visible to the founder and architect. Don't reach for them for every minor friction; the friction observer + whisper system handles ongoing observation. Use these when you have a concrete change to propose.

## Provider chain diagnostics

Kernos uses two named LLM provider chains: `primary` (main model) and `lightweight` (fast/cheap tier). Each chain is an ordered list of `ChainEntry(provider, model)`. (The legacy three-chain split — `primary`/`simple`/`cheap` — was collapsed; `simple` was removed as muddled.) Fallback proceeds entry-by-entry on transient failure; the canonical fallback shape is the `_call_chain()` entry point.

`diagnose_llm_chain` shows you:

- Which chains are active.
- The current entry order in each.
- Which entry succeeded last for the current (instance, member, space) context.
- Recent fallback history if downstream entries had to be tried.

`set_chain_model` lets you switch chains or pin a specific (provider, model) head:

```
set_chain_model(chain_name="primary")  # switch to a named chain
set_chain_model(provider="openai-codex", model="gpt-5.4-mini")  # pin head
```

This is for diagnosing a problem with a specific model; revert by calling without overrides.

## Messenger diagnostics

`diagnose_messenger(member_a_id, member_b_id)` inspects the disclosure gate's decision trace for that member pair: their declared relationship, the permission profile, recent disclosure decisions, recent surfaced messages, current escalation tier (per the abuse-prevention model).

Use it when a relational-messaging flow isn't working: a message you sent didn't surface, a message that should have been gated leaked through, an escalation tier feels wrong.

## When to reach for these tools

You're not expected to use diagnostics tools every turn. Reach for them when:

- The user reports something didn't happen the way they expected.
- A friction signal in your awareness block looks load-bearing.
- A tool returned an unexpected error and you want to understand why.
- You're about to escalate to the operator and want concrete evidence first.

## Effect classification

`read_runtime_trace`, `diagnose_issue`, `diagnose_llm_chain`, `diagnose_messenger` are `read`.
`propose_fix`, `submit_spec`, `set_chain_model` are `soft_write` (they create artifacts or modify per-context state; reversible).

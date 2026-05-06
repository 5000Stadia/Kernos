# Capability: External Agents (consult)

EXTERNAL-AGENT-CONSULTATION v1 (shipped 2026-04-30). The `consult` tool — bounded delegation to an external agent (a Claude or other LLM-backed agent running in a sandboxed subprocess) for help on a specific task.

This page is a capability summary. The full architectural detail lives at [`../EXTERNAL-AGENTS.md`](../EXTERNAL-AGENTS.md).

## What it does

`consult` lets you ask another agent for help on a task you've decided is beyond your current context — research, code generation, multi-step reasoning that would burn your own budget. The external agent runs in a subprocess sandbox with a restricted code-exec backend; you get its result back as a tool result.

Common use cases:

- **Research the agent farms out** — "look up how X works in this codebase" against a sandbox checkout.
- **Code generation under safety constraints** — the consult harness can scope the external agent's filesystem access and tool surface.
- **Multi-step reasoning that benefits from a fresh context window** — your context stays clean; the external agent's work returns as a structured result.

## Tool shape

```
consult(
  task: <natural-language description of what you need>,
  harness: <which harness backend to use>,
  ...harness-specific parameters
)
```

The harness chooses the runtime: subprocess substrate, code-exec backend, or a future managed-cloud backend.

## Reentrancy guard

External agents can themselves invoke `consult`, which means an unbounded recursion is possible in theory. The orchestrator enforces a reentrancy guard — depth-limited delegation, with the limit configurable per substrate. Crossing the limit returns a typed error rather than spinning indefinitely.

## Consultation log

Every consult invocation is logged to the consultation log (audit trail) with the task, the harness, the result, and timing. Operators can replay decisions; agents can inspect their own consultation history.

## Agent-surface wiring

The consult tool is in the canonical kernel-tool registry. Effect class: `soft_write` (the external agent runs in a sandbox and may produce side-effects in its own scratch space; nothing it does mutates Kernos state directly).

## When NOT to use it

- For routine documentation lookups — that's `request_reference`, not `consult`.
- For things you can do in one tool call yourself — the consult overhead isn't worth it.
- When the user is in a fast-turn conversation — consult adds latency.

Use it when the task is genuinely beyond your context and the agent farm pays for itself.

# External-Agent Consultation

Kernos has a first-class capability to consult external coding-agent
CLIs — Claude Code, Codex, Gemini, Aider — for review, second
opinions, exploratory thinking, or task execution. The substrate
lives at `kernos/kernel/external_agents/` and is the unified
primitive that subsumes the older `kernos/kernel/builders/` pattern.

This document is the operator + agent reference. The technical spec
is at `specs/EXTERNAL-AGENT-CONSULTATION-V1.md`.

## When to use which mode

External agents support two modes; pick based on what you want back.

| Mode | What you ask | What you get | Tool |
|---|---|---|---|
| **Consult** | A question | Free-text answer | `consult` |
| **Build** | A coding task | File modifications + stdout/stderr | `code_exec` (with `backend=` param) |

## When to use which harness

Each external agent is good at different things. Authoritative
guidance for the primary agent's decision-making:

| Harness | Strong for | Avoid for | Mode |
|---|---|---|---|
| **claude_code** | Architectural review, design critique, "have I missed an edge case?", documentation review | Pure code transforms (use codex or aider) | consult |
| **codex** | Code review of a specific change, second-opinion on a tricky implementation, fast iteration | Long deliberations (sessions are best for short threads) | consult |
| **gemini** | Cross-domain knowledge questions, when you want a different perspective on a tradeoff | Tasks requiring git/repo awareness (its session model is weakest) | consult |
| **aider** | "Implement this change in these files" — task-shaped CLI work | Q&A, exploratory thinking | build only |

## When to consult — and when not to

Consultation costs tokens and adds latency. The primary agent
should reach for it when the value beats the cost. Rubric:

| Use consultation for | Don't use consultation for |
|---|---|
| Code review / second opinion on a non-trivial change | Simple code lookups (use repo search) |
| Architectural sanity check before a big spec | Routine bug fixes — just fix it |
| "Have I missed an edge case?" double-check | User-facing answers — Kernos answers directly |
| Exploratory design space mapping | Tasks that need Kernos's persistent memory |
| Cross-checking a Codex / CC implementation | Lookup-style queries (just grep / open the file) |
| Hard correctness verification on substrate code | Anything you could resolve with a 30-second read |

## What the agent calls

### `consult` tool

```yaml
consult:
  harness: claude_code | codex | gemini    # NOT aider — see below
  question: str                            # the prompt
  context: dict | str                      # optional plumbing
  session_id_raw: str                      # optional, for threading
  workspace_dir: str                       # optional, defaults sensibly
  timeout_seconds: int = 600               # max 1800
  harness_options: dict = {}               # harness-specific knobs
```

Returns: `{response: str, harness: str, session_id: str,
metadata: dict, truncated: bool}`.

`session_id_raw` threads multi-call consultations within the same
agent task. Pass the same value across calls and the harness
preserves prior turns where its CLI supports it (Claude Code:
direct; Codex: thread resume via captured `thread_id`; Gemini:
prompt-replay fallback).

### `code_exec` with `backend=`

```yaml
code_exec:
  code: str
  ...
  backend: native | aider | claude-code | codex   # optional
```

Defaults to `KERNOS_BUILDER` env var (typically `native`). Per-call
override lets the agent pick — e.g., `backend="aider"` to invoke
the Aider builder for one specific edit without changing global
state.

## Aider — special note

Aider has been shipped infrastructure since long before this spec.
v1 is the first time it's actually accessible to the agent on a
per-call basis (via `code_exec(backend="aider", ...)`). Aider does
NOT support consult mode in v1: its CLI is task-shaped, not
Q&A-shaped. Calling `consult(harness="aider", ...)` raises
`HarnessUnavailable` with a clear message.

## Reentrancy policy — what consultation cannot do

Consultation is allowed only from agent-driven flows. The
ContextVar-based reentrancy guard blocks calls from substrate-
critical paths:

| Calling context | Consultation allowed? | Depth limit |
|---|---|---|
| Conversational turn (user message reply) | **Yes** | 2 |
| Drafter cohort flow | **Yes** | 1 |
| Compaction / fact-harvest pipeline | **No** | — |
| CRB approval / dispatch | **No** | — |
| Trigger evaluation runtime | **No** | — |
| Workflow execution (WLP) | **No** | — |
| Recovery sweep | **No** | — |

The blocked contexts have strict latency, deterministic-replay, or
authority-escalation concerns where consultation would be unsafe.
Attempting from a blocked context raises `ReentrancyBlocked`.

Allowed contexts have a depth limit (max nested consults from the
same task). Past the limit raises `DepthExceeded`. Defaults err
toward conservative: a primary agent turn can consult twice; a
drafter cohort can consult once.

## Audit + observability

Every consultation writes a row to `consultation_log` (in
`instance.db`):

* **`consultation_id`** — UUID-ish, primary key.
* **`status`** — `pending` → `succeeded` | `failed` | `timed_out`.
* **`harness`** + **`session_id`** + **`native_session_ref`** —
  triage trail across Kernos's session id and the harness CLI's
  native id.
* **`question`** + **`response`** — full text (TEXT column,
  MB-scale OK; truncation flagged when output exceeds cap).
* **`workspace_dir`** + **`timeout_seconds`** + **`metadata_json`**
  — invocation parameters.
* **`started_at`** + **`ended_at`** + **`exit_status`** + **`error`**
  — timing + failure detail.

Operator queries:

```sql
-- Recent consultations, newest first:
SELECT consultation_id, harness, status, started_at, error
FROM consultation_log
ORDER BY started_at DESC LIMIT 20;

-- Failures + timeouts only:
SELECT * FROM consultation_log
WHERE status IN ('failed', 'timed_out');

-- All turns of a threaded session:
SELECT consultation_id, started_at, question, response
FROM consultation_log
WHERE session_id = '<sanitized_hex>'
ORDER BY started_at;
```

## Architecture

Three layers:

```
kernos/kernel/external_agents/
├── orchestrator.py        ← agent-facing entry; reentrancy gate,
│                            workspace resolution, audit lifecycle
├── registry.py            ← which harnesses are wired; mode-aware get
├── harness.py             ← Harness protocol + result types
├── consultation_log.py    ← durable audit substrate
├── reentrancy.py          ← ContextVar-based calling-context gate
├── subprocess_substrate.py← shared spawn/capture/scope/sanitize
├── errors.py              ← typed hierarchy
├── acpx_adapter.py        ← ACPX dispatch layer (the substrate
│                            boundary for claude_code/codex/gemini)
├── bridge_watcher.py      ← outbound + inbound file-bridge loops
└── harnesses/             ← per-CLI shims (delegate to acpx_adapter)
    ├── claude_code.py
    ├── codex.py
    ├── gemini.py
    └── aider.py
```

`kernos/kernel/builders/` is now a **compatibility facade** —
re-exports from `external_agents/` so `KERNOS_BUILDER` env var,
`code_exec`, and existing imports keep working unchanged.

### `ANTHROPIC_API_KEY` env var caveat (2026-05-20 operator note)

If `ANTHROPIC_API_KEY` is set in the bot's environment, `claude-acp` (the ACPX backend for the Claude Code harness) **uses that API key instead of falling back to the Claude Code subscription / OAuth path**. If the API account has zero credit, every `consult(harness="claude_code", ...)` returns:

```
"Internal error: Credit balance is too low" [billing_error]
```

— and the error comes via JSON-RPC on **stdout**, not stderr, so older dispatch code only logged `stderr: ` (empty), hiding the cause for hours.

**Operator fix**: remove or comment out `ANTHROPIC_API_KEY` from `.env`. Kernos's own reasoning chain (gpt-5.5 / gpt-5.4-mini via the openai-codex provider) doesn't use Anthropic at all; the env var only affected the broker subprocess.

**Code fix**: as of commit 950faa2, dispatch failures now include JSON-RPC error payloads from stdout in addition to stderr — so future provider/auth/billing failures surface immediately in `server.log` instead of going silent.

### ACPX integration (2026-05-18)

The `claude_code`, `codex`, and `gemini` harnesses are now thin
compatibility shims that delegate to `acpx_adapter.dispatch`, which
shells out to [`openclaw/acpx`](https://www.npmjs.com/package/acpx)
— a headless CLI implementing the
[Agent Client Protocol](https://agentclientprotocol.com/) standard.
ACPX speaks ACP to each backend, so Kernos no longer per-CLI-wrangles
flags, session IDs, or output formats.

* **One-shot dispatch:** `acpx <agent> exec <prompt>`
* **Multi-turn:** `acpx <agent> sessions ensure --name <id>` first,
  then `acpx <agent> -s <id> <prompt>`
* **Session IDs** derive from substrate coordinates via
  `derive_session_id(instance_id, target, member_id, conversation_id)`
  — 16-char SHA-256 prefix, deterministic across processes.

Bring-up env vars:

* `KERNOS_ACPX_AUTO_INSTALL=1` — opt-in `npm i -g acpx@<pinned>`
  during bot startup. Default is **fail-loud** (log
  `AGENT_PROTOCOL_UNAVAILABLE`, don't auto-install).
* `KERNOS_ACPX_VERSION` — override the pinned ACPX version (default
  matches `acpx_adapter.EXPECTED_ACPX_VERSION`).
* `KERNOS_INSTANCE_ID` — used by `bridge_watcher` to scope its
  request/response directories.

### Bridge watcher

`bridge_watcher` runs two background loops launched from
`server.py` start-up:

| Loop | Reads | Writes | Purpose |
|------|-------|--------|---------|
| Outbound | `data/<inst>/coding_session_bridge/requests/` | `…/responses/` | Closes the `ask_coding_session` relay gap — Kernos's agent can ask CC/Codex something via ACPX. |
| Inbound  | `data/<inst>/cc_inbox/` | `cc_outbox/` | Lets external CLIs (me-as-CC in another session, scripts) ask Kernos to introspect itself. Read-only kinds: `free_text`, `inspect_state`, `read_file`, `list_files`, `sqlite_query`. |

Both loops use `O_CREAT|O_EXCL` sentinel files (`.processing`) for
atomic claim — concurrent watchers won't double-process. Response
files are written via `tmp+rename` so the response-emitter watcher
never sees partial JSON. Stale-lock recovery probes pid liveness via
`os.kill(pid, 0)` before reclaiming.

The inbound `sqlite_query` enforces SELECT/PRAGMA-only at the
dispatcher; `read_file` and `list_files` reject path-traversal by
resolving the requested path and checking it starts with the
`data_root`. These are hard refusals, not best-effort.

Gemini is wired through the same shim but live testing is gated
behind `KERNOS_LIVE_AGENT_TESTS=1` plus an authenticated install —
the shim works the moment `gemini` lands on PATH with creds, no
additional code change needed.

## Composition with shipped substrate

* **CRB / WDP / WLP / STS** — unchanged. Consultation runs in
  parallel; reentrancy guard prevents recursive interactions.
* **Drafter v2 action_log** — pattern reused for the
  `consultation_log` shape (claim-first lifecycle; durable audit
  across crashes).
* **MODEL-AND-STATUS-V1** — independent. Member can have their
  consultations governed by their model-override settings if the
  harness-specific config picks up `harness_options` for that
  steering.
* **Multi-member** — every consultation is per-(instance, member);
  audit row carries member_id; reentrancy state is per-async-task.

## Future arcs

Out of scope for v1, in scope for follow-on specs:

* **MCP transport** for harnesses with MCP server modes (Claude
  Code's `claude mcp`). v1 ships subprocess + ACPX.
* **`KERNOS-MCP-SERVER-V1`** — Kernos exposing its own tools as MCP
  so external agents can consult Kernos. Complementary direction
  to the inbound watcher's read-only file bridge.
* **Streaming consultation** surfaced to the Kernos agent (ACPX
  already streams NDJSON; the shim currently accumulates).
* **Token-budget enforcement** beyond per-call timeout.
* **User-facing consultation UI / inspection.**

## References

* Spec: `specs/EXTERNAL-AGENT-CONSULTATION-V1.md`.
* Agent code: `kernos/kernel/external_agents/`.
* Tests: `tests/test_external_agents_*.py`,
  `tests/test_acpx_adapter.py`, `tests/test_bridge_watcher.py`,
  and `tests/test_code_exec_backend_param.py`.
* Live tests guarded by `KERNOS_LIVE_AGENT_TESTS=1` env var.
* ACPX upstream: <https://www.npmjs.com/package/acpx>
* Agent Client Protocol: <https://agentclientprotocol.com/>
* Background on why ACP-over-subprocess avoids the hang pattern
  that bespoke per-CLI subprocess wrangling hits:
  <https://bighatgroup.com/blog/using-acp-with-openclaw-to-prevent-agent-hangs/>

# SUBSTRATE-SELF-TEST-V1

## Plain-English overview

This section is for human readers and for Kernos itself doing a
perspective check. The spec body below is the technical contract.

### What this spec exists to stop

Over a single session, Kernos's substrate revealed seven distinct
fundamental bugs while trying to perform one routine agent
round-trip (read a spec, ask a coding agent, get a response,
propose an action). Each bug was individually correct to fix; each
fix shipped clean tests. But the rate of new fundamental bugs
surfacing per round-trip — and the structural shape of all seven
(silent-failure-on-edge-case at a substrate boundary) — meant we
were converging too slowly. Codex's structural read named this:

> Substrate growth outrunning invariant growth. The seven bugs
> are not random defects; they are the same failure shape
> appearing at different boundaries: duplicated dispatch entry
> points, detector paths with separate truth models,
> retry/restart/self-heal mechanisms that recover locally but do
> not prove the full agent loop still works.

The diagnosis is honest accumulated tech debt, not a fix-begets-
bug spiral. The fixes are net-positive. The pattern is that each
fix was local because the *invariant* it enforced was local —
each one closed a single boundary's failure mode without
asserting the full round-trip still composes.

This spec moves substrate invariants from local pin-tests to
executable pre-merge contracts. A small set of round-trip soaks
that the substrate must pass before any commit lands, exercising
the load-bearing boundaries together rather than in isolation.

### What changes about how Kernos is developed

Today: a fix lands, its local unit tests pass, the broader test
suite passes, the operator restarts the bot, and discovers in
production whether the substrate still functions as a whole.

After this spec: the same fix lands, AND a suite of substrate
round-trip soaks runs as part of the same merge gate. If the
round-trip soak fails, the commit doesn't merge. Six of the
seven runtime-substrate bugs we just shipped fixes for would
have been caught pre-merge by these soaks (each one corresponds
to a probe in the suite). The seventh —
`restart_self` description clarity (`5cec074`) — was a
tool-description text fix and is honestly out of scope for
substrate soak coverage; it stays unit-test-only. See the
explicit exclusion rationale in the AC6 mutation matrix.

The soak suite is not a replacement for unit tests. It's an
additional layer that asserts *composition* — that the substrate
behaves correctly when its boundaries interact, not just when
each boundary is exercised in isolation.

### The eight probe categories

Each probe is a small, deterministic test that exercises one
substrate invariant the agent depends on for normal operation.
v1 ships these eight, named for the bugs that exposed them:

1. **Agent round-trip soak** — synthetic inbound message flows
   through handler / reasoning / integration / dispatcher with
   fakes BELOW the dispatcher seam (so the dispatcher actually
   runs, not stubbed). Behavioral signal + substrate state
   match expectations.
2. **Self-knowledge invariant** — `read_source` can reach
   `specs/` and `docs/` paths, not just the kernos package.
3. **Consult drain invariant** — synthetic ACPX-shaped
   subprocess emits a single line larger than 64 KiB; the
   dispatch completes without crashing the drain task.
4. **Dispatch canonicalization invariant** — for every entry
   in the alias dict (not one example), the alias is repaired
   correctly at all three ingress points and emits the
   appropriate receipt (async event from reasoning + enactment;
   log line from sync gate).
5. **Retry-with-feedback invariant** — when an integration
   attempt fails validation, attempt N+1's prompt explicitly
   includes attempt N's failure reason text. Three blind
   identical failures must not be possible.
6. **Gateway deafness invariant** — given healthy heartbeats
   AND total socket silence past the deaf window, the full
   detect → strike → restart cascade fires (3 strikes →
   `os.execv` called exactly once), not just the initial
   detection.
7. **Approval loop invariant** — an improvement attempt
   reaches the approval-receipt boundary with full binding,
   exercised through the full agent / spec / consult dispatch
   path against a fake ACPX-shaped binary (not a stub consult).
8. **Loop-health completion invariant** — the loop-health
   workflow's boot probe reaches
   `LOOP_HEALTH_EXECUTION_COMPLETED` within 30 seconds. Closes
   a gap from the parent autonomy spec that never landed as a
   probe.

Each probe asserts both a behavioral signal (the user-facing
outcome) AND a substrate state assertion (what changed in the
event stream, in SQLite, in the catalog). This matches the
existing testing pattern documented at `docs/TESTING-PATTERN.md`
which the smoke gate currently doesn't honor.

### What this spec does NOT do

- Does NOT consolidate the parallel dispatch ingresses into one
  canonical pipeline. That's the structural fix Codex named as
  the deeper architectural debt, but it's a substantial
  refactor and lands as a follow-up. v1 just asserts the
  invariant holds across all current ingresses.
- Does NOT unify the observer + watchdog into one health
  pipeline. Same logic — follow-up structural work.
- Does NOT add LLM-driven probes. Every probe is deterministic
  with hard fakes; no model dispatch in the soak suite.
- Does NOT replace any existing tests. Adds a new layer.

### Why these eight and not others

Seven of the eight are exactly the boundaries that surfaced as
broken in the 2026-05-25 session. The eighth (loop-health
completion) closes a known gap from the parent autonomy spec
that never landed as a probe. The principle going forward:
every time a new substrate boundary's failure surfaces in
production, the fix lands AND a probe gets added to this suite.
The suite grows empirically from operational evidence, not from
upfront prediction.

---

The remainder of this document is the technical spec the
implementation builds against.

---

**Date:** 2026-05-26 (v4 after Codex round-3 fold)
**Status:** Draft for review
**Scope:** New test layer at `tests/substrate_soak/` plus a
  smoke-gate extension in `kernos/kernel/self_test_gate.py` that
  runs the eight probes pre-merge AND as part of the
  post-bring-up health check. Eight probes shipping in v1 (seven
  named for boundaries that surfaced in the 2026-05-25 session,
  plus loop-health-completion which closes a gap from the parent
  autonomy spec).
**Estimated size:** ~600 LOC test files + ~150 LOC smoke-gate
  extension + ~50 LOC fixtures.

## Why this spec exists

Codex's structural read of the 2026-05-25 session named this
substrate-growth-outrunning-invariant-growth. The seven bugs
shipped that session — `f03e351`, `07226c8`, `dbfbdab`,
`5cec074`, `f8835e7`, `521c7f5`, `a7302b0` — each closed a
single boundary's failure mode with local tests. None of them
asserted the full agent round-trip composes correctly. The
existing smoke gate (`kernos/kernel/self_test_gate.py:30-34`)
runs only three substrate files; the parent autonomy spec
(`specs/KERNOS-AUTONOMOUS-IMPROVEMENT-LOOP-V1.md:469-476`)
expected autonomy-loop e2e + after-boot live probe that never
landed.

This spec adds the missing layer. Eight probes that exercise
substrate composition at the boundaries that have proven
load-bearing for production correctness.

## Design principles (load-bearing)

- **Probes are deterministic — no real external providers.**
  No real Anthropic/OpenAI model dispatch, no real Discord
  gateway connection, no real third-party HTTP. Fake binaries
  and fake subprocesses ARE acceptable for testing the
  dispatch pipeline shape (Probe 3 spawns a synthetic
  subprocess that emits NDJSON; Probe 7 uses a fake
  claude-code-shaped binary). The constraint is "no real
  external services," not "no real subprocesses." A probe
  failure points at Kernos's substrate, not at flaky external
  dependencies.
- **Each probe asserts substrate-fidelity per `docs/TESTING-
  PATTERN.md`** — behavioral signal AND substrate state, not
  just one. The probe API enforces this: each probe module
  declares `REQUIRED_BEHAVIORAL_KEYS` and
  `REQUIRED_SUBSTRATE_KEYS` as module-level constants, and
  `SubstrateSoakRunner` rejects ProbeResults missing any
  declared key OR containing only sentinel values (e.g.
  `{"ok": True}` is NOT acceptable as substrate evidence per
  Codex's round-1 concern).
- **The soak suite runs pre-merge AND post-bring-up via the
  same code path.** CI invokes the same `handle_run_self_test_
  suite(include_soak=True)` entry point developers use locally
  and the post-bring-up hook calls — one canonical contract,
  not two parallel gates. Drift between "what CI runs" and
  "what the substrate runs against itself" is the failure
  mode this principle exists to prevent.
- **New boundaries grow the suite from operational evidence.**
  Every time a new substrate failure surfaces in production,
  the fix lands AND a probe gets added. The suite is a living
  registry of "things the substrate has demonstrably gotten
  wrong before; here's the contract that pins them."
- **Fail loud, fail attributable, fail-gated.** Per
  [[loud-fail-over-silent-degradation]] — when a probe fails,
  the failure message names the specific boundary that broke,
  cites the originating bug-fix commit if applicable, and
  surfaces enough state for operator triage without a
  debugger. AND: while a `substrate.self_test_failed` event is
  current, autonomous commit/push workflows (improve_kernos
  orchestrator's git_commit + git_push primitives, the
  self-improvement workflow's autonomous merge paths) MUST
  refuse to declare green. Operator-initiated work proceeds;
  substrate-initiated mutation pauses until the failure is
  resolved or explicitly overridden.
- **Provider/ACPX-contract drift is real and out-of-band.**
  Codex's round-1 concern: a fake binary can't prove the live
  ACPX binary still matches the NDJSON shape. v1 ships a
  non-gating live-provider soak as a scheduled job
  (`scripts/run_live_provider_soak.sh`, fires daily via cron
  or on-demand) that exercises one real ACPX dispatch end-to-
  end against the actual claude-code/codex binaries. Failure
  there is loud-signal-only, NOT a merge gate (live providers
  are too flaky to gate pre-merge on). The deterministic suite
  remains the merge gate.

## What exists, what's missing

**Already in place:**
- `docs/TESTING-PATTERN.md` — documents the
  behavioral-signal + substrate-state assertion pattern this
  spec operationalizes.
- `kernos/kernel/self_test_gate.py` — existing smoke gate
  primitive; currently runs only 3 substrate files
  (lines 30-34).
- 334 test files, 6,401 `test_` functions — substantial unit
  coverage at the seam level.
- Per-probe regression pins for each of the 7 fixes shipped
  this session (the unit tests landed with each commit).
- `IMPROVEMENT-LOOP-WORKFLOW-V1` infrastructure for orchestrated
  round-trips (workspace, ledger, approval receipts).

**Missing — what this spec adds:**
- A dedicated `tests/substrate_soak/` directory containing the
  eight probe modules.
- A `SubstrateSoakRunner` helper in
  `kernos/kernel/self_test_gate.py` that drives the probes
  with shared fixtures (deterministic model/tool fakes,
  ephemeral SQLite, captured event stream).
- An extension to `run_self_test_suite` that runs the soak
  probes alongside the existing 3-file smoke gate, with
  separate reporting so soak failures don't get lost in the
  smoke output.
- A post-bring-up hook in `kernos/setup/bring_up_substrate.py`
  that runs the soak suite once per boot and emits
  `substrate.self_test_passed` or `substrate.self_test_failed`
  events.

## Probe inventory (v1 — eight)

Each probe lives in its own module under `tests/substrate_soak/`.
Each module exposes:
- `async def run_probe(fixtures) -> ProbeResult` — runs the probe
- `REQUIRED_BEHAVIORAL_KEYS: frozenset[str]` — keys the probe
  promises to populate in `ProbeResult.behavioral_evidence`
- `REQUIRED_SUBSTRATE_KEYS: frozenset[str]` — keys the probe
  promises to populate in `ProbeResult.substrate_evidence`

`ProbeResult` carries `passed: bool`, `probe_name: str`,
`behavioral_evidence: dict`, `substrate_evidence: dict`,
`duration_ms: int`. `SubstrateSoakRunner` validates both
evidence dicts against the module's declared key sets AND
rejects sentinel-only values (e.g. dict containing only
`{"ok": True}` fails the contract check even if the keys are
declared) — this is what stops the "shallow evidence" failure
mode Codex flagged.

### Probe 1 — `agent_round_trip_soak.py`

**Asserts:** a synthetic inbound user message flows through the
full handler → reasoning → integration → dispatcher → response
chain, exercising the real dispatcher and the real executor.
Fakes sit BELOW the dispatcher — at the model-provider seam and
at the tool-handler seam — so the dispatcher's behavior is
genuinely tested rather than mocked away. Per Codex round-1
(docs/TESTING-PATTERN.md): "don't mock the boundary you're
verifying."

**Fixture shape:**
- Real `MessageHandler` with real `ReasoningService` and real
  `StepDispatcher`.
- Fake at the model-provider seam: a deterministic provider
  that returns pre-canned `ContentBlock` lists keyed on the
  request shape.
- Fake at the tool-handler seam: real `_KERNEL_TOOLS`
  registration with one synthetic tool (e.g.
  `_soak_echo_tool`) whose handler returns a pre-canned
  result. The dispatcher's classify + canonicalize + lookup
  + execute path runs for real against this tool.
- Ephemeral SQLite event store (`:memory:`).

**Required behavioral keys:** `response_text`,
`response_tool_calls`.

**Required substrate keys:** `event_stream_kinds_in_order`,
`tool_dispatch_canonical_name`, `gate_classification`.

**Pass condition:**
- `behavioral_evidence["response_text"]` matches the expected
  output string exactly.
- `substrate_evidence["event_stream_kinds_in_order"]` contains
  `message.received`, `reasoning.request`, `reasoning.
  response`, `tool.called`, `tool.result`, `message.sent` in
  that order.
- `substrate_evidence["tool_dispatch_canonical_name"]` matches
  the synthetic tool's canonical name (proves dispatcher
  actually ran, not stubbed).
- `substrate_evidence["gate_classification"]` matches the
  expected classification (proves gate actually ran).

**Regression bug:** none specific — this is the umbrella probe
that catches integration regressions the other seven can't see
in isolation.

### Probe 2 — `self_knowledge_invariant.py`

**Asserts:** `read_source(path="specs/SUBSTRATE-SELF-TEST-V1.md")`
returns this very file's contents (or any spec file shipped at
suite-run time); `read_source(path="docs/TECHNICAL-
ARCHITECTURE.md")` returns the doc contents; bare-path
`read_source(path="kernel/awareness.py")` still works for
back-compat; security check (`path="../etc/passwd"`) still
rejects.

**Required behavioral keys:** `spec_read_result`,
`doc_read_result`, `kernos_read_result`, `traversal_reject_result`.

**Required substrate keys:** `allowed_roots`, `repo_root_resolved`.

**Pass condition:**
- All four call results contain non-error content (specs/docs
  reads include known substrings; kernos read includes "class"
  or "def"; traversal rejects with "not allowed").
- `substrate_evidence["allowed_roots"]` equals the
  current allowed-root set (`{kernos, specs, docs}`).

**Regression bug:** `07226c8`. Kernos couldn't read its own
specs because `read_source` was scoped to `kernos/` only.

### Probe 3 — `consult_drain_invariant.py`

**Asserts:** a fake ACPX-shaped subprocess emits a single
NDJSON line of >64 KiB on stdout (mirroring claude-code's
behavior when asked to read a large file); the dispatch drain
handles the line without raising `LimitOverrunError` and
surfaces the full content to the caller.

**Fixture shape:** spawns a real local subprocess (a small
`python -c '...'` invocation) that prints a synthetic NDJSON
event with an 80 KiB inlined payload conforming to the ACPX
event shape, then exits cleanly. NOT the real ACPX binary, NOT
a network call — a synthetic shape-compatible subprocess. Per
the design-principle clarification: "fake binaries are
acceptable; no real external providers."

**Required behavioral keys:** `dispatch_return_value`,
`accumulated_content_length`.

**Required substrate keys:** `largest_line_bytes_observed`,
`drain_completed_without_exception`.

**Pass condition:**
- `behavioral_evidence["dispatch_return_value"]` is a
  successful `ConsultResult` (not a `ConsultationFailed`
  exception).
- `behavioral_evidence["accumulated_content_length"]` >=
  80 KiB (proves the giant line was actually accumulated, not
  silently truncated).
- `substrate_evidence["largest_line_bytes_observed"]` >= 80 KiB
  (proves the drain DID see the giant line; pre-fix the drain
  would have crashed before recording this).
- `substrate_evidence["drain_completed_without_exception"]` is
  `True`.

**Regression bug:** `dbfbdab`. ACPX stdout drain crashed on
lines >64 KiB; surfaced as opaque `ConsultationFailed`.

### Probe 4 — `dispatch_canonicalization_invariant.py`

**Asserts:** for EVERY entry in `_TOOL_ALIASES`, all three
ingress points canonicalize correctly. Codex round-1: testing
one alias is insufficient — the alias table is the substrate's
durable record of observed hallucinations, and any new entry
that bypasses an ingress is a regression. Iterate the full
table, not one example.

**Per-ingress contract:**
- `ReasoningService.execute_tool` (async) — repairs, dispatches
  to canonical tool, emits `tool.alias_repaired` event with
  `context="dispatch"`.
- `DispatchGate.classify_tool_effect` (sync) — repairs and
  classifies. Does NOT emit an event because the call site is
  synchronous (per `kernos/kernel/gate.py:339` comment); it
  logs `TOOL_ALIAS_REPAIR alias=X canonical=Y context=classify`
  at INFO level instead. Probe asserts the LOG line is present,
  not an event.
- `StepDispatcher.dispatch` (async) — repairs, reaches the
  descriptor lookup with the canonical name, emits
  `tool.alias_repaired` event with `context="enactment"`.

This matches the current substrate shape (per Codex round-1
Q6) — the gate's sync constraint is a real architectural
condition the v1 probe must accommodate. The deeper
consolidation (single async repair pipeline at one ingress)
is a tracked follow-up spec (see "Tracked follow-up specs"
section).

**Required behavioral keys:** `alias_canonical_mappings_observed`
(dict of every alias → canonical pair the probe verified).

**Required substrate keys:** `reasoning_events_per_alias`,
`gate_log_lines_per_alias`, `enactment_events_per_alias`.

**Pass condition:**
- For every alias in `_TOOL_ALIASES`:
  - reasoning ingress emitted exactly one
    `tool.alias_repaired` event with `requested=alias`,
    `canonical=expected_canonical`, `context="dispatch"`.
  - gate ingress emitted exactly one INFO log line matching
    the expected format.
  - enactment ingress emitted exactly one
    `tool.alias_repaired` event with `context="enactment"`.
- Total event count matches `2 * len(_TOOL_ALIASES)` (one per
  alias per async ingress; gate has no event by design).

**Regression bug:** `f03e351` + `f8835e7`. Alias-repair was
wired into reasoning + gate but missed enactment dispatcher;
required a separate commit to close.

### Probe 5 — `retry_with_feedback_invariant.py`

**Asserts:** when an integration synthesis attempt fails
validation (intentionally invalid finalize block), attempt N+1's
prompt includes a `<prior_attempt_failures>` block naming the
specific component + reason that failed in attempt N.

**Required behavioral keys:** `attempt_1_outcome`,
`attempt_2_outcome`, `final_briefing_received`.

**Required substrate keys:** `attempt_2_prompt_contains_block`,
`attempt_2_prompt_failure_reason_text`,
`prior_attempt_failures_count`.

**Pass condition:**
- `behavioral_evidence["attempt_1_outcome"]` is "failed".
- `behavioral_evidence["attempt_2_outcome"]` is "succeeded"
  (with a valid briefing).
- `substrate_evidence["attempt_2_prompt_contains_block"]` is
  `True` — the second attempt's first message body contains
  `<prior_attempt_failures>`.
- `substrate_evidence["attempt_2_prompt_failure_reason_text"]`
  contains the exact failure reason string from attempt 1
  (not just generic retry framing — actionable specifics).
- `substrate_evidence["prior_attempt_failures_count"]` equals
  1 (one prior failure visible to attempt 2).

**Regression bug:** `521c7f5`. Retry loop replayed identical
prompts; 3x identical `ProposeTool.reason` failures observed in
production.

### Probe 6 — `gateway_deafness_invariant.py`

**Asserts:** with a stub Discord client reporting healthy
latency AND `_last_any_socket_event_ts` backdated past the deaf
window, the FULL detect-strike-restart cascade fires:
1. `_is_gateway_heartbeat_unhealthy()` returns True with the
   silence reason.
2. `GatewayHealthObserver._detect_gateway_deaf()` returns a
   `DISCORD_GATEWAY_DEAF` FrictionSignal with
   `pattern=total_socket_silence` evidence.
3. Three sequential `_watchdog_tick()` calls (simulating three
   intervals of continued unhealthy state) escalate from
   strike 1 → 2 → 3 and call the monkey-patched
   `os.execv` exactly once on the third strike.

Per Codex round-1 Q1: the probe must include the strike-to-
restart path because that's the actual invariant — detection
without escalation is not the full contract.

**Required behavioral keys:** `watchdog_unhealthy`,
`observer_signal_emitted`, `execv_called`.

**Required substrate keys:** `watchdog_reason`,
`observer_signal_evidence`, `strikes_before_restart`,
`execv_call_count`.

**Pass condition:**
- `behavioral_evidence["watchdog_unhealthy"]` is `True` and
  `behavioral_evidence["observer_signal_emitted"]` is `True`.
- `substrate_evidence["watchdog_reason"]` contains
  "no socket events received" and
  "gateway deaf despite latency".
- `substrate_evidence["observer_signal_evidence"]` includes
  `"pattern=total_socket_silence"`.
- `substrate_evidence["strikes_before_restart"]` equals 3.
- `substrate_evidence["execv_call_count"]` equals 1 (proves
  the escalation actually reached the restart call, not just
  fired strikes).

**Regression bug:** `a7302b0`. Both detection layers were blind
to "healthy heartbeat + total socket silence" for the 100-minute
production incident.

### Probe 7 — `approval_loop_invariant.py`

**Asserts:** an improvement attempt is started via
`improve_kernos(spec_requirement="trivial change")`, walks the
full orchestrator path (workspace setup → spec cycle → impl
cycle → approval receipt), and reaches a state where the
approval receipt has a valid `binding_payload_json` containing
`attempt_id`, `expected_parent_sha`, `expected_diff_hash`.

**Fixture shape:**
- Real `ImprovementLoopOrchestrator`, real
  `improvement_workspace`, real `improvement_ledger`, real
  `approval_receipts`.
- Real `acpx_adapter.dispatch()` call path. To exercise this
  WITHOUT a real network call, the probe installs a fake
  `acpx` binary on `PATH` (an executable shell script or
  `python -c '...'` wrapper) that emits canned NDJSON events
  matching a successful claude-code session. The
  `acpx_binary()` resolver finds this fake first; the real
  binary is never invoked. Per Codex round-1 Q6 — the fake
  must be ACPX-compatible (matching the binary contract +
  flag parsing), not just any subprocess.
- Real git worktree (the probe creates a temporary repo + an
  initial commit; the orchestrator commits a one-line change
  to it).
- Real SQLite for ledger + approval-receipts tables (ephemeral
  database in tmp path).

**Required behavioral keys:** `attempt_id`,
`approval_id`, `final_state`.

**Required substrate keys:** `binding_payload_attempt_id`,
`binding_payload_expected_parent_sha`,
`binding_payload_expected_diff_hash`, `ledger_event_kinds`.

**Pass condition:**
- `behavioral_evidence["attempt_id"]` starts with "att_".
- `substrate_evidence["binding_payload_attempt_id"]` matches
  the attempt_id.
- `substrate_evidence["binding_payload_expected_parent_sha"]`
  is a valid SHA hex.
- `substrate_evidence["binding_payload_expected_diff_hash"]`
  starts with "sha256:".
- `substrate_evidence["ledger_event_kinds"]` contains
  `workspace_created`, `spec_iteration`, `impl_iteration`,
  `approval_requested` (proves the full orchestrator path
  ran, not just the receipt issue at the end).

**Difference from existing test:** the existing
`test_ac6_approval_receipt_issued_with_binding` test uses a
stub consult (`_make_converging_consult()`). This probe uses
the real ACPX adapter pointed at a fake ACPX-shaped binary
that emits canned content. It exercises the FULL agent /
spec / consult dispatch path the production loop will take —
the fake is at the binary contract boundary, not at the
adapter API.

**Regression bug:** none specific — this is the umbrella probe
that catches improvement-loop composition regressions.

### Probe 8 — `loop_health_completion_invariant.py`

**Asserts:** the loop-health workflow emits its boot probe AND
that probe reaches `LOOP_HEALTH_EXECUTION_COMPLETED` within
30 seconds. Per parent autonomy spec
(`specs/KERNOS-AUTONOMOUS-IMPROVEMENT-LOOP-V1.md:469-476`):
this end-to-end completion was specifically called for but
never landed as a probe. Codex round-1 Q7 flagged the gap.

**Fixture shape:**
- Real `loop_health` workflow registered against an ephemeral
  workflow runtime.
- Real `emit_boot_probe()` invocation against a fake
  workflow-execution backend that processes triggers + actions
  in-process.
- A 30-second deadline (configurable via env var for slow CI).

**Required behavioral keys:** `boot_probe_emitted`,
`execution_completed`, `elapsed_seconds`.

**Required substrate keys:** `boot_probe_event_payload`,
`completion_event_payload`, `workflow_execution_outcome`.

**Pass condition:**
- `behavioral_evidence["boot_probe_emitted"]` is `True` within
  1 second of probe start.
- `behavioral_evidence["execution_completed"]` is `True`
  within the deadline.
- `behavioral_evidence["elapsed_seconds"]` < 30.
- `substrate_evidence["workflow_execution_outcome"]` is
  `"completed"`.
- Both event payloads carry the same `boot_id` (proves the
  completion event is bound to THIS boot probe, not a stale
  earlier one).

**Regression bug:** none yet — this probe is the missing
acceptance criterion from the parent autonomy spec. Pins it
before regression has a chance to happen.

## SubstrateSoakRunner

New helper in `kernos/kernel/self_test_gate.py`:

```python
class SubstrateSoakRunner:
    """Drives the eight substrate-soak probes with shared
    fixtures. Probes are deterministic — hard fakes injected at
    well-defined seams; no real models, no real ACPX, no real
    Discord. A probe failure points at Kernos's substrate, not
    at flaky external dependencies.

    SUBSTRATE-SELF-TEST-V1 (2026-05-26)."""

    async def run_all(self) -> SoakSuiteResult:
        """Run all probes serially. Returns SoakSuiteResult with
        per-probe outcomes plus aggregate pass/fail. Each probe
        runs against a fresh fixture set so failures don't
        contaminate later probes."""

    async def run_probe(self, probe_name: str) -> ProbeResult:
        """Run a single probe by name. For targeted retries +
        operator-driven post-mortems."""
```

## Smoke gate extension

The existing kernel tool `run_self_test_suite` (schema
`RUN_SELF_TEST_SUITE_TOOL` at
`kernos/kernel/self_test_gate.py:46`, handler
`handle_run_self_test_suite` at the same file's line 174)
extends to accept an optional `include_soak: bool = False`
parameter in the schema (default False for back-compat; CI
flips it on). When True, the handler runs the existing 3-file
smoke gate AND `SubstrateSoakRunner.run_all()`, reporting them
under separate result keys so soak failures don't get lost in
the smoke output.

**Single canonical contract (Codex round-1 Q6):** the CI
workflow, the local developer script, and the post-bring-up
hook ALL invoke `handle_run_self_test_suite(include_soak=
True)` — not separate `pytest tests/substrate_soak/`
invocations. This prevents drift between "what CI gates on"
and "what the substrate runs against itself." The pytest test
files under `tests/substrate_soak/` exist for IDE discovery
and individual-probe development, but the contract gate is
always the tool path.

## Post-bring-up hook

New step in `bring_up_substrate.py` after the existing
`emit_boot_probe()` call (per
`kernos/setup/bring_up_substrate.py:732`; the
`LOOP_HEALTH_BOOT_PROBE_FIRED` line is a log emitted by
`emit_boot_probe()`, not a named hook): invoke
`handle_run_self_test_suite(include_soak=True)` once. On full
pass, emit `substrate.self_test_passed` event with per-probe
durations.

On any-probe failure: emit `substrate.self_test_failed` event
with severity=unhealthy, the failing probe names, and
behavioral + substrate evidence in the payload. Log loud at
WARNING level. Does NOT abort bring-up — operator sees the
loud signal and decides whether to continue, restart, or roll
back.

**Autonomous-mutation gate (Codex round-1 Q5 caveat):** while
a `substrate.self_test_failed` event is the most recent
substrate-self-test event in the event stream (i.e. unhealthy
state is current), autonomous commit/push workflows MUST
refuse to declare green. Specifically:

- `improve_kernos` orchestrator's `git_commit` + `git_push`
  primitives check for current unhealthy state at action time
  and refuse with `SubstrateUnhealthyError`.
- The `self_improvement` workflow's `mark_resolved` (or
  closure-spec's `run_closure_probe`) similarly refuse to
  transition state while unhealthy.
- Operator-initiated work (slash commands, manual REPL
  invocation) proceeds without this gate — the gate is on
  AUTONOMOUS substrate mutation, not human-initiated work.

This is the "don't let the substrate quietly do real things
while it knows itself to be broken" guardrail. A successful
later `substrate.self_test_passed` event clears the gate.

## Pre-merge integration

A new GitHub Actions workflow (`.github/workflows/substrate-
soak.yml`) runs:

```bash
python -m kernos.kernel.self_test_gate --include-soak --json
```

— a small CLI wrapper around `handle_run_self_test_suite` so
CI and the substrate run THE SAME PATH. Failure (non-zero
exit) blocks merge.

For local pre-commit: `scripts/run_substrate_soak.sh` (new)
invokes the same CLI. Optional pre-commit hook
(`pre-commit-config.yaml` entry, opt-in) wires it.

## Acceptance criteria

**AC1 — Eight probe modules exist** under `tests/substrate_
soak/` with the names listed in the inventory section above.

**AC2 — Each probe declares + populates required evidence
keys; runner rejects shallow evidence.**
- Every probe module exposes module-level
  `REQUIRED_BEHAVIORAL_KEYS: frozenset[str]` and
  `REQUIRED_SUBSTRATE_KEYS: frozenset[str]` constants.
- `SubstrateSoakRunner` validates each ProbeResult against
  these constants: every declared key must be present AND must
  carry a non-sentinel value (`{"ok": True}` is rejected;
  bools, numerics, strings, and structured dicts are OK).
- A probe that returns shallow/sentinel evidence is reported
  as failed with `failure_reason="shallow_evidence"` even if
  its `passed: True` flag is set.

**AC3 — `SubstrateSoakRunner.run_all()` runs all eight serially
against fresh fixtures.** Test verifies isolation: a probe
failure doesn't contaminate the next probe.

**AC4 — `handle_run_self_test_suite(include_soak=True)` runs
the soak suite alongside the existing smoke gate.** Separate
result keys. Schema update adds `include_soak: boolean`
(default false) to `RUN_SELF_TEST_SUITE_TOOL`.

**AC5 — Post-bring-up hook emits `substrate.self_test_passed`
on full pass and `substrate.self_test_failed` on any failure**
with severity, per-probe outcomes, and evidence in the
payload. Does NOT abort bring-up.

**AC6 — Mutation matrix proves probe attribution.** New test
`tests/substrate_soak/test_mutation_matrix.py` enumerates
known mutations (one per shipped fix from the 2026-05-25
session) and runs the full soak suite under each. For each
mutation row:
- exactly ONE probe must fail (not zero, not two+)
- the failing probe must be the one mapped to that mutation
  in the table below

Mutation table:

| Mutation | Expected failing probe |
|---|---|
| monkeypatch `read_source` to require `kernos/` prefix | Probe 2 |
| monkeypatch `_read_lines_unbounded` to `async for line in reader` | Probe 3 |
| monkeypatch `canonicalize_tool_name` to identity | Probe 4 |
| monkeypatch enactment dispatcher to skip alias-repair | Probe 4 |
| monkeypatch `_build_initial_messages` to ignore `prior_attempt_failures` | Probe 5 |
| monkeypatch `_is_gateway_heartbeat_unhealthy` to skip the silence check | Probe 6 |
| monkeypatch `_detect_gateway_deaf` to skip pattern B | Probe 6 |
| stub `consult_fn` to bypass the real ACPX dispatch | Probe 7 |
| monkeypatch `emit_boot_probe` to no-op | Probe 8 |
| monkeypatch the model-provider seam in Probe 1 to skip dispatcher | Probe 1 |

This is the "would this probe have caught the bug" proof —
not informal verification, an executable matrix.

**Explicit exclusion: `5cec074` (`restart_self` description
clarity).** This was a tool-description text change to clarify
that calls after `restart_self` in the same response are
dropped. The mutation would be "revert the description"; the
soak suite cannot catch a description regression because it
does not exercise model interpretation of the description (no
real model dispatch in the deterministic suite per the
"Probes are deterministic — no real external providers"
principle in the Design principles section above). This fix
stays unit-test-only (the
`test_restart_self_description_names_turn_boundary` regression
pin at `tests/test_self_admin_tools.py`). Codex round-2
flagged the inconsistency with "all seven would be caught" —
the suite catches the six runtime-substrate fixes; the
seventh is a description fix and is honestly out of scope for
substrate soak coverage.

**AC7 — GitHub Actions workflow + local script gate on the
single canonical contract.** Both invoke
`python -m kernos.kernel.self_test_gate --include-soak`. PR
with a probe-failing branch cannot merge to main. Local
developers running the script see the same pass/fail.

**AC8 — Soak suite runs in under 60 seconds** end-to-end on a
clean repo. Per Codex round-1 watchpoint: AC8's prior 30s
budget was likely tight given Probe 7 walks the full
improvement-loop workspace + git + approval flow. Raised to
60s; if it overruns in practice, individual probes can be
flagged for optimization or moved to a slower nightly tier.

**AC9 — Autonomous-mutation gate fires on unhealthy state.**
When the most recent `substrate.self_test_*` event in the
event stream is `substrate.self_test_failed`, calls to
`git_commit` and `git_push` primitives from the
`improve_kernos` orchestrator path raise
`SubstrateUnhealthyError` with the current failing probe
names. Calls from operator-initiated paths (slash commands,
REPL) proceed unaffected.

**AC10 — Live-provider soak script exists but does NOT gate
merges.** `scripts/run_live_provider_soak.sh` invokes one
real ACPX dispatch end-to-end (claude-code or codex,
configurable). Documented as on-demand + scheduled-only;
explicitly excluded from CI merge gates.

**AC11 — CLI wrapper exists at
`python -m kernos.kernel.self_test_gate`.** Codex round-2
confirmed this entry point does NOT currently exist
(`kernos/kernel/self_test_gate.py` ends at line 301 with no
`__main__` / `argparse` / `include_soak` references). This
spec adds the CLI as an explicit deliverable. Contract:
- `python -m kernos.kernel.self_test_gate --include-soak
  --json` runs the smoke gate + soak suite via the same
  `handle_run_self_test_suite(include_soak=True)` code path
  the kernel-tool dispatch uses.
- Exit code 0 on full pass; non-zero on any failure
  (smoke OR soak).
- `--json` flag emits machine-readable result for CI
  consumption; default is human-readable.
- Per-probe outcomes surface in the JSON output under
  `soak_results[probe_name]`.

Without this AC the "single canonical contract" design
principle is unenforceable — CI would have to re-implement
the soak path.

## Tracked follow-up specs (Codex round-1 Q8 commitment)

Codex round-1 explicit condition for accepting v1: "ship v1
first, but do not let it become the endpoint. Tests across
three ingresses can freeze the duplication instead of removing
it. v1 should be 'guardrail before surgery,' not 'tests
instead of surgery.'"

The two structural follow-ups below are explicitly tracked as
required successors. Their existence is part of v1's
ratification contract — if these aren't planned, v1 risks
becoming load-bearing for the duplication it was meant to
guard against until removal.

### DISPATCH-INGRESS-CONSOLIDATION-V1 (follow-up)

**Owner:** architect (Kernos), ratified by founder
**Exit criteria:**
- `_TOOL_ALIASES` is consulted in exactly ONE canonical
  pipeline; the parallel sites at reasoning.execute_tool +
  gate.classify_tool_effect + enactment.dispatcher all
  delegate to that single pipeline.
- Probe 4 reduces from "verify every ingress" to "verify the
  single canonical pipeline" — a complexity reduction in this
  spec, not just a relocation of the bug surface.
- Gate's sync-no-event constraint is either eliminated (the
  pipeline emits the receipt) or explicitly waived with
  rationale.

**Sequencing:** spec draft begins within 2 weeks of v1 merge.
v1's probes catch any new regressions during the consolidation
work; consolidation removes the duplication v1 was guarding.

### GATEWAY-HEALTH-UNIFICATION-V1 (follow-up)

**Owner:** architect (Kernos), ratified by founder
**Exit criteria:**
- The standalone `_discord_gateway_watchdog_loop` in server.py
  is migrated into the `GatewayHealthObserver` as a
  remediation action (matching the pattern in
  `specs/GATEWAY-HEALTH-OBSERVER-V1.md:88`).
- One ground-truth health pipeline produces both the
  diagnostic signal AND the remediation action; no parallel
  decision loops.
- Probe 6 reduces from "verify both detection layers" to
  "verify the single health pipeline."

**Sequencing:** spec draft begins within 4 weeks of v1 merge.
Lower priority than dispatch consolidation because the
duplication cost is smaller (two places vs three) and the v1
probe makes the duplication safe in the interim.

## Open questions for architect ratification

1. **Should soak failures abort bring-up?** v1 emits loud
   signal + activates autonomous-mutation gate but continues
   bring-up. Lean: emit-loud-don't-abort + autonomous gate
   for v1 (per Codex round-1 Q5 caveat). Reconsider once we
   have soak telemetry.
2. **Probe-kind extensibility — formal registration mechanism
   or hand-list?** v1 hand-lists the eight probes; formal
   registration in v2 if the suite grows past ~15 probes.
3. **Live-provider soak scheduling cadence — daily, weekly,
   on-demand only?** v1 ships the script + documents
   on-demand + nightly cron stub; actual cron activation is
   operator choice.

## Out of scope (deferred — beyond the tracked follow-ups)

- **LLM-driven probes.** Every v1 probe is deterministic. If
  later evidence shows we need LLM-driven probes for some
  invariants, that's a v2 extension.
- **Cross-instance soak.** v1 runs single-instance probes;
  multi-tenant soak invariants are out of scope.
- **Probe parallelization.** v1 runs probes serially for
  isolation; parallel execution is a v2 optimization if AC8's
  60-second budget becomes binding.

---

**Routing this spec:** drafted under founder green-light to
move from whack-a-mole bug-fixing to structural hardening.
Round-1 Codex review returned YELLOW with substantive folds;
v2 incorporated all 10 areas. Round-2 returned YELLOW with 3
small folds (restart_self exclusion rationale, CLI wrapper as
explicit AC11, stale "seven" → "eight" cleanup); this v3
incorporates all 3. Awaits Codex round-3 verification per
[[multi-round-codex-convergence]] then founder ratification
per [[push-approval-semantics]].

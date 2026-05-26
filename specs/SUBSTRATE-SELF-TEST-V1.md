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
round-trip soak fails, the commit doesn't merge. The seven bugs
we just shipped fixes for would all have been caught pre-merge
by these soaks (each one corresponds to a probe in the suite).

The soak suite is not a replacement for unit tests. It's an
additional layer that asserts *composition* — that the substrate
behaves correctly when its boundaries interact, not just when
each boundary is exercised in isolation.

### The seven probe categories

Each probe is a small, deterministic test that exercises one
substrate invariant the agent depends on for normal operation.
v1 ships these seven, named for the bugs that exposed them:

1. **Agent round-trip soak** — synthetic inbound message flows
   through handler / reasoning / integration / dispatcher with
   deterministic model + tool fakes. Behavioral signal in
   console + substrate state in dump match expectations.
2. **Self-knowledge invariant** — `read_source` can reach
   `specs/` and `docs/` paths, not just the kernos package.
3. **Consult drain invariant** — synthetic ACPX adapter emits a
   single line larger than 64 KiB; the dispatch completes
   without crashing the drain task.
4. **Dispatch canonicalization invariant** — a known
   hallucinated alias is repaired correctly at ALL THREE
   ingress points (reasoning.execute_tool, gate.classify_tool_
   effect, enactment.dispatcher) and emits the canonical receipt
   event from each.
5. **Retry-with-feedback invariant** — when an integration
   attempt fails validation, attempt N+1's prompt explicitly
   includes attempt N's failure reason. Three blind identical
   failures must not be possible.
6. **Gateway deafness invariant** — given healthy heartbeats AND
   total socket silence past the deaf window, both watchdog and
   observer must fire the DISCORD_GATEWAY_DEAF signal and
   escalate to restart.
7. **Approval loop invariant** — an improvement attempt reaches
   the approval-receipt boundary with full binding, exercised
   through the full agent / spec / consult path rather than a
   stub consult.

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

### Why these seven and not others

These are exactly the seven boundaries that surfaced as broken
in the last session. The principle going forward: every time a
new substrate boundary's failure surfaces in production, the
fix lands AND a probe gets added to this suite. The suite grows
empirically from operational evidence, not from upfront
prediction.

---

The remainder of this document is the technical spec the
implementation builds against.

---

**Date:** 2026-05-26
**Status:** Draft for review
**Scope:** New test layer at `tests/substrate_soak/` plus a
  smoke-gate extension in `kernos/kernel/self_test_gate.py` that
  runs the seven probes pre-merge AND as part of the
  post-bring-up health check. Seven probes shipping in v1, named
  for the boundaries that surfaced in the 2026-05-25 session.
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

This spec adds the missing layer. Seven probes that exercise
substrate composition at the boundaries that have proven
load-bearing for production correctness.

## Design principles (load-bearing)

- **Probes are deterministic.** No real model calls, no real
  ACPX subprocesses, no real Discord gateway. Hard fakes
  injected at well-defined seams. A probe failure points at
  Kernos's substrate, not at flaky external dependencies.
- **Each probe asserts substrate-fidelity per `docs/TESTING-
  PATTERN.md`** — behavioral signal AND substrate state, not
  just one. A response in console plus the matching event
  payload plus the matching SQLite row.
- **The soak suite runs pre-merge AND post-bring-up.** Same
  probes, two contexts. Pre-merge gates commits; post-bring-up
  fires loud if a fresh process can't pass its own contracts.
- **New boundaries grow the suite from operational evidence.**
  Every time a new substrate failure surfaces in production,
  the fix lands AND a probe gets added. The suite is a living
  registry of "things the substrate has demonstrably gotten
  wrong before; here's the contract that pins them."
- **Fail loud, fail attributable.** Per
  [[loud-fail-over-silent-degradation]] — when a probe fails,
  the failure message names the specific boundary that broke,
  cites the originating bug-fix commit if applicable, and
  surfaces enough state for operator triage without a debugger.

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
  seven probe modules.
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

## Probe inventory (v1 — seven)

Each probe lives in its own module under `tests/substrate_soak/`.
Each module exposes a single `async def run_probe(fixtures) ->
ProbeResult` function. ProbeResult carries `passed: bool`,
`probe_name: str`, `behavioral_evidence: dict`, `substrate_
evidence: dict`, `duration_ms: int`.

### Probe 1 — `agent_round_trip_soak.py`

**Asserts:** a synthetic inbound user message flows through the
full handler → reasoning → integration → dispatcher → response
chain with deterministic fakes, and produces both the expected
user-visible text AND the expected event-stream sequence.

**Fixture shape:** in-memory handler with stubbed model that
returns a pre-canned response; stubbed tool dispatcher that
returns a pre-canned result; ephemeral SQLite event store.

**Pass condition:** response text matches expected; event stream
contains `message.received` + `reasoning.request` +
`reasoning.response` + `tool.called` + `tool.result` +
`message.sent` events in order.

**Regression bug:** none specific — this is the umbrella probe
that catches integration regressions the other six can't see in
isolation.

### Probe 2 — `self_knowledge_invariant.py`

**Asserts:** `read_source(path="specs/SUBSTRATE-SELF-TEST-V1.md")`
returns this very file's contents (or any spec file shipped at
suite-run time); `read_source(path="docs/TECHNICAL-
ARCHITECTURE.md")` returns the doc contents; bare-path
`read_source(path="kernel/awareness.py")` still works for
back-compat.

**Pass condition:** all three calls return non-error content
with expected substrings; security check (`path="../etc/passwd"`)
still rejects.

**Regression bug:** `07226c8`. Kernos couldn't read its own
specs because `read_source` was scoped to `kernos/` only.

### Probe 3 — `consult_drain_invariant.py`

**Asserts:** a fake ACPX subprocess emits a single NDJSON line
of >64 KiB on stdout (mirroring claude-code's behavior when
asked to read a large file); the dispatch drain handles the line
without raising `LimitOverrunError` and surfaces the full
content to the caller.

**Fixture shape:** spawns a real subprocess that prints a
synthetic NDJSON event with an 80 KiB inlined payload, then
exits.

**Pass condition:** dispatch returns successfully with the full
inlined content intact; no `ConsultationFailed` raised.

**Regression bug:** `dbfbdab`. ACPX stdout drain crashed on
lines >64 KiB; surfaced as opaque `ConsultationFailed`.

### Probe 4 — `dispatch_canonicalization_invariant.py`

**Asserts:** a known hallucinated alias from the alias dict
(`planning_orchestration.create_plan` → `manage_plan`) is
canonicalized correctly at all three ingress points:
- `ReasoningService.execute_tool` repairs and dispatches
- `DispatchGate.classify_tool_effect` repairs and classifies
- `StepDispatcher.dispatch` repairs and reaches the descriptor

**Pass condition:** all three ingresses receive the alias, all
three reach the canonical tool, all three emit the
`tool.alias_repaired` event with `requested`, `canonical`, and
`context` fields populated correctly.

**Regression bug:** `f03e351` + `f8835e7`. Alias-repair was
wired into reasoning + gate but missed enactment dispatcher;
required a separate commit to close.

### Probe 5 — `retry_with_feedback_invariant.py`

**Asserts:** when an integration synthesis attempt fails
validation (intentionally invalid finalize block), attempt N+1's
prompt includes a `<prior_attempt_failures>` block naming the
specific component + reason that failed in attempt N.

**Pass condition:** the synthesis chain caller receives a prompt
on its second invocation that contains the failure reason from
its first invocation. Three identical failures must not be
possible — if attempt 1 fails with reason X, attempt 2's prompt
must include X.

**Regression bug:** `521c7f5`. Retry loop replayed identical
prompts; 3x identical `ProposeTool.reason` failures observed in
production.

### Probe 6 — `gateway_deafness_invariant.py`

**Asserts:** with a stub Discord client reporting healthy
latency AND `_last_any_socket_event_ts` backdated past the deaf
window, both `_is_gateway_heartbeat_unhealthy()` returns True
with the silence reason AND
`GatewayHealthObserver._detect_gateway_deaf()` returns a
`DISCORD_GATEWAY_DEAF` FrictionSignal with
`pattern=total_socket_silence` evidence.

**Pass condition:** watchdog flags unhealthy; observer emits
the signal; both name the silence-based failure mode
explicitly.

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

**Pass condition:** the receipt row exists in
`approval_receipts` table with the binding payload populated;
the receipt is retrievable via `get_receipt(approval_id=...)`.

**Difference from existing test:** the existing
`test_ac6_approval_receipt_issued_with_binding` test uses a
stub consult (`_make_converging_consult()`). This probe uses
the real ACPX adapter pointed at a fake claude-code subprocess
that returns canned content. It exercises the FULL agent /
spec / consult path the production loop will take.

**Regression bug:** none specific — this is the umbrella probe
that catches improvement-loop composition regressions.

## SubstrateSoakRunner

New helper in `kernos/kernel/self_test_gate.py`:

```python
class SubstrateSoakRunner:
    """Drives the seven substrate-soak probes with shared
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

`run_self_test_suite` (existing kernel tool) extends to accept
an optional `include_soak: bool = False` parameter (default
False for back-compat; CI flips it on). When True, the function
runs the existing 3-file smoke gate AND the
`SubstrateSoakRunner.run_all()`, reporting them under separate
result keys so soak failures don't get lost in the smoke
output.

## Post-bring-up hook

New step in `bring_up_substrate.py` after the existing
`LOOP_HEALTH_BOOT_PROBE_FIRED` hook: invoke
`SubstrateSoakRunner.run_all()` once. On pass, emit
`substrate.self_test_passed` event with per-probe durations.
On any-probe failure, emit `substrate.self_test_failed` event
with the failing probe names + evidence, AND log loud at WARNING
level. Does NOT abort bring-up — operator sees the loud signal
and decides whether to continue, restart, or roll back.

## Pre-merge integration

A new GitHub Actions workflow (`.github/workflows/substrate-
soak.yml`) runs `pytest tests/substrate_soak/ -v` on every
push to a branch with an open PR + on every merge to main.
Failure blocks merge.

For local pre-commit: `scripts/run_substrate_soak.sh` (new) is
a one-liner that runs the same suite locally. Optional
pre-commit hook (`pre-commit-config.yaml` entry, opt-in) wires
it.

## Acceptance criteria

**AC1 — Seven probe modules exist** under `tests/substrate_
soak/` with the names listed in the inventory section above.

**AC2 — Each probe asserts BOTH behavioral signal AND substrate
state.** Per-probe contract enforced via `ProbeResult.
behavioral_evidence` and `substrate_evidence` both being
non-empty dicts.

**AC3 — `SubstrateSoakRunner.run_all()` runs all seven serially
against fresh fixtures.** Test verifies isolation: a probe
failure doesn't contaminate the next probe.

**AC4 — `run_self_test_suite(include_soak=True)` runs the soak
suite alongside the existing smoke gate.** Separate result keys.

**AC5 — Post-bring-up hook emits `substrate.self_test_passed`
on full pass and `substrate.self_test_failed` on any failure**
with per-probe outcomes in the payload. Does NOT abort
bring-up.

**AC6 — Regression coverage.** Each of the seven 2026-05-25
fixes (`f03e351`, `07226c8`, `dbfbdab`, `5cec074`, `f8835e7`,
`521c7f5`, `a7302b0`) is exercised by at least one probe. Test
verifies the probes catch regressions by intentionally breaking
the relevant substrate code in a fork and observing the
matching probe fail.

**AC7 — GitHub Actions workflow gates merges.** PR with a
probe-failing branch cannot merge to main.

**AC8 — Local script (`scripts/run_substrate_soak.sh`) runs the
suite in under 30 seconds** end-to-end on a clean repo. Per
[[capability-first-posture]] — the suite has to be fast enough
that developers actually run it.

## Open questions for architect ratification

1. **Should soak failures abort bring-up?** v1 emits loud signal
   but continues bring-up. The argument for aborting: a deaf
   gateway probe failure on boot means the bot SHOULDN'T come up
   into a known-broken state. The argument against: aborting on
   a flaky probe could prevent operator from getting into the
   bot to debug. Lean: emit-loud + continue for v1; reconsider
   once we have soak telemetry.
2. **Are seven probes enough for v1, or should we identify more
   from existing tech debt before shipping?** Lean: ship the
   seven that map to the 2026-05-25 incidents; let the
   "grow-from-operational-evidence" principle add more.
3. **Probe-kind extensibility — do we need a registration
   mechanism now, or hand-list the seven in v1?** Lean:
   hand-list for v1, formal registration in v2 if the suite
   grows past ~15 probes.

## Out of scope (deferred)

- **Consolidating the three dispatch ingresses into one
  canonical pipeline.** Codex named this as the deeper
  architectural fix. v1 just asserts the invariant holds across
  all current ingresses; consolidation is a follow-up spec.
- **Unifying watchdog + observer into one health pipeline.**
  Same — v1 asserts both detect deafness correctly; unification
  is follow-up.
- **LLM-driven probes.** Every v1 probe is deterministic. If
  later evidence shows we need LLM-driven probes for some
  invariants, that's a v2 extension.
- **Cross-instance soak.** v1 runs single-instance probes;
  multi-tenant soak invariants are out of scope.

---

**Routing this spec:** drafted under founder green-light to
move from whack-a-mole bug-fixing to structural hardening.
Awaits Codex pressure-test then founder ratification per
[[multi-round-codex-convergence]] and
[[push-approval-semantics]].

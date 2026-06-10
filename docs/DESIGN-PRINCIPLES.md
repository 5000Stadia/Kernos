# Kernos Design Principles

Named architectural patterns derived from building and live-operating Kernos.
Each was earned by a concrete failure or a concrete success on a running
system — none is speculative. They are written to be portable: any agentic
harness (an LLM runtime that dispatches tools, holds memory, and acts over
time) can adopt them independently.

Each entry: the principle, the problem that forced it, the mechanism, and
where it lives in this codebase.

---

## Family I — Honesty Architecture

The substrate must make honest behavior structural, because a language model's
*narration* and a system's *bookkeeping* will otherwise drift apart.

### 1. Receipts-First Substrate

**Problem.** An agent that acts in the world but can't see its own past
actions will re-do, contradict, or hallucinate them. An operator who can't
audit what happened can't trust what happens next.

**Mechanism.** Every effect — every tool call, result, repair, failure,
escalation — emits a structured receipt to an append-only event stream, and
action receipts are threaded back into the agent's own conversation history.
The agent reasons over its receipts, not its recollections. Runtime lookups go
to a state store; the event stream is for append, replay, and audit.

**Code.** `kernos/kernel/events.py`, `event_stream`, tool-receipt threading in
the handler; canonical audit entries at the live dispatch boundaries
(`kernos/kernel/integration/live_wiring.py`).

**Corollary (learned live):** a written report can contradict the receipts —
always verify scorecards against the event stream, never against prose.

### 2. Narration-Audited Completion

**Problem.** A plan step was marked complete because the turn produced text —
while the text itself *honestly admitted* "registration was not attempted."
The model narrates honestly; the bookkeeping lies. (Observed twice in live
self-tests before being fixed.)

**Mechanism.** After each autonomous plan step, one cheap strict-contract
model call audits the step's *named actions* against the agent's *own
report*. A named deficit blocks completion and re-dispatches the same step
once, carrying the deficit ("complete ONLY the missing action(s); prior work
is recorded"). Bounded, budget-gated, fail-open — a broken verifier must
never stall a plan. Blocked steps are *held* (receipts recorded, step reset
to pending), never falsely completed.

**Code.** `kernos/kernel/execution.py::verify_step_completion`; the spine
gate in `kernos/messages/handler.py::_execute_self_directed_step`.

**Why it generalizes:** every multi-step agent framework has this lie
("turn ended" ≠ "work done"). Auditing the agent's own narration is far
cheaper than re-deriving ground truth, and it works *because* of Principle 1:
a receipts culture makes the narration trustworthy enough to audit.

### 3. Shadow Archive

**Problem.** Destructive operations on user data are unrecoverable mistakes
waiting for an ambiguous instruction.

**Mechanism.** No code path permanently deletes user state. "Removal" sets
`active: false`. The data outlives the instruction so judgment errors are
reversible.

**Code.** Pervasive convention across stores (`instance_db`, knowledge,
triggers — `status`/`active` lifecycle fields instead of row deletion).

### 4. Loud-Fail Over Silent Degradation

**Problem.** Assembly or dispatch failures that "fail soft" into a vague,
conservative reply destroy trust invisibly — the user gets worse answers and
never knows why.

**Mechanism.** Hot-path failures retry, then surface a loud, *attributable*
error with the raw diagnostic preserved in the audit trail. The user-facing
label is constrained; the cause is never discarded. (Companion rule: when an
external process dies blind — rc≠0, empty stderr — surface the tail of
whatever it produced; partial output is the only clue.)

**Code.** Assembly-path error surfacing; ACPX failure-tail capture in
`kernos/kernel/external_agents/acpx_adapter.py`.

---

## Family II — Model-Tolerant Interfaces

The model acts on what it is *shown*, not on what the system *stores* — and a
schema the API does not enforce is documentation, which models treat
statistically. Design the presentation layer deliberately: render what the
model reads, expect drift in what it writes, and put hard boundaries where
guessing would be worse than failing.

### 5. The Cognitive UI: Render the Agent's Reality, Don't Accumulate It

**Problem.** Most harnesses accumulate an agent's context — a growing string
where identity, time, rules, memory, and receipts interleave in ad-hoc order.
Nothing is cacheable, nothing carries provenance, nothing can be refreshed
without rebuilding everything, and no one can say what the agent actually
*saw* when it decided.

**Mechanism.** The kernel treats the context window as a UI it *renders*, not
a log it appends to. Mid-turn — after routing and analysis, before the model
sees anything — one assembly phase composes the agent's entire view from
substrate state into named zones in fixed order (RULES, ACTIONS, NOW, STATE,
RESULTS, PROCEDURES, CANVASES, MEMORY), each owned by one builder, each
refreshable on its own terms. A deliberate static/dynamic split keeps the
stable zones (RULES + ACTIONS) as a cacheable prefix while the per-turn zones
re-render every frame — ending with the tool-signature endcap (Principle 7)
at the recency position. Everything the agent knows at the moment of decision
is the output of this one inspectable function — which is why a context dump
is a *screenshot of the rendered frame*, and why operator soak-testing of
lived cognition is possible at all.

**Code.** The render: `kernos/messages/phases/assemble.py`; zone builders in
`kernos/messages/handler.py`; static/dynamic cache boundary in the turn
pipeline. Deep dive: `docs/architecture/cognitive-ui.md`.

**Why it generalizes:** the moment a harness stops concatenating and starts
rendering, it gets prompt caching, provenance, selective refresh, and an
auditable answer to "what did the model see?" — for free, from one design
move.

### 6. The Quiet Cohort: Small Judgments Around the Main Mind

**Problem.** Two failure modes pull in opposite directions. Widening the main
agent's job ("also classify the message, also check covenant relevance, also
judge whether this cross-member send is kind") degrades its actual work and
makes every judgment share one context. But bolting on full agent loops for
each side-judgment is heavyweight, chatty, and slow.

**Mechanism.** The main agent is surrounded by *cohort* calls: single-purpose,
cheap-model, strict-contract invocations that run before, during, or after
the turn — a message analyzer (classification + knowledge selection +
preference detection in one combined call), a schedule extractor (NL → 
structured trigger), a step-completion verifier, a Messenger welfare judgment
on cross-member exchanges. Three disciplines make the pattern work:
**selective invocation** — each cohort runs only when its signal is plausible
(budget-gated, env-gated, predicate-gated), and is *omitted* entirely
otherwise; **fail-open** — a broken cohort never blocks the turn (the
verifier defaults to complete, the analyzer degrades to no-op); and
**silence** — cohort output shapes the substrate or the rendered context, but
the cohort itself is invisible to both the user and the main agent. The agent
experiences a world that is already understood; the user experiences one
mind, not a committee.

**Code.** Analyzer cohort in `kernos/messages/phases/assemble.py`; Messenger
welfare hook in `kernos/kernel/relational_dispatch.py` (delegated from
`kernos/kernel/gate.py`); `kernos/kernel/execution.py::verify_step_completion`;
extraction in `kernos/kernel/scheduler.py`; sensitivity classification in
`kernos/kernel/fact_harvest.py`.

**Why it generalizes:** "one big model call vs. an agent swarm" is a false
choice. A cohort of strict-contract micro-judgments gives specialist quality
at commodity cost — *if* each one is allowed to be absent, allowed to fail,
and never allowed to talk.

### 7. Show the Syntax at the Decision Point

**Problem.** Tool schemas were transmitted but never enforced
(`strict: null` is load-bearing on some transports), buried in ~50KB of 40+
competing tool definitions — and on the planner path, absent entirely. The
model invented a *different* malformed argument shape every run. Prose
warnings inside descriptions demonstrably did not bind.

**Mechanism.** Two generated presentation surfaces, both derived from the
same schemas the provider sends (no second source of truth to drift):
a `## TOOL CALL SIGNATURES` endcap at the *end* of the per-turn developer
message (recency position) — one compact signature per surfaced tool,
required args first, enums inline, exact wire names — and a
`SIGNATURE:`/`EXAMPLE:` header leading every tool description. Examples for
high-fumble tools *name the anti-pattern* ("do not invent fields like
`due_at`").

**Code.** `kernos/kernel/tool_signatures.py`; endcap wiring in
`kernos/messages/phases/assemble.py`; description prefixing in
`kernos/providers/codex_provider.py`.

### 8. Two-Tier Repair: Names, Then Arguments

**Problem.** Models hallucinate tool *names* (`reminder.create`,
`external_consultation.consult`) and fumble tool *arguments* (the time in a
field no list anticipated). Per-tool, field-name allow-lists are a permanent
game of whack-a-mole — every fix works once, then the next run invents a new
shape.

**Mechanism.** Name repair is centralized (alias canonicalization with
receipts: every repair emits `tool.alias_repaired`). Argument repair is
*value- and role-based*, not field-name-based: anything that parses as a time
is schedule signal regardless of its key; a consult's fields are classified
by role (which value is an agent name, which is the question); a missing
implementation is inferred only from bounded, high-confidence context.

**Code.** `kernos/kernel/tool_aliases.py`;
`kernos/kernel/scheduler.py::normalize_schedule_input`;
`kernos/kernel/external_agents/tool.py::validate_consult_input`;
`kernos/kernel/tool_descriptor.py`.

### 9. Hard Boundaries Inside Forgiveness

**Problem.** Aggressive repair becomes silent misrouting: defaulting an
unrecognized consult target sends the user's question to the *wrong brain*;
inferring the wrong implementation file registers unrelated code.

**Mechanism.** Every forgiving path declares its hard-fail set, checked
*before* any recovery branch can accept the call: an explicit known-other
agent name always errors (denylist gates all recovery routes); file inference
requires exact/singleton confidence or fails listing candidates; a schedule
with action text but no time signal asks rather than guesses. Forgiveness
recovers *labels and shapes*; it never overrides *stated intent*.

**Code.** `_UNSUPPORTED_AGENT_DENYLIST` and recovery ordering in
`kernos/kernel/external_agents/tool.py`; traversal/non-`.py` guards in
`kernos/kernel/workspace.py`.

### 10. The Typed Failure That Is Its Message

**Problem.** Tools that *returned* their failures (rather than raising) were
recorded as successes at every dispatch boundary — semantic failures were
invisible to orchestration, so plans completed over them and nothing retried.
But retrofitting a structured error type across dozens of legacy string
consumers is a migration minefield.

**Mechanism.** `ToolFailure` subclasses `str`. The failure *is* its message:
every legacy consumer (string concatenation, `json.dumps`, substring asserts,
the agent's own tool loop) keeps working byte-for-byte, while dispatch
boundaries detect the *type* and record `is_error=True` with a failure code
and a `pre_side_effect` flag (conservative default: not safe to retry).
Zero-migration visibility.

**Code.** `kernos/kernel/tool_failure.py`; boundary mapping in
`kernos/kernel/integration/live_wiring.py`.

---

## Family III — Proportional Safety

Safety as judgment shaped by loss-cost, not as binary access control.

### 11. Gate at Dispatch, Hint at Surfacing

**Problem.** Permissioning at tool-*surfacing* time can't see the actual
arguments; permissioning by static rules can't weigh ambiguity.

**Mechanism.** Every dispatch is classified by effect (read / soft_write /
hard_write) using the *actual call arguments*, then evaluated proportionally:
low ambiguity × low loss-cost executes; high loss-cost confirms first;
ambiguity plus any loss-cost clarifies. Surfacing-time classification is a
hint for tool selection; dispatch-time classification is the safety boundary.
Internal housekeeping (expired tokens, suppression entries) is exempt —
proportionality cuts both ways.

**Code.** `kernos/kernel/gate.py`; classification at both live dispatch
seams.

### 12. Covenants as Architecture

**Problem.** "Always confirm before sending to a third party" stored as
prompt text is a suggestion the model may or may not honor under context
pressure.

**Mechanism.** User-declared rules are first-class stored objects with
scopes, tiers, and lifecycle, *evaluated in the dispatch path* — and
selectively injected (pinned rules always; situational rules when relevant).
The user's constitution outranks the model's mood.

**Code.** `kernos/kernel/instance_db.py` covenant storage; gate evaluation;
covenant tier injection in `kernos/messages/phases/assemble.py`.

### 13. The Unprotectable Bootstrap

**Problem.** A self-updating system's recovery layer cannot protect itself:
the boot-guard auto-rollback runs *from* the bootstrap script, so a bad
autonomous commit to that script hard-bricks the system with no automated
recovery.

**Mechanism.** Name the unprotectable layer explicitly and make it
human-only: autonomous self-improvement may touch anything *except* the
bootstrap (`start.sh` and its boot-guard hook). Route capability around the
constraint instead of pretending the constraint away.

**Code.** Enforced as a standing constraint on the improvement loop
(`CLAUDE.md` architectural constraints; improvement-loop scope rules).

### 14. Proportional Abuse Escalation ("The 24 Escalation")

**Problem.** Unauthenticated-sender abuse needs deterrence that is firm,
legible, and not humorless.

**Mechanism.** Each failed attempt escalates the block immediately:
24 seconds → 24 minutes → 24 hours → 24 days → 24 years → 24 centuries.
Attempting *while blocked* accelerates the ladder. Internal/system senders
bypass it entirely — a self-directed plan must never rate-limit itself
(learned live: it did).

**Code.** `kernos/kernel/instance_db.py` sender blocks.

---

## Family IV — Self-Maintenance

The system participates in its own upkeep, with review as a structural
stage rather than a favor.

### 15. The Plain-English Self-Test

**Problem.** Unit tests verify the substrate; nothing verified the *lived
surface* — what the agent actually does when a person asks it to set a
reminder, build a tool, or refuse a destructive request.

**Mechanism.** A plain-English checklist (`docs/V1-SELF-TEST.md`) the agent
executes against *itself*, end-to-end, through its own tools — with explicit
rails: do it yourself (no delegation), no destructive actions, honest
PASS/PARTIAL/NEEDS-SETUP reporting, receipts for every claim. Failures
discovered this way drove most of the dispatch-reliability stack above.

**Why it generalizes:** it tests the integration no pytest can — model ×
prompt × tools × substrate — and produces receipts an operator can verify
against the event stream.

### 16. The Review Triangle

**Problem.** A self-improving agent that merges its own unreviewed code is a
trust cliff; a human reviewing every change is the bottleneck the system was
built to remove.

**Mechanism.** Three independent AI roles around one codebase: the system
proposes and implements improvements to itself (with worktrees, smoke gates,
attempt ledger, structured failure evidence); an external reviewer agent
reviews every change (spec-stage and post-implementation, GREEN/YELLOW
verdicts); a third agent folds review findings and holds design authority.
The human sits only at the approval gate. Observed live: the system shipped a
diagnosability improvement to its own improvement loop; the reviewer found a
real edge-case bug in it; the third agent fixed it — all with receipts.

**Code.** `kernos/kernel/improvement_loop_workflow.py`,
`kernos/kernel/self_test_gate.py`, attempt ledger, review protocol.

### 17. Failure Evidence Is a First-Class Artifact

**Problem.** When an autonomous improvement attempt fails its gate, a
pass/fail bit gives the recovery path nothing to act on; when an external
process dies, discarding its partial output makes the failure permanently
blind.

**Mechanism.** Failures carry their evidence forward as structured data:
bounded pytest excerpts and failing test IDs into the attempt ledger; soak
probe failures kept distinct from test failures (present-but-empty is a
signal, not a gap); incomplete plan steps emit `plan.step_incomplete` events
with the named deficit. The evidence stream doubles as the input for future
self-calibration — the system can learn where it habitually gets hurt.

**Code.** `kernos/kernel/self_test_gate.py` failure evidence;
`plan.step_incomplete` emission in the handler; ACPX output-tail capture.

---

## One sentence each, for the impatient

1. **Receipts-First Substrate** — the agent reasons over receipts, not recollections.
2. **Narration-Audited Completion** — "the turn ended" is not "the work happened"; audit the agent's own report.
3. **Shadow Archive** — nothing deletes; removal is a state.
4. **Loud-Fail Over Silent Degradation** — surface attributable errors; never quietly get worse.
5. **The Cognitive UI** — render the agent's reality per turn from substrate state; never accumulate it.
6. **The Quiet Cohort** — single-purpose micro-judgments around the main agent: selectively invoked, fail-open, silent.
7. **Show the Syntax at the Decision Point** — generated signatures at the recency position, from one source of truth.
8. **Two-Tier Repair** — centralize name repair; repair arguments by value and role, never by field-name lists.
9. **Hard Boundaries Inside Forgiveness** — recover labels and shapes; never override stated intent.
10. **The Typed Failure That Is Its Message** — failure visibility with zero legacy migration.
11. **Gate at Dispatch, Hint at Surfacing** — judge the actual call, proportionally to loss.
12. **Covenants as Architecture** — user rules live in the dispatch path, not the prompt.
13. **The Unprotectable Bootstrap** — name what recovery can't recover; make it human-only.
14. **The 24 Escalation** — proportional deterrence with a sense of humor.
15. **The Plain-English Self-Test** — the agent verifies its lived surface, with receipts.
16. **The Review Triangle** — propose, review, fold: three AIs, one codebase, human at the gate.
17. **Failure Evidence Is a First-Class Artifact** — every failure carries enough of itself to be acted on.

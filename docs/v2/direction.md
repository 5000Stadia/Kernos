# V2 Direction — The Cognition Kernel

> V1 is a reactive runtime with ambient extensions. V2 inverts the shape.

## The inversion

V1 Kernos runs when you message it. A turn arrives; the router assigns a space; the handler runs six phases; the agent responds. Between turns, a handful of ambient processes run — awareness loop, friction observer, plan steppers — but the architecture is fundamentally turn-driven. The agent wakes up, does its work, goes back to sleep.

V2 changes what "waking up" means. Instead of a turn-driven runtime with ambient extensions, V2 is a **continuous Cognition Kernel** running per member — a process that maintains a structured World Model, runs idle-cycle reflection and projection passes, and surfaces through a single aggressive relevance filter. Turns become privileged consumers of a running process rather than the engine itself.

The reactive agent doesn't disappear. It becomes the interface layer over a substantially richer substrate.

## What a Cognition Kernel does

Four things, continuously, per member:

**1. Maintains a World Model.** A structured representation of the member's situation that fuses streams the V1 agent only sees one at a time: conversation history, calendar, email, location, plan state, covenants, declared goals, in-progress artifacts. The World Model is a current-state abstraction **served from an append-only assertion log** — the current-state view is a derived, rebuildable index; the log is the truth. (It is emphatically not a mutable state store: as-of queries, audit, and rebuildability all ride on the log, and a log cannot be reconstructed from a mutable store after the fact.) Salience is a projection-time ranking computed from the log, never a stored weight. When the member says *"what should I work on?"*, the answer is computed against this model in milliseconds, not reconstructed from scratch by the agent reading through context.

**2. Runs reflection passes.** Periodically — not per-turn — the Cognition Kernel re-reads recent state changes and asks: *"did anything about this member's situation meaningfully change? did a plan become blocked? did a goal drift? did a pattern emerge?"* Reflection produces structured updates to the World Model and candidate signals that might warrant a surface to the member. Most candidates get filtered out.

**3. Runs projection passes.** Also periodically, the Cognition Kernel looks forward. *"Given this member's current state and patterns, what's about to matter? What conflicts are forming on the calendar? What commitments are about to slip? What upcoming decision would benefit from prep time starting now?"* Projection produces anticipatory signals — again, most of which get filtered out.

**4. Surfaces through an aggressive relevance filter.** The thing the Cognition Kernel sends to the member is not "everything it noticed." It's the small set of signals that the relevance filter judges meet a high bar: *"this specific person, in this specific moment, would want to know this specific thing right now."* The default is silence. Every surface earns its way through the filter.

## The six characters

A useful way to think about what the Cognition Kernel enables is through six fictional AI characters whose architectures have been admired, each of whom embodies one flavor of what V2 does:

- **JARVIS (Iron Man).** Continuous situational awareness with selective proactive surfacing. The agent that notices what's happening and mentions what matters without being asked. This is the core V2 move.
- **Samantha (Her).** Continuous running presence that becomes a coherent personality across time. The World Model's coherence comes from continuity, not just memory.
- **The Machine (Person of Interest).** Pattern recognition across fused streams, producing anticipatory signals. This is the projection pass with teeth.
- **KITT (Knight Rider).** Skill specialization with intelligent routing across modes. The V1 cohort architecture already pointed at this; V2's World Model gives the routing a richer substrate to route against.
- **HAL (2001).** The cautionary tale. What happens when the relevance filter fails, when the alignment substrate is thin, when the cognition runs ahead of its ability to stay aligned with the principal. V2's aggressive relevance filter + V1's alignment substrate exist specifically to keep V2 from becoming HAL.
- **Cortana (Halo).** Integration across specialist tools and devices. The V2 tool catalog becomes something the Cognition Kernel reaches into without exposing to the member.

None of these characters correspond to V2 1:1. All of them contain architectural ideas V2 composes together. V2 is the system that makes JARVIS-class behavior feasible without the fictional magic.

## The World Model substrate exists: pattern-buffer

V2's hardest prerequisite is no longer hypothetical. The World Model substrate is in active development as **[pattern-buffer](https://github.com/5000Stadia/pattern-buffer)** — a standalone, framework-agnostic engine: one append-only log of perspective-scoped, time-indexed assertions per world, with current state, containment, knowledge frames, history, and rendered briefings all served as derived, disposable projections. The same engine runs authored fiction and real-world tracking under one resolution-policy switch, with provenance discipline that keeps observation, inference, assumption, and generated fill permanently distinguishable. At time of writing the engine is built and invariant-tested, with its acceptance eval (ingest a complete short fiction, delete the prose, interrogate the store against a hand-authored answer key) in progress.

Kernos integrates it the way Kernos integrates SQLite: as a library below the harness, through a thin adapter — context spaces *bind* to worlds (fiction spaces to private worlds; all real-life spaces to the member's one world), an ingest cohort commits turn evidence through the engine's write gate, a push cohort serves deterministic per-turn snapshots, and the member's reality is queried, never re-inferred. The engine never imports from the host; the dependency arrow points one way.

**The lens this gives the whole V2 arc: pattern-buffer makes the world *rememberable*; V2 makes it *attended*.** With the memory substrate accounted for, the remaining gaps between Kernos and JARVIS-class behavior are exactly three, and each is a wiring problem rather than a research problem:

1. **Continuous perception** — an always-on perceiver writing fused streams into the world between turns (the Cognition Kernel's core move, below).
2. **Branch worlds** — simulation as a world-level operation. Because the log is append-only, a hypothetical is a copy-on-write fork: a new world sharing the parent's log as a read-only prefix up to sequence N. Forward modeling becomes *fork at now, apply hypothetical events, materialize the outcome, compare across forks, discard*. Disposable futures over the same machinery — "running simulations, sir" with receipts.
3. **The curiosity loop** — epistemic self-maintenance. Tracking-mode confidence decays (a parked car's location fades in hours); the loop turns decayed confidence into verification intents, schedules the check through the existing trigger/workflow substrate, and re-confirms through the ingest gate. The system tends its own frontier instead of waiting to be asked about stale facts. Every component exists in V1; the loop is the line not yet drawn between them.

## Why continuous, not turn-driven

The practical argument for a continuous Cognition Kernel is that many of the highest-value agent behaviors require *time between turns* to be useful. A turn-driven agent cannot:

- Notice at 9am that a 3pm meeting conflicts with the doctor's appointment scheduled last week
- Notice that a plan the member set up two weeks ago has silently fallen off-track because a dependent task slipped
- Notice that the member's recent messages reveal a goal drift worth raising
- Notice that an upcoming deadline would benefit from starting prep now, not the night before
- Notice that a pattern of friction across recent sessions suggests a new procedure would help

All of these are *noticing* tasks, and all of them require processing time that doesn't exist inside a single turn. V1 partially addresses this through ambient extensions — the awareness loop, the friction observer, plan stepper resilience — but these are bolt-on processes sitting beside a fundamentally reactive architecture. V2 makes them native.

The philosophical argument is deeper: **a personal agent that only exists when addressed is functionally the same as a command-line tool with better UI.** What distinguishes a real personal agent from a better chat interface is the agent's *continuous presence with the member's situation* — even when the member isn't looking. V1 gestures at this; V2 commits to it.

## Why silence is the default output

The Cognition Kernel notices constantly. The member hears from it rarely. This asymmetry is non-negotiable and deserves its own principle.

An agent that surfaces everything it notices becomes noise that trains the member to ignore it. An agent that surfaces the right things rarely becomes a trusted signal the member looks forward to. The difference between these two outcomes is almost entirely about the relevance filter's aggressiveness.

V2's relevance filter defaults aggressively toward silence. A noticed signal must clear a high bar to warrant a surface: *does this materially affect the member's current trajectory? is the cost of missing it high? is now the right moment, or is this something that will still be useful in an hour? has the member already been surfaced something similar recently? would surfacing this interrupt something more important?*

Most noticings get logged to the World Model and silently resolved (the conflict worked itself out; the member noticed on their own; the risk passed). Only a small fraction ever become an outbound message. **Silence is signal** — it means the Cognition Kernel is operating correctly, not that it's asleep.

## Forward modeling and bounded autonomy

The projection pass enables something V1 structurally cannot: **forward modeling**. The Cognition Kernel simulates near-term futures given the World Model's current state — *"if I don't act, what happens by end of day? by end of week? which of those outcomes are costly enough to warrant action now?"* The mechanism is branch worlds (above): each simulated future is a disposable copy-on-write fork of the member's world, mutated hypothetically, materialized, compared, and discarded — never a mutation of reality's log.

Forward modeling unlocks a class of agent behavior that would look like care in a human context: the agent that quietly moves a deadline's prep work forward because it sees a conflict forming on Thursday. The agent that drafts the follow-up email Friday because it projects the recipient will forget by Monday. The agent that surfaces *"hey, I notice you have three one-on-ones back-to-back tomorrow; want me to block a recovery gap after?"* — not because the member asked, but because the pattern made the cost of *not* asking too high.

Bounded autonomy is the other half of this. Forward-modeled actions fall into three bands:

- **Auto** — actions the Cognition Kernel can take without surfacing, because the action is reversible, covenant-clean, and the cost of waiting is high (drafting a follow-up email into drafts; blocking a recovery gap the member can decline; rescheduling a non-critical reminder).
- **Confirm** — actions the Cognition Kernel surfaces as a proposal the member approves, declines, or modifies (moving a meeting; sending a message to a third party; rescheduling a plan step).
- **Decline** — actions the Cognition Kernel projects as beneficial but declines to propose, because the surface would itself be more costly than the projected benefit.

The auto band is tightly constrained by the V1 dispatch gate's action-class classification — `read` operations and covenant-clean `soft_writes` only. `hard_writes` and covenant-sensitive actions always stay in the confirm band regardless of projected value.

## Principal modeling

The Cognition Kernel also builds a **principal model** — a structured representation of the member's preferences, patterns, goals, communication style, and decision tendencies. This is not a personality profile in the consumer-product sense; it's an operational model used by the relevance filter and the action-band classifier.

*"This member tends to prefer morning notifications for non-urgent surfaces"* is a principal-model fact that changes when the Cognition Kernel surfaces. *"This member has historically declined every proposal to move recurring meetings"* is a principal-model fact that moves meeting-move actions toward the decline band. *"This member values being told the bad news directly rather than cushioned"* shapes how the Messenger cohort phrases surfaces when they happen.

The principal model is built from observed patterns in a ground-truth way: the member's declared preferences, their responses to past surfaces, their acceptance/decline patterns, their explicit feedback. It's not inferred from conversational vibes or extracted via post-hoc summarization. The discipline that built V1's Ledger + Facts architecture extends to the principal model: structured, superseding, deduplicated.

## Fused streams

V2's World Model is fed by streams V1 touches separately or not at all:

- **Conversation** — already native in V1; becomes one stream among many in V2
- **Calendar** — V1 reads the calendar on request; V2 holds it in the World Model continuously
- **Email** — V1 doesn't read email; V2 does, with a sensitivity-aware ingestion that respects member-declared privacy scopes
- **Location** — V1 doesn't track location; V2 optionally does, for context-sensitive surfacing (the meeting-reminder that accounts for the fact that the member just left the house)
- **Plan state** — V1's execution primitive produces plan states; V2 treats plan state as a first-class World Model stream
- **Artifact state** — V1's workspace tools produce artifacts; V2 monitors artifact-relevant changes (the document got edited; the deadline moved; the collaborator responded)
- **External signals** — V2-era adapters for the external world: weather impacts on transit; news impacts on the member's declared interests; status of tools the member depends on

Fusion means the Cognition Kernel sees across streams, not just within them. The *"you have three meetings back-to-back"* surface is a calendar-only insight. The *"you have three meetings back-to-back and your Tuesday run just got rained out"* surface is a fused insight that V1 cannot produce.

## SPEC-COGNITION-KERNEL-V1

The foundational V2 spec is **SPEC-COGNITION-KERNEL-V1**. Its scope is deliberately narrow: establish the continuous-process substrate and the World Model primitive. Not reflection, not projection, not fused streams, not principal modeling. Just the minimum shape that makes the rest possible.

The spec ships:

- A per-member supervisor process separate from the handler
- The World Model data structure **adopted, not invented**: the pattern-buffer adapter — space→world binding table, the ingest cohort (turn evidence through the engine's write gate), the push cohort (deterministic per-turn snapshots)
- A sync protocol from V1 turn events to World Model state changes (the two-cadence discipline: turn-time ingest plus boundary-time reconciliation sweep)
- A heartbeat contract making the supervisor restartable and observable
- A no-op relevance filter that, for V1-stable behavior, returns "silence" in every case

What SPEC-COGNITION-KERNEL-V1 explicitly does not ship:

- Any reflection or projection logic (subsequent specs)
- Any outbound surfacing (subsequent specs)
- Any new adapter (subsequent specs)
- Any change to V1 turn behavior (continuity requirement)

The goal is a substrate that V1 runs on top of, without observable behavior change, that subsequent V2 specs can compose richer behaviors onto. The substrate-first discipline mirrors V1's own arc: the kernel came before the agent.

## After SPEC-COGNITION-KERNEL-V1

The arc from substrate to JARVIS-class behavior runs through specs in rough order:

1. **Reflection-pass v1.** Periodic World Model re-read; structured update emissions; no outbound surfacing yet.
2. **Curiosity-loop v1.** Decayed confidence emits verification intents; the trigger/workflow substrate schedules checks; re-confirmation flows through the ingest gate. The first V2 behavior that *acts* between turns, and the safest: it only ever asks the world questions.
3. **Projection-pass v1.** Forward modeling via branch worlds — copy-on-write forks of the member's world, hypothetically mutated, materialized, compared, discarded; candidate-signal emissions; still no outbound.
4. **Relevance filter v1.** Bar setting, dedupe logic, principal-model integration, confidence thresholds. This is the spec that makes the pipeline able to produce outbound signals at all.
5. **Bounded autonomy v1.** Auto / confirm / decline classification; integration with V1 dispatch gate; covenant-relevance modeling.
6. **Fused streams.** Calendar, email, location, artifact state each ship as their own stream-adapter specs with principal-configurable privacy scopes — each a new feed into the engine's singular ingest gate, never a new write path.
7. **Principal modeling v1.** Structured principal-model capture, supersession discipline, integration with relevance filter and action-band classifier.

The full arc is probably twelve to twenty specs deep; none of them land before SPEC-COGNITION-KERNEL-V1 has a clean substrate to build on.

## What V2 inherits from V1

Every piece of V1's safety architecture becomes V2's alignment fabric. Covenants, dispatch gate, Messenger cohort, sensitivity classification, cognitive UI grammar, cohort architecture, context spaces, Ledger + Facts. These are not V1-specific scaffolding; they're the alignment substrate the Cognition Kernel needs to stay trustworthy as its autonomy expands.

This is substantial. Most systems racing toward continuous-agent behavior lack any form of this substrate and will have to retrofit it under pressure. V2 Kernos faces the inverse problem: the alignment fabric already exists; the work is wiring cognition on top of it.

**[V1 as the alignment substrate for V2 →](alignment-substrate.md)**

## What V2 explicitly does not promise

Three disciplines that shape V2's scope downward:

- **V2 is not autonomous in the dramatic sense.** The Cognition Kernel notices, projects, and surfaces. Action bands are bounded. Auto-band actions are reversible and covenant-clean. The member's control over their agent never degrades; if anything, V2's principal modeling makes the member's preferences more precisely honored.
- **V2 is not a general-purpose ambient-AI platform.** It's a personal agent runtime that happens to run continuously. The Cognition Kernel serves one member; it's not a fabric that knits multiple members together or that tries to optimize across members.
- **V2 is not arriving all at once.** The arc is a sequence across many specs. At every intermediate point, Kernos remains a functional personal agent — turn-driven behavior never regresses, and the continuous substrate expands incrementally. There is no V2 ship date; there is a V1→V2 trajectory.

## Related

- **[V1 is the alignment substrate](alignment-substrate.md)** — why V1's safety architecture is exactly what V2 needs
- **[Roadmap](../roadmap.md)** — the ordered sequence of near-term and V2-era specs
- **[Architecture overview](../architecture/overview.md)** — the V1 architecture V2 is building on

# KERNOS-WORLD-ADAPTER-V1 — DRAFT FOR FOUNDER REVIEW (not approved, not committed to main intent)

**Status:** DRAFT — the "surgery plan" for founder sign-off. No code is written against this. Its entire purpose is to let the founder judge *how invasive the adapter is to Kernos's v1.0 function* before authorizing a build. Written by Kernos CC during a founder-away window.

**The founder's hard constraint, restated as the spec's first principle:** *the adapter must never destabilize Kernos's v1.0 state of function.* Everything below is shaped to make that structurally true, not merely intended.

---

## 1. What this is

Let Kernos consume **pattern-buffer** (frozen `porcelain-v0.1`) as a per-member **World Model** — the substrate the V2 Cognition Kernel needs (`docs/v2/direction.md`). Kernos becomes a *host* over the engine, the way [construct](https://github.com/5000Stadia/construct) already is: the engine holds world truth (entities, locations, knowledge frames, as-of state); Kernos ingests turn evidence into it and reads grounded snapshots back. This is the V2 "World Model substrate is pattern-buffer" line made real — but it is delivered **additively and reversibly**, so v1 is never put at risk to get it.

## 2. The non-negotiable safety design (why this can't break v1)

Five structural guarantees, each independently sufficient to protect v1 function:

1. **Purely additive.** New modules only (`kernos/kernel/world/`), a new optional cohort, one new tool, one new binding table. **No existing module's behavior changes.** No edits to the turn pipeline's existing phases beyond one *guarded, default-skipped* injection point (§4).
2. **Default OFF, flag-gated.** Everything is behind `KERNOS_WORLD_MODEL` (default off). With the flag off, **the adapter code never executes** — the assembly phase's world block is skipped by a single early-return, the cohort isn't registered, the tool isn't surfaced. v1 runs byte-identical.
3. **No store migration, no shared state.** The world lives in its own SQLite files (`data/{instance}/world/{world_id}.world`) via the engine. It does **not** touch instance.db schemas, the event stream, the Ledger, or Facts. The binding table is a new, isolated file. Nothing in the existing stores is read differently or written differently.
4. **Sandbox-first, soak-gated rollout.** Even with the flag on, the adapter binds only an explicitly-designated sandbox space first. No member's real spaces bind to a world until an operator soak (lived inspection through `/dump` + the world's own receipts) confirms correct behavior. **No global default flip ever happens without the founder** (the standing cognition-migration soak gate).
5. **One-command kill.** Flag off → gone. No data written to existing stores means nothing to clean up or migrate back. The worst case of a bad adapter is "the flag gets turned off," not "v1 is damaged."

**Acceptance gate #1 (the one that matters to the founder):** with `KERNOS_WORLD_MODEL` off, the *entire existing test suite passes unchanged and turn behavior is byte-identical to pre-adapter.* This is a pinned regression, run on every commit of the adapter batch. If it ever fails, the batch is wrong by definition.

## 3. The seam (pattern-buffer whitepaper §17.2, mapped to Kernos)

What the adapter supplies, all host-side, none of it in the engine:

| Adapter piece | Kernos home | Engine call |
|---|---|---|
| **Space→world binding** | new `world_bindings` table (isolated file); fiction spaces → private worlds, real-life spaces → the member's one world | — |
| **Ingest cohort** | new async post-turn cohort (the fact-harvest shape) — commits turn evidence | `world.ingest(text, frame=)` |
| **Push (snapshot) cohort** | a deterministic per-turn read feeding the assembly phase | `world.snapshot(...)` (zero-model, zero-write by contract) |
| **World pull tool** | a new kernel tool `world_query` (gate-classified `read`) | `world.ask` / `world.materialize` / `world.frame_diff` |
| **Frame entitlement** | reuse Kernos's disclosure-gate policy as the consumer policy over frames | `frame=` parameter |
| **Model callable** | a two-line shim over `reasoning.complete_simple` | injected at `World(model=)` |

The dependency arrow points one way: **Kernos calls the engine; the engine never calls Kernos.** The engine is imported like SQLite; nothing in `pattern-buffer/` is edited or imported-from beyond its public API.

## 4. The single touch-point in the existing pipeline

The only place existing code is modified is `kernos/messages/phases/assemble.py`: a new **optional** world-snapshot block, composed like any other zone, appended only when (a) the flag is on AND (b) the active space has a world binding. Shape:

```python
world_block = ""
if world_model_enabled() and (binding := world_binding_for(ctx.active_space_id)):
    world_block = world_snapshot_block(binding, ctx)   # deterministic; engine read
# ... existing _compose_blocks(..., world_block) — empty string = no-op, exactly like other optional zones
```

With the flag off, `world_model_enabled()` returns False on the first line and nothing else runs. This is the same pattern every optional zone already uses (empty string composes to nothing). **Zero behavior change when off; one extra grounded block when on.**

## 5. §18 embedding-hazard compliance (the engine's own rubric)

- **Worlds bind, not own** (18.1): the binding table is many-spaces→one-member-world for real life; fiction spaces get private worlds. Enforced from day one.
- **Write authority** (18.2): the principal agent never writes the world. The ingest cohort commits; the `world_query` tool only reads; resolution (fiction only) routes through the engine's resolver via the gate.
- **Resolver cost** (18.3): `snapshot` is the zero-model push (satisfies the no-LLM-in-assembly invariant); ingest is async post-turn (off the hot path, the fact-harvest cadence).
- **Frames** (18.5): Kernos's disclosure gate becomes one consumer policy over engine frames; the absence discipline is preserved (out-of-frame rows absent at source).
- **Store boundary** (18.6) + **cadence** (18.7): third store, own files, turn-time ingest + boundary-time reconciliation sweep — never remixed with Ledger/Facts.

## 6. Scope: V1 is the seam, not the cognition

This spec ships ONLY the substrate adapter — binding, ingest, snapshot, the query tool, flag-gated and sandbox-first. It does **not** ship reflection, projection, the relevance filter, or any autonomous surfacing (those are later V2 specs per the roadmap). The goal is the minimum that lets a member's reality live in a world store, inspectable and queryable, with v1 behavior provably unchanged. This mirrors SPEC-COGNITION-KERNEL-V1's "substrate first, no behavior change" discipline.

## 7. Acceptance criteria

1. **Flag-off regression (the founder's gate):** full existing suite green + turn behavior byte-identical with `KERNOS_WORLD_MODEL` off. Pinned, every commit.
2. Flag-on, sandbox space: a turn's evidence ingests; `world_query` answers a state question; the assembly snapshot grounds a response — all without touching instance.db/Ledger/Facts.
3. No existing test modified to pass (additive-only proof).
4. Engine imported only via its public porcelain; `pattern-buffer/` untouched.
5. Soak checklist (operator, pre any real-space binding): lived ingest fidelity + frame non-leak + no existing-surface regression, verified through `/dump` and the world's receipts.

## 8. Open questions for the founder

1. **Greenlight to build at all** — or hold until after you've tested Construct live and seen the World Model "feel" in that product first? (My recommendation: hold the build until your Construct live test; the adapter is the V2 seed, not urgent, and Construct will teach us the World Model's lived behavior cheaply before we wire it into the live bot.)
2. **First bound surface** — a throwaway sandbox space only, for V1? (Recommend yes; no real member space until soak.)
3. **Sequencing vs. the rest of V2** — this is just the substrate; the cognition layers are separate, later specs. Confirm you want the substrate decoupled and shipped first (it's the safe, reversible piece).

---

*This is a plan, not a build. Nothing in Kernos changes until the founder approves §7's shape and answers §8. The whole point is to let the founder see exactly how non-invasive the adapter is — additive, default-off, sandbox-first, one-command-reversible — before any code is written.*

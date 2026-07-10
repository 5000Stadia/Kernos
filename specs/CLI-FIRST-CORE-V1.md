# CLI-FIRST-CORE-V1 — platform-neutral server core + CLI-first onboarding

**Status:** DRAFT r2 — Cx spec-review round 1 (YELLOW) folded; awaiting re-review
GREEN, then build. **Origin:** founder directive 2026-07-10 — "the Discord
connection should not be required to use KERNOS." Coupling audit: kernel clean
(zero `import discord` outside `adapters/discord_bot.py` + `server.py`); the
remaining coupling is the chassis (`server.py` event loop, owner bootstrap).

**r2 changelog (Cx round-1 blocking amendments, all folded):** (1) boot-guard
promotion moved behind an explicit **readiness barrier** (core AND every
configured startup-critical adapter) so a Discord-breaking commit can never be
promoted to last-known-good; (2) supervised **CoreRuntime** lifetime with owned
shutdown + once-only construction guard (Discord `on_ready` recurs on
reconnect); (3) the sync 429 wrapper is NOT reusable around `client.start` — an
**async equivalent** with its own tests; "byte-identical" weakened to
*behaviorally equivalent with explicit lifecycle pins*; (4) explicit
**DiscordRuntime** extraction boundary with a block-by-block inventory
(flusher/watchdog are Discord-only; scheduled update/restart gains a
platform-neutral activity source); (5) `cli:<hostname-user>` replaced by a
**persisted instance-id resolver** (env → adopt unambiguous legacy → refuse on
ambiguity → fresh generates + persists); (6) Telegram-only acceptance
strengthened to **message→handler→reply** with a bound owner.

## Intent

1. **Kernos runs with zero platform adapters.** One LLM key → talk on the CLI.
2. **Every adapter is optional and token-gated** — Discord becomes what
   Telegram already is (`if token:` → register + start), losing chassis status.
3. **First run is a guided product moment** — connect an LLM, meet your agent,
   then *optionally* connect Telegram/Discord/Twilio (the blueprint's
   capability-first nudge), never demanded.

## Non-negotiable constraints

- **`start.sh` untouched.** It invokes `python kernos/server.py`; that entry
  point keeps working, including the adapterless case (the supervisor keeps the
  process alive — see A2).
- **Boot-guard auto-rollback contract preserved via the readiness barrier
  (A5).** This is the hard-brick protection; it must be *stronger* after the
  extraction, never weaker.
- **Behaviorally equivalent Discord operation when `DISCORD_BOT_TOKEN` is set**,
  pinned by explicit lifecycle tests (connect, watchdog arm, presence, command
  tree sync, reconnect path, 429 backoff schedule + operator text). Not claimed
  as byte-identical — the run loop changes shape (A3).
- **Adapter/handler isolation preserved** (grep-verified).
- **Boot symmetry:** `repl.py` re-bases onto the shared core construction via
  options, preserving its intentional omissions (no pollers, dev data dir)
  unless this spec names a change.
- No DECISIONS.md status edits.

## Phase A — platform-neutral core extraction

**A1. CoreRuntime.** New `kernos/server_core.py` exposing a
`build_core_runtime(...) -> CoreRuntime` factory. `CoreRuntime` owns: event
stream writer, MCP manager, state store + instance DB, handler, scheduler,
awareness evaluators, poller registry, and **every background task it starts**
(task list held on the object). It provides `close()` — cancel owned tasks,
stop pollers, disconnect MCP, flush/stop event writer, close state/instance DB
— and is **constructed exactly once per process**: the Discord `on_ready`
re-entry (reconnect) hits a construction guard and only re-runs the
Discord-facing rewiring, exactly like today's semantics but explicit.
`repl.py`'s `build_dev_handler` becomes a thin wrapper over the same factory
(public seam + test contract preserved).

**A2. Supervisor main.** `server.py.__main__`: load env → `build_core_runtime`
→ construct each adapter whose credentials exist → supervise. The supervisor:
- awaits the set of adapter lifetimes; with **zero adapters it awaits a
  shutdown event** (the adapterless daemon stays alive under start.sh),
- installs SIGINT/SIGTERM handlers that trigger orderly shutdown
  (`CoreRuntime.close()` + adapter closes),
- propagates adapter-task exceptions to the supervisor (a crashed adapter is
  loud, not silently dropped).

**A3. DiscordRuntime.** All Discord surface moves behind a
`DiscordRuntime` factory/module: client + intents, command tree + decorators,
slash/command handlers, gateway watchdog, deferred-delivery flusher,
gateway-health provider wiring, presence, tree sync, and the **async 429
smart-backoff** — a new `await client.start()`-based equivalent of
`_run_with_429_smart_backoff` (asyncio.sleep, same schedule and operator
text, explicit close/cancel on abort, exception propagation to the
supervisor), **with its own tests** (the sync wrapper's tests are not evidence
for the new lifecycle). No Discord token → the module is never imported
(module-level `import discord` leaves server.py).

**on_ready block inventory** (the extraction boundary, per current server.py):

| Block | Classification |
|---|---|
| event-stream writer, state/instance DB, MCP manager, handler, scheduler, awareness | core (A1) |
| boot_guard mark-ok + rollback-notice surfacing | production lifecycle — moves to supervisor, gated by the A5 barrier |
| improvement-loop orphan reconcile | production lifecycle — supervisor, after core ready |
| Telegram/Twilio poller start | core poller registry (token-gated, as today) |
| gateway watchdog, deferred-delivery flusher, presence, tree sync, command handlers | Discord-only (A3) |
| scheduled update/restart quiet-window check (currently reads Discord inbound timestamps) | core, with an **injected activity source** — each registered adapter contributes last-inbound; Discord supplies its timestamps when present; adapterless falls back to handler-level last-turn time |
| gateway-health provider injection | Discord-only; the injection point must tolerate absence (no provider registered → no observer) |

**A4. Instance-identity resolver** (replaces the r1 `cli:<hostname-user>`
rule, which collides across clones and ignores legacy keying). One resolver,
used by server and repl alike:
1. `KERNOS_INSTANCE_ID` env — explicit wins (as today).
2. Else inspect the data dir for persisted instance identity; **adopt an
   unambiguous single legacy instance ID** (rows may be `discord:*`, phone, or
   explicit — all valid).
3. **Ambiguity (multiple candidate instance IDs) → refuse with the candidates
   listed** and the env-var instruction. Never guess.
4. Fresh data dir → generate one collision-resistant ID (uuid-suffixed
   platform-neutral shape) and **persist it atomically**; subsequent boots
   read the persisted value. Identity is never re-derived from mutable
   hostname/user state per boot.
**Pin:** an existing no-env Discord install loads the same tenant after the
change (regression test on a seeded legacy data dir).

**A5. Boot-guard readiness barrier.** `mark_boot_ok` fires only when: **core
runtime ready AND every configured startup-critical adapter has reached its
ready signal** (Discord = `on_ready`; Telegram/Twilio = poller started and
first poll issued). Zero configured adapters → barrier is core-readiness
alone. Rollback-notice *consumption/surfacing* may run once state/handler
exist (it only reads + messages), but **promotion to last-known-good uses the
barrier**. This preserves the exact protective property of today's placement:
a commit that breaks a configured adapter is never promoted.
**Pins:** (a) configured-Discord-fails → no mark_boot_ok (rollback fires on
next boot per existing machinery); (b) Telegram-only success → marks;
(c) zero-adapter success → marks.

**Acceptance A (slice A = all of Phase A, nothing from B):**
- `.env` with ONLY `TELEGRAM_BOT_TOKEN` + LLM key **and an already-bound
  owner member-channel**: a Telegram message from the owner reaches the
  handler and a reply is delivered (message→handler→reply, not merely
  poller-start). Fresh-sender rejection unchanged (`_resolve_incoming`
  semantics; fresh owner bootstrap is Phase B).
- `.env` with only an LLM key: adapterless daemon boots under
  `python kernos/server.py`, stays alive, marks boot-ok (barrier c), REPL
  chat works against the same core recipe, SIGTERM shuts down cleanly.
- `.env` with Discord token: lifecycle pins (connect, watchdog, presence,
  tree sync, reconnect re-entry guard, 429 schedule text) green; A5 pin (a).
- Legacy-tenant pin (A4). Full suite green. Import-isolation grep clean.
- `no Discord token → discord package never imported` (assert via
  sys.modules in a seam test).

## Phase B — CLI-first onboarding (assembling the built pieces)

**B0. Canonical terminal namespace decision.** Production CLI channel value is
**`cli`** (`channels.py`, `chat.py`, NormalizedMessage already use it).
`repl.py`'s deliberate `repl` platform remains a *dev* namespace; B1's guided
chat runs on `cli`. Compatibility rule: existing `repl:*` member channels stay
valid dev artifacts; no migration of dev data; member-channel lookup treats
them as distinct channels (as today). The spec names this explicitly so the
cli-vs-repl split is a decision, not an accident.

**B1. First-run detection + guided flow.** `kernos` entry with no configured
LLM key:
1. **LLM connect** — reuse `kernos/setup/console.py` (`kernos setup llm`):
   getpass key entry, `.env` write, verified ping.
2. **Meet your agent** — CLI chat over the shared core on the `cli` channel.
   Owner bootstrap per B2; hatching proceeds organically.
3. **Optional platforms — deterministic transition:** the platform offer
   surfaces (a) always via explicit command (`kernos setup platforms` /
   in-chat "connect telegram"), and (b) proactively exactly once, at the
   first turn boundary where the owner's member profile reads
   `hatched=true` (the concrete post-hatch condition checked after each
   turn; no "settles" heuristic). Wizard per platform: BotFather/portal
   walkthrough → token via getpass → `.env` write → poller hot-start or
   restart note. Declining is a first-class ending.

**B2. Owner bootstrap on CLI — reuse, don't fork.** Local machine possession =
owner auth (trusted-local CLI boundary). Implementation **reuses the existing
`ensure_owner`/`get_member_by_channel` mechanics already exercised by
`repl.py:219-261`** — no second bootstrap path. **Pin:** an existing owner
(e.g. Discord-bootstrapped) gains a `cli` channel on their existing member
profile — one owner, one more channel, no duplicate owner, no tenant split.

**B3. README follows** once B1 lands (`kernos` → guided everything). Not before.

**Acceptance B:**
- Fresh clone, no `.env`: guided LLM connect → chat → hatching completes on
  `cli` → platform offer fires once at the hatched-condition boundary →
  declined → subsequent runs go straight to chat with memory intact.
- Same flow accepting Telegram: token wizard → owner messages the bot → same
  member, same memory, both channels on one profile (B2 pin).
- Discord-hardcoded bootstrap path untouched for existing installs.

## Out of scope

Multi-member CLI onboarding; enterprise/phone-verification flows; any
`start.sh` modification; new adapters; MCP-server exposure of Kernos itself.

## Sequencing

Slice A ships alone (Cx-recommended cut: CoreRuntime + readiness barrier +
supervision + DiscordRuntime + Telegram-existing-owner + adapterless-daemon
acceptance; `build_dev_handler` rebased; the polished `kernos` command, fresh
CLI onboarding, hatching UX, and platform wizard all deferred to B). Cx
re-review of this r2 before code; post-impl review per phase; live-verify A
against the founder's Telegram case and B against a scratch data-dir first run.

# CLI-FIRST-CORE-V1 — platform-neutral server core + CLI-first onboarding

**Status:** DRAFT for review (Cx spec pass, then build on GREEN + founder go).
**Origin:** founder directive 2026-07-10 — "The Discord connection should not be
required to use KERNOS." Coupling audit confirmed the kernel is clean (zero
`import discord` outside `adapters/discord_bot.py` + `server.py`; the
adapter/handler isolation constraint held). What remains Discord-coupled is the
*chassis*: `server.py`'s event loop is `discord.Client.run()`, and owner
bootstrap is Discord-hardcoded. This spec removes both, and assembles the
already-built onboarding pieces (`kernos setup llm`, `kernos setup`,
`credentials_cli`, `repl.py`, the blueprint's onboarding nudge) into a
CLI-first front door.

## Intent

1. **Kernos runs with zero platform adapters.** One LLM key → talk on the CLI.
2. **Every adapter is optional and token-gated** — Discord becomes what
   Telegram already is in `server.py` (`if token:` → register + start), losing
   its chassis status.
3. **First run is a guided product moment** — connect an LLM, meet your agent,
   then *optionally* connect Telegram/Discord/Twilio, offered capability-first
   (the blueprint's onboarding nudge), never demanded.

## Non-negotiable constraints

- **`start.sh` untouched.** It invokes `python kernos/server.py`; that entry
  point and its boot_guard pre-launch interplay must keep working byte-for-byte.
  The extraction happens *inside* — `server.py` remains the file start.sh runs.
- **Byte-identical Discord behavior when `DISCORD_BOT_TOKEN` is set** (the
  production bot must not notice this change). Gateway watchdog, presence,
  slash commands, reconnect/backoff — all arm exactly as today, but only when
  the Discord adapter is active.
- **Adapter/handler isolation preserved** (grep-verified, as always).
- **Boot symmetry with `repl.py` maintained** — the extracted core is the
  shared recipe both entry points use (repl.py's docstring contract becomes
  literal shared code instead of a mirrored copy).
- No DECISIONS.md status edits (owner/design-review own that).

## Phase A — platform-neutral core extraction

**A1. Extract the boot recipe.** New `kernos/server_core.py` owning the
platform-neutral boot currently living in `on_ready`: event-stream writer, MCP
manager, state store, handler construction, scheduler, awareness evaluators,
boot_guard `mark_boot_ok` + rollback-notice surfacing, improvement-loop orphan
reconcile, and poller startup. `repl.py` re-bases onto it (delete the mirrored
copy; keep its public `build_dev_handler` seam and test contract).

**A2. Plain asyncio main.** `server.py.__main__` becomes: load env → boot core
→ register each adapter whose credentials exist → run the loop. Discord:
`await client.start(token)` as a task inside the shared loop (replacing
blocking `client.run()`), wrapped in the existing smart-backoff/429 logic.
Telegram/Twilio: exactly as today. **Zero adapters configured is a valid,
booting state** (scheduler + awareness run; chat happens via CLI).

**A3. Discord-conditional machinery.** Gateway watchdog, Discord presence,
command-tree sync, and the Discord-shaped wipe/restart handlers arm only when
the Discord adapter registered. No Discord token → no Discord imports executed
(module-level `import discord` in server.py moves behind the adapter branch or
into the adapter module).

**A4. Instance identity decoupled.** `KERNOS_INSTANCE_ID` remains the anchor;
absent, first-boot derives a platform-neutral default (`cli:<hostname-user>`
shape) instead of requiring the `discord:<id>` convention. Existing
`discord:*` instances load unchanged (no migration).

**Acceptance A:**
- `.env` with ONLY `TELEGRAM_BOT_TOKEN` (+ LLM key): server boots, Telegram
  serves. ← the founder's original complaint, fixed.
- `.env` with only an LLM key: server boots adapter-less; REPL/CLI chat works.
- `.env` with Discord token: existing behavior regression-pinned (watchdog
  arms, presence sets, slash commands register).
- Full suite green; new seam tests for each adapter-combination boot.

## Phase B — CLI-first onboarding (assembling the built pieces)

**B1. First-run detection + guided flow.** `kernos` (or `python -m kernos`,
whichever entry lands cleanest) with no configured LLM key enters the guided
first run:
1. **LLM connect** — reuse `kernos/setup/console.py` (`kernos setup llm`), the
   existing interactive console. getpass-style key entry, `.env` write,
   verify with a ping call.
2. **Meet your agent** — drop straight into the CLI chat (repl surface over
   the extracted core). Owner bootstrap runs on the **cli channel**
   (`channels.py` already names it) — hatching proceeds exactly as on Discord:
   organic turns, the agent names itself when the moment is right. This is the
   blueprint's "first contact is the product moment," on the terminal.
3. **Optional platforms, capability-first** — after hatching settles (or on
   `kernos setup platforms`), offer Telegram/Discord/Twilio connection with
   the blueprint's nudge framing ("want me on your phone? Telegram takes two
   minutes"). Wizard steps per platform: BotFather walkthrough → token via
   getpass → `.env` write → poller hot-start or restart note. Declining is a
   first-class ending — CLI-only Kernos is complete, not degraded.

**B2. Owner bootstrap generalization.** The Discord-hardcoded owner bootstrap
gains the CLI path: local operator = owner (the machine's user is
authenticated by possession), member profile + hatching keyed to the
platform-neutral instance id from A4. Discord-first installs behave as today.

**B3. README follows.** Quick start updates to the real one-command story once
B1 lands (`kernos` → guided everything). Not before.

**Acceptance B:**
- Fresh clone, no `.env`: `kernos` walks LLM connect → chat → hatching
  completes on CLI → platform offer declined → subsequent runs go straight to
  chat with memory intact.
- Same flow accepting Telegram: token wizard → message the bot → same agent,
  same memory, both surfaces.
- Discord-hardcoded bootstrap path untouched for existing installs.

## Out of scope

Multi-member CLI onboarding; enterprise/phone-verification flows (blueprint
consumer/enterprise onboarding); any `start.sh` modification; Gemini or new
adapters; MCP-server exposure of Kernos itself.

## Sequencing

A ships alone first (it unblocks Telegram-without-Discord immediately and
carries all the regression risk — isolate it). B follows on A's core. Codex
spec-review both phases now; post-impl review per phase; full-suite + targeted
seam tests per the house standard; live-verify A against the founder's
Telegram case and B against a scratch data-dir first run.

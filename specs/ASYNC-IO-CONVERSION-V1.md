# ASYNC-IO-CONVERSION-V1 — convert sync file I/O in message turn pipeline

**Status:** parked follow-up (scoped, not yet scheduled)
**Surfaced by:** root-cause investigation of 2026-05-19 14:24 silent-gateway failure
**Companion fixes already shipped:**
- `2a3600c` — DISCORD-GATEWAY-WATCHDOG-V1 (recovery safety net)
- `<this batch>` — LOG-PERSIST-V1 (file-based log capture for future RCA)

## Problem

discord.py's gateway WebSocket needs heartbeats acked within ~41s. The heartbeat task runs on the same asyncio event loop as everything else. When that loop is blocked by synchronous I/O for long enough (cumulative or single-shot), heartbeats miss, Discord FINs the connection, and discord.py's reconnect coroutine ALSO can't run (it's on the same loop). Result: bot enters a permanently-online-but-deaf state until manual restart.

Codex investigation (2026-05-19) confirmed the diagnostic chain via discord.py source review + community failure patterns. The proximate trigger in our case is unclear (logs were wiped by recovery restart — exactly why LOG-PERSIST-V1 ships first), but the root cause class is well-established: scattered synchronous file I/O inside async coroutines in the turn pipeline.

## Sites to convert (Codex grep results, ranked by load-bearing-ness)

**Tier 1 — every turn:**
- `kernos/kernel/conversation_log.py:104, 110, 124, 177, 217, 277, 336, 355, 387` — sync log/meta file reads/writes
- `kernos/kernel/runtime_trace.py:92, 98, 145` — sync trace append/read/rotate after persist
- `kernos/messages/handler.py:3917, 6672` — sync parent-briefing / workspace-tool-schema reads

**Tier 2 — frequent triggers:**
- `kernos/kernel/compaction.py:435, 487, 502, 689, 1194, 1279, 1339, 1344, 1371-1378, 1400` — compaction state/doc/index reads/writes
- `kernos/kernel/files.py:202, 238, 246, 342, 385` — space-file / manifest I/O
- `kernos/kernel/execution.py:192` — `_plan.json` read on awareness load

**Tier 3 — slash commands + setup:**
- `kernos/messages/handler.py:4930, 5004` — `/dump` sync file write/read
- `kernos/messages/handler.py:4329` — `/restart` `.env` read_text
- `kernos/kernel/canvas.py:1252` — canvas page read via assemble's "Our Procedures" load
- `kernos/setup/service_state.py:235` — service_state read from assemble tool filtering
- `kernos/kernel/workspace.py:232, 1042` — workspace manifest reads on route lazy registration

**Conditional (off by default):**
- `kernos/providers/codex_provider.py:593, 630` — diagnostic payload writes when capture env flags enabled
- `kernos/kernel/state_json.py:169, 176, 970, 1077, 1146, 1161` — only when `KERNOS_STORE_BACKEND=json`

## Conversion approach

Two acceptable patterns:

1. **`aiofiles`** — drop-in async file API:
   ```python
   import aiofiles
   async with aiofiles.open(path, "r") as f:
       data = await f.read()
   ```

2. **`loop.run_in_executor`** — wrap existing sync call:
   ```python
   import asyncio
   data = await asyncio.get_running_loop().run_in_executor(
       None, path.read_text, "utf-8"
   )
   ```

Prefer `aiofiles` for new code; use `run_in_executor` when the sync call is buried in helpers we don't want to thread `async` through.

## Acceptance criteria

- Every site listed above either uses an async file API or is wrapped in `run_in_executor`
- New regression test that asserts no `Path.read_text` / `Path.write_text` / `open(...).read()` in the turn pipeline (static check via AST grep)
- 24-hour live soak with the bot under typical conversation load, watching `data/<instance>/diagnostics/server.log` for `Heartbeat blocked` warnings — must show ZERO during the soak
- Watchdog (`DISCORD-GATEWAY-WATCHDOG-V1`) remains in place as the safety net regardless

## Out of scope for v1

- Converting the sqlite paths (already async via aiosqlite where it matters)
- Tier-3 slash-command paths (run rarely; lower priority)
- Background workflow loops (separate task / not in the turn hot path)

## Why not just do it now

The conversion touches ~30 sites across 8+ files. Doing it safely requires:
1. Substrate-fidelity tests at each site (which we're building anyway for the existing turn pipeline tests)
2. The LOG-PERSIST-V1 logs in place so we can verify `Heartbeat blocked` warnings actually disappear (the success metric)
3. A live soak window where we can observe behavior

Sequence: LOG-PERSIST-V1 ships first (this batch), watchdog is the safety net (already live), this spec gets scheduled once we have a clean diagnostic baseline + a soak window.

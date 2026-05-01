## NOW

**Status:** EXTERNAL-AGENT-CONSULTATION v1 shipped; CLEANUP-BATCH-V1 shipped; multi-member V1 done; Drafter v2 + STS + CRB live.
**Owner:** Founder
**Action:** Live verify EXTERNAL-AGENT-CONSULTATION v1 per standing policy. Next spec: KERNOS-DOMAIN-PASS v1 → WORKFLOW-TRIGGERS-CONSOLIDATION (when external review verdict in) → REASONING-DISPATCH-EXTRACT (parked from CLEANUP-BATCH-V1 Pass 5).
**Tests:** see Phase Summary table

> **Rule:** This block is always the first thing in the file. Whoever completes a step updates it before handing off. Format is always: Status (what), Owner (who), Action (next thing to do).

> **What this file is:** Claude Code's entry point. Read this first, then execute the Active Spec. Full planning and roadmap live in Notion. Architecture lives in `docs/TECHNICAL-ARCHITECTURE.md`.

---

## Active Spec

None currently active. Next spec will be assigned by founder.

---

## Phase Summary

| Phase | What | Tests | Completed |
|-------|------|-------|-----------|
| 1A | First Spark — SMS/Discord, Calendar MCP, persistence | 65 | 2026-03-01 |
| 1B | The Kernel — events, state, reasoning, capabilities, tasks, templates, projectors, isolation | 297 | 2026-03-06 |
| 2 | Memory + Context — entity resolution, space routing, compaction, retrieval | 627 | 2026-03-14 |
| 3 | Agent Workspace — files, tools, MCP, gate, awareness, covenants, identity, docs | 1039 | 2026-03-20 |
| 4A | Hardening — triggers, timezone, convlog locking, handler hygiene | 1330 | 2026-03-26 |
| 4B | Code Health — knowledge filtering, gate calibration, template cleanup, receipts | 1359 | 2026-03-27 |
| 4C | Structural Refactor — handler decompose, reasoning extract, handler protocol | 1359 | 2026-03-28 |
| 4D | Context Quality — ledger architecture, selective injection, fact harvest, tool surfacing | 1392 | 2026-03-31 |
| RT | Runtime Hardening — result budgeting, concurrency, timeouts, retry, turn serialization, closeout | 1429 | 2026-04-02 |
| 6A-1 | Preference State — first-class Preference type, persistence, migration from KnowledgeEntry | 1443 | 2026-04-02 |
| 6A-2 | Preference Linkage — source_preference_id on Trigger/CovenantRule, reconciliation cascade | 1453 | 2026-04-02 |
| 6A-3 | State Introspection — user truth view, operator state view, inspect_state tool, /status | 1467 | 2026-04-02 |
| 6A-4 | Preference Parser — in-turn detect, compile, match, commit; conservative detection | 1486 | 2026-04-02 |
| 6A-5 | Prompt-Contract Reduction — lean prompt, schema pruning, structural enforcement over instructions | 1487 | 2026-04-03 |
| FO-V1 | Friction Observer V1 — 8 signal patterns, post-turn detection, diagnostic reports | 1510 | 2026-04-03 |
| CS-1→5 | Context Spaces — hierarchy, scope chain, aliases, content migration, validation | 1561 | 2026-04-05 |
| TS-R | Tool Surfacing Redesign — keyword→LLM intent, three-tier surfacing, catalog scan | 1561 | 2026-04-05 |
| ARCH | Architecture Audit — truth document validation, dead code removal, security review | 1561 | 2026-04-06 |
| AW-1→5 | Agentic Workspace V1 — execute_code, workspace manifest, tool registration, builder flow | 1561 | 2026-04-07 |
| TW | Tool Window — token-budgeted schema-weighted LRU, ALWAYS_PINNED + active zones | 1561 | 2026-04-07 |
| PK | Procedural Knowledge — covenants (behavior) vs procedures (workflows), _procedures.md | 1561 | 2026-04-07 |
| CO | Cohort Optimization — Message Analyzer (combined classifier), fact harvest in compaction | 1561 | 2026-04-07 |
| SDE | Self-Directed Execution — manage_plan, plan resilience, recovery, fallback chain | 1655 | 2026-04-08 |
| IL-1 | Improvement Loop Pass 1 — covenant selective injection (pinned/situational tiers) | 1655 | 2026-04-10 |
| IL-2 | Improvement Loop Pass 2 — behavioral pattern detection, friction→whisper loop | 1688 | 2026-04-10 |
| IL-3 | Improvement Loop Pass 3 — positive workflow capture from compaction | 1693 | 2026-04-10 |
| COV-R | Default Covenants Revision — 8 covenants incl. spirit type, user-intent-is-authorization, information stewardship | 1693 | 2026-04-11 |
| IL-T2 | Improvement Loop Tier 2 — runtime trace, diagnostic tools (diagnose/propose/submit), /debug | 1721 | 2026-04-11 |
| TI-FIX | Trace Instrumentation Fix — collector threaded through handler → reasoning → providers | 1721 | 2026-04-11 |
| FUT | Follow-Up Tracking — compaction-driven trigger creation for implicit commitments/deadlines | 1733 | 2026-04-11 |
| WH | Whisper Hardening — dedup, 48h expiry, busy-state suppression | 1733 | 2026-04-11 |
| COV-R2 | Default Covenants Revision — 8 covenants incl. spirit, stewardship, response depth | 1733 | 2026-04-11 |
| CAL-TZ | Calendar Timezone Fix + orphaned plan steps + brave_local_search removal | 1733 | 2026-04-11 |
| BJORK | Bjork Dual-Strength Activation — retrieval ranking, reinforcement on use, storage on REINFORCE | 1746 | 2026-04-12 |
| SQLITE | SQLite State Migration — SqliteStateStore (38 methods), instance.db, WAL mode, future-proof schema | 1763 | 2026-04-12 |
| RENAME | instance_id Rename — mechanical rename tenant_id → instance_id across entire codebase | 1756 | 2026-04-12 |
| MEMBER | Member Identity & Resolution — invite codes (KERN-XXXX), manage_members tool, per-member scoping | 1773 | 2026-04-12 |
| TELEGRAM | Telegram Adapter — Bot API long polling, text-only V1, 4096-char chunking | 1781 | 2026-04-12 |
| PLATLOCK | Platform-Locked Invite Codes — codes bound to platform, setup instructions returned with code | 1783 | 2026-04-12 |
| CHAINS | Data-Driven Provider Chains — ChainEntry/ChainConfig, three named chains, build_chains_from_env | 1794 | 2026-04-13 |
| PLAT-ID | Platform Identity Discovery — getMe for Telegram, client.user for Discord, dynamic invite instructions | 1803 | 2026-04-13 |
| MM-P1 | Multi-Member Pass 1 — per-member profiles, spaces, conversations, compaction, knowledge, covenants, bootstrap | 1803 | 2026-04-13 |
| SOUL-REV | Soul Revision — per-member agent identity, hatching modes (unique/inherit), "Kernos" is platform not agent | 1803 | 2026-04-13 |
| HATCH | Hatching Personality Framework — organic emergence, 15-turn graduation, rich consolidation | 1808 | 2026-04-14 |
| STEWARD | Stewardship V1 — value extraction + tension detection at compaction, operational insights | 1808 | 2026-04-14 |
| MM-P2ab | Multi-Member Pass 2a+2b — instance stewardship, sensitivity classification (open/contextual/personal) | 1808 | 2026-04-15 |
| MM-P2c1 | Multi-Member Pass 2c.1 — relationships table, declare/list actions, permission profiles, STATE injection | 1808 | 2026-04-16 |
| ABUSE | Escalating Abuse Prevention — The 24 Escalation (24s→24m→24h→24d→24y→24c) | 1808 | 2026-04-14 |
| WIPE | /wipe me + /wipe all with exact-phrase confirmation, /disconnect, /restart cross-platform | 1808 | 2026-04-15 |
| EAC | EXTERNAL-AGENT-CONSULTATION v1 — consult tool, code_exec backends, reentrancy guard, consultation_log audit, agent-surface wiring | 4922 | 2026-04-30 |
| CB1 | CLEANUP-BATCH-V1 — public-surface canonical links, launcher-split docs, capability matrix + /capabilities, DECISIONS.md refresh, compaction follow-up receipts, command-parity drift test, kernel-tool dispatch path registry, NOW-block CI guard | 4952 | 2026-04-30 |

---

## Where Things Live

- **Full roadmap + priority stack:** Notion (Kernos Roadmap — Canonical)
- **Kit reviews + design documents:** Notion (Kit Reviews)
- **As-built architecture:** `docs/TECHNICAL-ARCHITECTURE.md`
- **Kernel design:** `docs/reference/kernel-architecture-outline.md`
- **Completed specs:** `specs/completed/`
- **Design documents:** `docs/reference/design-*.md`
- **Friction reports:** `data/diagnostics/friction/`

---

## Key Architectural Decisions (permanent reference)

These are load-bearing decisions that Claude Code should always respect:

1. **Handler never knows about platform adapters; adapters never know about the handler.** All communication through NormalizedMessage.
2. **Every piece of state keyed to instance_id.** Multi-instance from day one.
3. **No destructive deletions.** Shadow archive for user data. Internal operational artifacts (whispers, expired tokens) can be cleaned up.
4. **Gate philosophy: reactive soft_write = agent acts.** The user's conversational intent IS the authorization — whether explicit ("set an appointment"), confirmation ("Sure"), or rule-based. Gate only evaluates: hard_write (always), third-party impact, proactive/background actions, must_not covenant violations. "Conservative by default" applies to what the AGENT initiates, not what the USER requests.
5. **Behavioral contracts are the safety mechanism, not access restriction.** "Agent thinks, kernel enforces."
6. **Cognitive UI grammar:** RULES / NOW / STATE / RESULTS / ACTIONS / MEMORY / CONVERSATION — rebuilt every turn.
7. **Cohort agent architecture:** One principal reasoning agent surrounded by bounded specialized mediators (Router, Shaper, Surfacer, Gate, Harvester, Budgeter, Friction Observer, Preference Parser, etc.). Each should be bypassable.
8. **Turn serialization invariant:** For any (instance, space) pair, only one turn may own reasoning and side effects at a time. Per-space mailbox/runner pattern.
9. **Depth should be recoverable, not always loaded.** Memory, facts, and tools are selectively loaded per-turn.
10. **Provider neutral.** Use "lightweight model" or "cheap model" instead of "Haiku." The cheap model varies by provider configuration.
11. **Subtraction over addition.** When addressing problems, prefer: removal > structural enforcement > simplification > adding instructions. More prompt text has diminishing returns. Enforce in code, not English.
12. **Spec handoff principle.** Spec everything you're CERTAIN about (even if it's "how"). Leave open only things where Claude Code's codebase knowledge produces a better answer. Each spec opens with explicit Certain vs Open declaration.
13. **Fallback chain for LLM providers.** Three named chains (primary, simple, cheap), each an ordered list of `ChainEntry(provider, model)`. Data-driven via `ChainConfig` dict — built from env vars today, pointed at `config/providers.json` tomorrow. `_call_chain()` is the single fallback entry point. Currently: Codex → GLM-5.1 → MiniMax M2.7 → Gemma 4 31B.
14. **Plans never rot.** Three-tier resilience: provider retries → step retries with backoff → hourly slow-poll. Plan sweep every 10 minutes catches stale plans. Recovery on restart re-enqueues in-progress steps. Only explicit user action pauses a plan.
15. **Personality via principles, not traits.** "Your personality is the shape of your attention." Decision principles produce behavior; trait lists produce cosplay. Operating principles split into core non-negotiables (always enforced) and situational guidance (prefer/generally).
16. **Covenant selective injection.** Covenants classified as pinned (safety, system) or situational (preferences). MessageAnalyzer selects relevant situational covenants per turn. Prevents prompt bloat as covenants accumulate.
17. **User intent IS authorization.** When the user explicitly requests an action (amount, recipient, target specified), don't re-confirm. Their request is the confirmation. Gate only intervenes for ambiguous + high-stakes.
18. **Information belongs to whoever shared it.** Routine information flows naturally between known contacts. Sensitive/confidential information requires the sharer's consent before disclosure — even to people they know well.
19. **Spirit covenant renders first.** The agent reads purpose and warmth before rules and constraints. "You got this" before "never do X."
20. **browser-use REJECTED.** Nested agent frameworks that wrap the LLM in another agent layer are an anti-pattern for Kernos. Native MCP tools (Lightpanda) are the right abstraction.
21. **DuckDuckGo as search fallback.** Brave primary → DDG safety net on rate limit. Self-hosted SearXNG parked as future option if Brave remains problematic.
22. **Follow-ups: dual-path.** Explicit via manage_schedule (real-time), implicit via compaction extraction (safety net). Compaction follow-ups are provisional — deduped against existing triggers before creation.
23. **Whisper delivery: ambient by default.** Direct message only for EXTERNAL_DEADLINE type with due date within 24 hours. Everything else surfaces as a whisper the agent weaves into conversation.
24. **90-day horizon cap.** Compaction-extracted follow-ups beyond 90 days are rejected. Long-horizon items belong in Living State or Ledger, not triggers.
25. **SQLite per instance, instance.db shared.** Each instance gets `data/{instance}/kernos.db` with WAL mode. Shared cross-instance state (members, channels, relay) lives in `data/instance.db`. Schema designed for access patterns, not JSON mirroring — indexed columns for queries, JSON overflow for rare fields.
26. **Bjork dual-strength memory.** Knowledge entries ranked by `compute_retrieval_strength()` before MessageAnalyzer sees them. Well-established facts (high storage_strength) resist decay. Entries below 0.10 strength filtered entirely. Replaces the crude `_is_stale_knowledge(days=14)` check.
27. **instance_id replaces tenant_id.** Kernos instances aren't tenants — they're Kernos instances. The naming reflects the product, not infrastructure jargon.
28. **Invite code system: one mechanism, three use cases.** KERN-XXXX codes handle new user registration, existing user connecting a new platform, and spam rejection. One table, one code path. Zero LLM calls for all unregistered sender paths.
29. **Invite codes are platform-locked.** A code generated for Discord only redeems on Discord. Instructions travel with the code. If the platform isn't set up, the agent gets setup instructions instead of a code. The instructions registry is extensible for future adapters.
30. **Adapters are dumb pipe.** Discord, SMS, Telegram adapters know their platform and nothing else. Member identity, authorization, and security are handler concerns. Adding a new platform is ~150 lines of adapter + poller. See `docs/ADAPTER-GUIDE.md`.
31. **Per-member soul.** "Kernos" is the platform name, not the agent identity. Each member hatches their own agent with its own name, personality, and relationship. Soul dataclass fields are deprecated — identity lives in member_profiles.
32. **Hatching is organic emergence, not configuration.** 15-turn minimum. 8 engagement points (entry energy, resonance testing, naming, emoji, genuine curiosity, correction surfacing, rhetorical discovery, uncertainty honesty). Graduation produces a personality hypothesis, not a verdict.
33. **Stewardship emerges from compaction.** Value extraction and tension detection ride the compaction harvest — zero additional LLM calls. Operational insights only surface when there's a concrete actionable idea, not pattern reports.
34. **Sensitivity classification at write time.** Every knowledge entry tagged open/contextual/personal at harvest. Conservative default (personal when unsure). Enforcement via member_id filtering + future cross-member decision procedure.
35. **Relationships are declared, not inferred.** Pairwise, bidirectional, with four permission profiles (full-share, work-only, coordination-only, minimal). Conservative provisional defaults until both sides confirm. Topic exceptions are covenants with relationship scope.
36. **The 24 Escalation.** Each failed sender attempt escalates: 24 seconds → 24 minutes → 24 hours → 24 days → 24 years → 24 centuries. Spamming while blocked accelerates through tiers.
37. **Tool receipts paint reality.** After each turn with tool calls, a receipt entry logs effects in the world ("Created event: Team standup, 9am") not API calls. The agent sees its own prior actions on future turns.
38. **Full transparency.** The agent has no hidden instructions. The owner may inspect any part of the operating context. "Absence of evidence in your window is not evidence of absence in reality."

---

*When Claude Code finishes a spec: update the NOW block, increment test count, move spec to specs/completed/.*

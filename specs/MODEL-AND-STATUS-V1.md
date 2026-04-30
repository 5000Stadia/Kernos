# MODEL-AND-STATUS-V1 — surface and switch the active model from chat

## Why

QoL: the user can't see which model is answering a turn, can't switch to an
alternative without restarting the agent or editing env vars, and has no read
surface that surfaces the chain's failover state. OpenClaw exposes this via
its `/model` interactive picker. Kernos's adapter-isolation rule (handler
never imports adapter-specific UI; adapters never import handler internals)
makes a Discord-only picker the wrong shape for v1. Plain-text commands give
the same QoL across every channel — Discord, Telegram, SMS, CLI — at far
lower complexity.

## Scope

Two complementary commands, plain text on every adapter:

* **`/status` (extension)** — adds a Models block to the existing handler
  output. No code path leaves the handler.
* **`/model` (new)** — lists chains and effective entries, switches the
  active chain or the head-override for the current member-space.

Per Codex pre-spec review (refinement #5): "active chain" and
"provider/model override" are separate concepts in storage and API. A chain
switch records *which named chain the member wants*; a model override
records *which entry shadows the head of that chain*. `/model reset`
clears both unless narrower commands are added later.

## Universal commands invariant

**Every Kernos slash command — existing and future — works identically on
every adapter the kernel speaks (Discord, Telegram, SMS, CLI, future
connectors).** The contract is: a slash command is a string in, a string
out. Any rendering richer than markdown is the *future* job of an
adapter-agnostic structured-response shape (an `intent` field on the
handler's response that adapters interpret per their capabilities), not
the v1 job of any individual command.

This ties the architecture: every new command lands in the handler's
slash intercept block, returns a string, and is forbidden from importing
adapter packages. Adapters are forbidden from intercepting slash
commands — they pass `/`-prefixed text through to the handler unchanged.

This invariant covers `/status` and `/model` here, AND every future
slash command added as new connectors land. A Telegram or future
connector can render the same markdown without code changes; if it
later wants buttons, the layered structured-response contract slots
on top without breaking text fallback.

## /status — Models block

Append after the existing reminders/signals lines:

```
**Models** (this space)
Active chain: primary
Effective head: anthropic/claude-sonnet-4.6
Fallback: openrouter/glm-5.1, openai/gpt-5.5
Status: primary head answering
```

Failure-state surfacing: when the head is in cooldown / disabled /
auth-failure, the line reads `Status: head <reason>; lightweight answering`
or similar. The reason is sourced from the existing failover state machine
in `kernos/providers/chains.py` / `ReasoningService` — not a new tracker.

When a chain switch or model override is set on this `(member, space)`,
add a line:

```
Override (this space): chain=lightweight, head=openrouter/glm-5.1
```

When no override is set the Override line is omitted.

## /model — list / switch

Three modes. All sticky per `(instance_id, member_id, space_id)` —
matching how spaces, conversation logs, and knowledge already key.

### `/model` (no args) — list

```
**Models** (this space)
Active chain: primary
Effective head: anthropic/claude-sonnet-4.6 (active)
Status: primary head answering

Available chains:
  • primary       — anthropic/claude-sonnet-4.6 → openrouter/glm-5.1 → openai/gpt-5.5
  • lightweight   — anthropic/claude-haiku-4.5 → openrouter/glm-5.1-flash

Switch with:
  /model primary | lightweight        — switch chain
  /model anthropic/claude-haiku-4.5   — override head (must be in a configured chain)
  /model reset                         — clear override
```

The list is rendered from `build_chains_from_env()` output. Entries that
failed to instantiate at boot (missing credentials, etc.) are marked
`(unavailable)` and cannot be selected.

### `/model <chain>` — switch active chain

`<chain>` must be one of the configured chain names (`primary`,
`lightweight` today). Persists `chain_name` on the override row and
clears any prior `provider/model` override (the new chain has its own
head). Confirms inline:

```
Switched chain to lightweight for this space.
Effective head: anthropic/claude-haiku-4.5
```

### `/model <provider>/<model>` — override head

Validates the spec is among the entries already in some configured
chain. If yes, persists `override_provider` + `override_model` against
the currently-active chain (default chain if none set). The override
behaves as "preferred first attempt" per Codex refinement #1: when the
override head fails, ReasoningService falls through to the natural
chain sequence with the overridden entry de-duped, never re-attempted.

Rejection:

```
'foo/bar' is not in any configured chain. Available entries:
  • anthropic/claude-sonnet-4.6
  • anthropic/claude-haiku-4.5
  • openrouter/glm-5.1
  ...
```

### `/model reset`

Clears both chain selection and model override for this `(member,
space)`. Returns to the default chain config.

```
Cleared model override. Falling back to configured default (primary chain).
```

## Storage

New `model_overrides` table on `instance.db`:

```sql
CREATE TABLE IF NOT EXISTS model_overrides (
    instance_id       TEXT NOT NULL,
    member_id         TEXT NOT NULL,
    space_id          TEXT NOT NULL,
    chain_name        TEXT,                 -- NULL when no chain switch (use default)
    override_provider TEXT,                 -- NULL when no head override
    override_model    TEXT,                 -- NULL when no head override
    set_at            TEXT NOT NULL,
    PRIMARY KEY (instance_id, member_id, space_id),
    CHECK (
        (override_provider IS NULL AND override_model IS NULL)
        OR (override_provider IS NOT NULL AND override_model IS NOT NULL)
    )
);
```

Constraints:

* SQL `CHECK` enforces the nullable pair invariant — both provider and
  model set together or both null. Database-level guarantee, not just
  application-level discipline (Codex post-spec fold #B).
* Either `chain_name` or both override fields may be set independently;
  `/model reset` deletes the row entirely.

### Stale-config behavior (Codex post-spec fold #B)

The override is persisted as plain strings. If the environment changes
between writes (env vars edited, fallback list trimmed, provider
credentials revoked), the persisted spec may name a chain or entry
that no longer exists in the chains built at startup.

Resolution rule at dispatch:

1. If `chain_name` references a chain not in the current
   `ChainConfig`, the override is treated as **stale** for that field.
   Fall back to the request's default `chain_kind`.
2. If `(override_provider, override_model)` does not match any entry
   in any current chain, the override is treated as **stale** for
   that field. Skip the prepend; iterate the chain unmodified.
3. Stale state surfaces in `/status` and `/model` output:

   ```
   Override (this space): chain=lightweight (active), head=foo/bar (unavailable — not in any current chain)
   ```

4. The row is NOT auto-deleted on stale detection — the user may
   re-add the missing entry to env later. They can `/model reset`
   to clear explicitly.

Stale-config detection is read-only at dispatch; no schema change
or background sweep.

`InstanceDB.get_model_override(instance_id, member_id, space_id)` returns
a typed `ModelOverride | None`. `set_chain(...)`, `set_head_override(...)`,
`reset(...)` are the three mutators. All idempotent.

## ReasoningService integration

At dispatch time, `ReasoningService` currently picks the chain by
`chain_kind` (`primary` / `lightweight`) and iterates entries. The new
flow:

1. Look up `ModelOverride` for `(instance_id, member_id, space_id)`.
2. If `chain_name` set, prefer that chain over the request's `chain_kind`.
3. If `override_provider` + `override_model` set, prepend that entry to
   the chosen chain (with the same entry de-duped from later positions
   so it isn't tried twice).
4. Iterate as today.

No changes to chain-build or failover logic. The override is a single
runtime indirection at the dispatch site.

## Adapter isolation

Both commands return strings. No structured response shape. No adapter
imports. Discord, Telegram, SMS, CLI all see the same markdown. The
Discord adapter's existing 2000-char chunk fallback handles long lists.

A future Discord-specific interactive picker can layer on top by
adding an *adapter-agnostic* "intent" field to the handler's response
shape — out of v1 scope.

## Tests

* **Schema**: `model_overrides` table + columns + composite PK + the
  CHECK constraint rejects rows where exactly one of
  `override_provider` / `override_model` is set.
* **`InstanceDB` mutators**: round-trip set/get/reset; idempotent
  same-value writes; CHECK constraint propagates as `IntegrityError`.
* **`_handle_status` extension**: Models block present when chains
  exist; override line shown only when override set; failure-state
  reason surfaces correctly; **stale-config marker** rendered when
  the persisted spec names an entry no longer in the current chains.
* **`_handle_model_command`**:
  * No-args list shape.
  * Chain switch happy path + unknown-chain rejection.
  * Provider/model override happy path + invalid-spec rejection.
  * Reset clears both fields.
  * **Stale-override list**: persisted override referencing a removed
    entry renders the `(unavailable)` marker, does NOT auto-delete.
* **ReasoningService dispatch**:
  * Override head wins on happy path.
  * Override-head failure falls through to the natural chain with
    override de-duped (Codex pre-spec refinement #1).
  * Stale chain_name falls back to default `chain_kind`.
  * Stale `(override_provider, override_model)` skips the prepend.
* **Universal-commands invariant** (Codex post-spec fold #C — two
  layers):
  * **Behavioral, per-adapter**: a `/`-prefixed `NormalizedMessage`
    flows through each adapter's `inbound()` without modification —
    the slash text reaches the handler unchanged. One test per
    adapter (Discord, Telegram, SMS, CLI), parametrized over
    representative slash commands including `/status` and `/model`.
  * **Behavioral, per-handler-branch**: the handler's slash intercept
    block branches each return `str` for representative invocations
    of `/status`, `/help`, `/spaces`, `/wipe me`, `/wipe all`,
    `/restart` (non-owner path so it doesn't actually exec),
    `/disconnect`, `/dump`, `/model`, `/model reset`. The structural
    AST walk remains as a backstop but is not the load-bearing pin.
* **Shared-resolver structural pin**: the "effective chain/head"
  resolver lives in one helper consumed by both `_handle_status` /
  `_handle_model_command` and `ReasoningService` dispatch — no
  duplicate logic across C2 and C3 (Codex post-spec note on ask A).
* **No-regression**: existing `/status` output line ordering
  preserved (Models block appended, not interleaved); `/help`,
  `/wipe`, `/restart`, `/disconnect`, `/spaces`, `/dump` unaffected.

## Acceptance criteria

1. `/status` renders a Models block listing active chain + effective
   head + fallback entries + status reason. Sanitizes internal IDs
   per existing D5 surface discipline.
2. `/model` (no args) renders the chain list with selectable
   alternatives.
3. `/model primary` / `/model lightweight` switch the chain for the
   current `(member, space)`. Sticky across turns until reset.
4. `/model <provider>/<model>` overrides the head when the spec is in
   a configured chain. Rejects unknown specs with the available list.
5. `/model reset` clears both chain selection and head override.
6. ReasoningService picks the override-respecting chain at dispatch;
   override is "preferred first attempt," not a hard pin — natural
   fallback applies on override-head failure.
7. Plain text only. No Discord-specific UI. SMS-length-aware via the
   existing chunker.
8. All adapter isolation invariants hold (handler does not import
   adapter; adapter does not import handler internals).
9. **Universal commands invariant holds**: `/status` and `/model`
   render identically on every adapter — Discord, Telegram, SMS,
   CLI. No adapter intercepts the commands; all return the same
   string from the handler. Pinned by **two layers of behavioral
   tests** (load-bearing): (a) per-adapter inbound pass-through —
   a `/`-prefixed `NormalizedMessage` flows through each adapter's
   `inbound()` unchanged; (b) per-handler-branch — the slash
   intercept block branches each return `str` for representative
   invocations of every existing slash command plus `/model`. AST
   walking remains as a structural backstop only.
10. No regression on the existing `/status`, `/help`, `/wipe`,
    `/restart`, `/disconnect`, `/spaces`, `/dump` commands.

## Out of scope (parked)

* Discord interactive picker (buttons + select menu) — needs an
  adapter-agnostic structured-response contract first.
* Recents list for quick switching — requires UI affordance to be
  worth the storage; defer with the picker.
* Per-instance default chain override (admin operation distinct from
  per-member preference).
* Arbitrary model strings outside the configured chains (auth and
  capability validation at switch time is too heavy for v1).

## Commit shape

Single batch, target three commits:

* **C1 — schema + InstanceDB mutators + tests.** New table, three
  methods, schema/round-trip/idempotency tests including the CHECK
  constraint rejection path.
* **C2 — handler `/model` + `/status` extension + tests.** New
  `_handle_model_command`, extend `_handle_status`, dispatch in the
  existing slash intercept block. **Land the shared "effective
  chain/head" resolver here** — both handler renderings and the C3
  dispatch path consume the same helper, no duplicate logic. Also
  land the universal-commands behavioral pins (per-adapter inbound
  pass-through, per-branch return-type tests).
* **C3 — ReasoningService override-aware dispatch + tests.**
  Override lookup at dispatch, prepend-with-dedupe, fallthrough on
  failure, stale-config skip behavior. Consumes the C2 resolver.

Codex review confer on the spec design pre-write (done) and on the
implementation post-batch.

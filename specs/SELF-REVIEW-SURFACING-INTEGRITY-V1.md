# SELF-REVIEW-SURFACING-INTEGRITY-V1 — durable governance findings + coverage completion

**Status:** Draft rev 4 (kreview rounds 1–3; scope narrowing ACCEPTED; round-3 corrections
must-fixes folded)
**Builds on:** SELF-MAINTENANCE-REVIEW-V3 (functional map, coverage-gap check, improvement
docket), FRICTION-RESPONSE-V1 (friction substrate, two-key memory, verification states).
**Modules:** `kernos/kernel/self_maintenance_review.py` (constitutional — human-gated),
`kernos/kernel/friction.py`, `kernos/kernel/friction_response.py`,
`kernos/messages/handler.py` (`_handle_dump`), `tests/test_self_maintenance_review.py`,
`tests/test_friction_response.py`, `tests/test_handler.py`.

**Constitutional note.** `self_maintenance_review.py` is owned by the
`self-maintenance-methodology` element, flagged `constitutional: True`. Evolution there is
human-gated and never self-applied. This spec exists because the human gate (KABE) reviewed
the findings and approved the direction. That approval *is* the gate; recorded here so the
provenance of a constitutional change is legible.

**Implementation order: B → A → C → D** (per kreview). B first so the durable lifecycle
exists before the live gap is repaired, making the open→resolved transition testable on a
real condition. C depends on B's persistence safety. D asserts the final combined shape.

## Why

**The observable defect.** Four modules belong to no element of the functional map:
`kernos/discord_runtime.py`, `kernos/kernel/tool_failure.py`,
`kernos/kernel/tool_signatures.py`, `kernos/kernel/topic_hints.py`. The daily review selects
only from mapped elements, so these four are **structurally ineligible for self-review**,
while V3 documents the map as covering every module. Independently confirmed by kreview:
42 slices, 317 substantive modules, exactly those four unassigned.

**The cause is benign.** `REVIEW_SLICES` is a hand-authored static snapshot committed
2026-06-05 (`0c5407f`). All four modules were created after it — `tool_failure.py` and
`tool_signatures.py` 06-09, `topic_hints.py` 06-10, `discord_runtime.py` 07-10. Ownership is
prefix matching against fixed tuples, so *being unmapped is the default state of every new
file*. The map does not decay; it never grows on its own. The coverage-gap detector exists
for exactly this and worked correctly.

**The structural problem is why it stayed open for two months.** `REVIEW_SLICES` lives in
`self_maintenance_review.py`, which the map itself flags constitutional. The one repair the
system detects perfectly is one it is forbidden by design to make. It can only whisper — and
that whisper is one-shot, gated on `_fp != state["gap_surfaced_fingerprint"]`, so it
announces once per structural shape change and goes quiet, with **no durable record that
the concern is still open**.

Generalized: **a human-gated finding routed to a one-shot ephemeral whisper is
indistinguishable from a finding never raised.** The constitutional gate is correct and this
spec does not weaken it. The defect is the absence of a persistent queue behind the gate.

**Non-goals.** Weakening constitutional human-gating. Allowing the review to self-apply map
edits. Changing the two-lens method, evolution discipline, selection algorithm, or the
friction observer's pure-sink contract.

## Part B — A durable `governance` finding class *(land first)*

*Rev 1 recommended reusing the opportunity class with a marker field. kreview refuted this
with specifics, all of which I verified in the tree:*

- `open_opportunities()` takes `window_days: int = 30` and skips anything older — items
  silently vanish after 30 days.
- Its `"signature"` is `p.stem`, i.e. the UUID-bearing filename
  (`FRICTION_{ts}_{type}_{uuid8}.md`) — not a stable issue key.
- `FrictionObserver._write_report` mints a fresh `uuid.uuid4().hex[:8]` per write, so the
  write path always creates a new report; there is no upsert.
- There is no close-on-condition path for opportunities.
- `open_opportunities()` returns only `{desc, signature, mtime}`, so any marker field is
  discarded before rendering — a constitutional item would be shown as an ordinary "propose
  through the normal approval gate" opportunity.

Reuse-with-marker is therefore unworkable. **Adopt a distinct `governance` class inside the
same friction substrate** (not a parallel primitive), specified end-to-end:

**Identity.** Stable signature = the *condition identity*, e.g. `self-review:coverage-gap`.
The sorted unassigned-module set is the **fingerprint/payload**, not the signature. (Rev 1
had this inverted; as kreview noted, that would open a new item on every partial repair and
strand the prior one.) This mirrors FRICTION-RESPONSE-V1's two-key memory.

**Storage.** Filenames are derived from a **safe slug + short hash** of the signature —
e.g. `GOVERNANCE_selfreview_coverage_gap_{sha256(sig)[:12]}.md` — never the raw signature,
which contains a colon and must not reach a path. Stable across upserts by construction.

**Writer.** Atomic upsert keyed by signature — re-detection updates payload and last-seen,
never duplicates.

**Class parser (kreview must-fix 1 — this was missing from scope entirely).**
`friction_response.report_class()` currently special-cases only exact `opportunity` and maps
**every other value to `error`**, so a `governance` report would enter Shape B escalation
rather than skipping it. It must be extended:

- **no** `Class:` header → `error` (legacy compatibility, unchanged);
- explicit `Class: error` → `error`;
- exact `opportunity` → `opportunity`, unchanged;
- exact `governance` → `governance`, excluded from Shape B;
- **any explicit unrecognized value → `unknown`, quarantined.**

*Rev 3 proposed mapping unknown → `error`. kreview overruled this and is right: `error` is
not a safe default at an authority boundary, because it enters Shape B and can reach
reactive gated automation. A typo in `Class: governance` would then silently **broaden** a
human-gated item into an automated lane — the opposite of fail-safe.*

`unknown` is therefore excluded from Shape B and every auto-trigger, **logged loudly**, and
rendered in `/dump` diagnostics so it cannot vanish. This fails closed on a newly explicit
but invalid classification while leaving legacy class-less behavior untouched.

**Readers.** Updated together, or the class leaks back into opportunity semantics:
- `governance` items are **exempt from the 30-day window** — they persist until resolved.
- They **skip Shape B escalation**.
- The class marker survives into report metadata and rendering, so the surface states the
  item is **human-gated**, not "propose through the normal gate."
- No auto-trigger may consume a `governance` item.

**Recovery path (kreview must-fix 3).** A persisted file is not a recoverable queue unless
someone can enumerate it after missing the one-shot whisper. Add
`open_governance_items(data_dir)` returning `{signature, payload, opened_iso, last_seen_iso,
human_gated}` with no TTL, and render it as an **Open governance items** section in
`MessageHandler._handle_dump` — the operator diagnostic surface (`/status` is deliberately
user-facing since SURFACE-DISCIPLINE-PASS D5 and is the wrong home). Enumeration must not
re-whisper: reading the queue is not surfacing it.

**Closure and reopen (kreview must-fix 4).** Resolves only on the condition genuinely
clearing — for coverage gaps, when `unassigned_modules()` no longer reports those modules —
never on a TTL. Idle is not resolved (FRICTION-RESPONSE-V1 rule).

Closure follows the repository's **existing** shadow-archive substrate: **no hard
deletion**, reusing `diagnostics/friction_resolved/` and the manifest semantics of
`friction_response.archive_resolved_signature` (`friction_response.py:393`). *Rev 3 said
"the friction `archive/`", which kreview correctly flagged would spawn a parallel,
vaguely-named archive primitive while claiming a single friction substrate. Verified: the
canonical shadow archive is `diagnostics/friction_resolved/`.*

The resolved manifest preserves the governance **signature**, final **payload**, and
closure audit fields (opened, closed, resolving condition). Archived filenames carry a
closure timestamp so a later recurrence of the same condition reopens cleanly without
colliding with the archived name.

**Persistence acknowledgement is tracked separately from the surface fingerprint.** If the
whisper succeeds, the durable write fails, and `gap_surfaced_fingerprint` commits, the
original one-shot-loss defect survives intact. So a failed upsert must remain eligible for
retry **without** re-nagging the whisper. Two independent bits of state, not one.

**Evaluate the condition every eligible scan — not behind the shape gate.**
*(kreview must-fix 2; this was a genuine correctness bug in rev 2, verified in the tree.)*
`shape_fingerprint()` hashes the set of module **paths** returned by `list_modules()`.
Part A repairs ownership by editing `REVIEW_SLICES` — which does not change any module path,
so the fingerprint is **unchanged**. Had governance upsert/close stayed behind the existing
`_fp != gap_surfaced_fingerprint` branch, repairing the map would never be observed and the
durable item could never close: the spec would ship an item that is permanently open, which
is exactly the failure mode it exists to fix.

Therefore: on **every** due review scan, recompute `unassigned_modules()` and drive the
governance item's upsert / update / close from that live result. `shape_fingerprint` is
retained **only** as the anti-nag key for whisper surfacing, never for lifecycle state.

**Best-effort, non-breaking.** Failure to open a durable item never breaks the review, per
the kernel's best-effort emission convention — but per the previous paragraph, "best-effort"
must not mean "silently dropped."

### Scope narrowing (my counter-proposal to kreview must-fix 2)

kreview correctly observes that only coverage-gap closure is well-defined, and that absence
from one stochastic LLM review is not proof a constitutional finding is resolved.

Rather than invent a closure mechanism for stochastic findings, **v1 admits only
deterministically-verifiable conditions into the `governance` class** — v1 ships exactly one
producer, the coverage gap, whose open and closed states are both computable from
`unassigned_modules()`. Constitutional findings from the LLM review continue to surface as
today (whisper only) and are explicitly **out of scope** here.

Rationale: a durable queue whose items cannot be reliably closed becomes a growing list of
maybe-stale obligations — the failure mode this spec exists to fix, one level up. Admitting
only conditions with a deterministic verifier keeps the guarantee honest.

**kreview explicitly ACCEPTED this narrowing in round 2** as the correct boundary rather
than an evasion, and directed that the general case be named as a non-goal.

**Named non-goal / follow-on.** Stochastic LLM-originated constitutional findings do not
enter the governance queue in v1. Admitting them requires either explicit human resolution
or a per-finding deterministic verifier; absence from one stochastic review is not proof of
resolution. That is its own spec.

## Part A — Complete the map

Extend existing elements' `paths` tuples. Single-owner discipline makes each assignment a
real intention claim:

| Module | Element | Status |
|---|---|---|
| `kernos/discord_runtime.py` | `message-adapters` | kreview GREEN — sits with `sms_poller.py` / `telegram_poller.py` |
| `kernos/kernel/tool_signatures.py` | `tool-catalog-registry` | kreview GREEN — catalog/surfacing metadata |
| `kernos/kernel/topic_hints.py` | `context-routing` | kreview GREEN — router-produced hints feeding domain formation; `knowledge-retrieval` would conflate routing evidence with the user-knowledge moat |
| `kernos/kernel/tool_failure.py` | `workshop-tool-primitive` **with expanded intent** | conditional per kreview |

Per kreview: `tool_failure.py` is acceptable in `workshop-tool-primitive` **only if that
element's `intent` is widened** from tool *authoring* to include the universal
tool-execution/result contract. `ToolFailure` spans reasoning, scheduler, workspace, and
live integration dispatch, so the current tool-making intent alone does not explain the
ownership. The `intent` string is updated accordingly in the same change.
(`tool-catalog-registry` is rejected — this is not catalog metadata.)

**No refreshed hard-coded count.** Per kreview, do not replace `~311` with `~317` — that
just re-arms the same stale fact. Architectural prose asserts *"all substantive modules are
owned"* and the detector plus acceptance criterion 1 enforce it.

## Part C — One review tick, one whisper

The coverage-gap note is emitted as an independent `whisper_fn` call at
`self_maintenance_review.py:1150`, before slice selection; the review whisper follows at
`:1222`. One scheduled reflection therefore costs two System-space interruptions — a
regression against "ambient, not demanding." Fold the note into the single review whisper as
a section.

**Must-fix per kreview:** a pending coverage section has to participate in **both**
`has_anything_to_say()` and `to_whisper_text()`. Otherwise a healthy, quiet slice never
calls the combined whisper at all and the gap note silently disappears — strictly worse
than today.

**Ordering hazard.** The gap check sits after the `not_due` gate but before slice selection,
with `error` (`:1185`) and `parse_error` (`:1199`) early-returns before the review whisper.
Required behavior:

- Commit `gap_surfaced_fingerprint` **only** on successful delivery, preserving the prior
  Codex must-fix ("a failed surface re-tries next shape-change tick").
- On `error` / `parse_error`, either surface the coverage note standalone or leave the
  fingerprint uncommitted so it retries. The invariant is that **the note is never silently
  dropped**.
- Part B's durable persistence state remains **independent** of this surface fingerprint.

## Part D — Repair the stale test

`test_failed_whisper_does_not_bury_finding` fails on `origin/main`. Verified — and
independently reproduced by kreview — that **nothing is buried**: both whispers fire and the
finding is delivered intact at index 1, with the coverage-gap note at index 0. The test
asserts `seen_whispers[0]`, an assumption of one whisper per tick that V3 invalidated. User
impact nil; the real cost is a persistently red suite eroding the meaning of "pytest green."

*Rev 1 proposed asserting `report["kind"] == "coverage_gap"`, which kreview correctly notes
conflicts with Part C — after folding there is one combined report, so the top level cannot
be `coverage_gap`.* Revised, per the substrate-fidelity standard:

- Assert **exactly one** successful whisper (Part C promises this; "any whisper" is too
  loose once that promise exists).
- Assert it contains `real concern`.
- Assert the nested coverage payload is identifiable: `report["coverage_gap"]["kind"] ==
  "coverage_gap"`.
- Keep `state["seen"]` committing only after success — the invariant the test is named for,
  which still holds.

## Acceptance criteria

1. `unassigned_modules(REVIEW_SLICES, repo_root)` returns empty on a clean tree, and
   architectural prose claims coverage without a hard-coded module count.
2. A module added under a mapped prefix is auto-owned; one added outside all prefixes still
   registers as a gap (Part A does not weaken the detector).
3. A coverage gap opens exactly one durable `governance` item keyed by condition identity;
   re-detection upserts rather than duplicating; partial repair updates the fingerprint
   without stranding or re-opening. Filenames derive from a safe slug + hash, never the raw
   colon-bearing signature.
4. `report_class()`: class-less → `error` (legacy unchanged); explicit `Class: error` →
   `error`; exact `opportunity` → `opportunity`; exact `governance` → `governance`; any
   explicit unrecognized value → `unknown`. Both `governance` and `unknown` are excluded
   from Shape B and every auto-trigger; `unknown` is logged loudly and rendered in `/dump`.
   Asserted directly: a typo'd `Class: governnace` must NOT reach Shape B.
5. A `governance` item survives past 30 days, renders as human-gated, and is consumed by no
   auto-trigger.
6. **The item closes after Part A's map repair.** Explicitly tested end-to-end: open the
   item on the real gap, apply the ownership fix, run a due scan, assert the item closes —
   proving lifecycle evaluation does not sit behind `shape_fingerprint`, which the repair
   leaves unchanged.
7. Closure archives into the existing `diagnostics/friction_resolved/` substrate via
   `archive_resolved_signature` manifest semantics — never a new archive primitive, never a
   hard delete — preserving signature, final payload, and closure audit fields; a later
   recurrence reopens without colliding with the archived name.
8. `open_governance_items()` enumerates open items with the human-gated marker intact, and
   enumeration does not re-whisper.
9. A failed durable write leaves the item eligible for retry **without** re-nagging the
   whisper; persistence state and `gap_surfaced_fingerprint` are independent.
10. A single review tick produces exactly one whisper containing both review and any
    coverage section, including when the slice is otherwise healthy and quiet.
11. The gap note is never dropped on `error` / `parse_error`.
12. **No change to constitutional human-gating:** no path lets the review self-apply a map
    edit or any constitutional-element change; the `governance` class creates bookkeeping,
    not authority. Verifiable end-to-end now that the marker survives to rendering.
13. `test_failed_whisper_does_not_bury_finding` passes and still fails under a genuine
    burial (verified by temporary mutation).
14. Full `tests/test_self_maintenance_review.py` green; no regression in
    `tests/test_recursive_self_heal.py`, `tests/test_friction_response.py`, or
    `tests/test_handler.py` (shared governors + the new dump section).

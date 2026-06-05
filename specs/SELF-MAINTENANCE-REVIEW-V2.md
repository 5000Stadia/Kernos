# SELF-MAINTENANCE-REVIEW-V2 — Selective selection + on-demand targeting

**Status:** Draft (for Codex spec review)
**Builds on:** SELF-MAINTENANCE-REVIEW-V1 (daily two-lens self-review, default-off, 11 rotating slices)
**Module:** `kernos/kernel/self_maintenance_review.py`, `kernos/messages/handler.py`

## Why

V1 picks the daily slice by blind round-robin (`slice_for_cursor`). Rotation is a
*completeness guarantee* (every slice reviewed within ~a week — no blind spots),
but it ignores where the action actually is: a slice that just had friction or
heavy code churn waits its turn behind quiet ones. And the new agent-callable
`run_self_review` tool can't aim at a named area.

This spec keeps rotation's coverage guarantee while letting **recent signal
promote** the most-relevant slice, and adds **on-demand targeting**.

## Goals

1. **Signal-promoted selection with a rotation floor.** Daily auto-pick favours
   the slice with the most recent signal, but no slice can ever starve.
2. **On-demand targeting.** `run_self_review` (and `/selfreview`) accept an
   optional target slice; the owner/agent can say "review the dispatch gate".
3. **Enable the daily auto-review on the live instance** (code default stays
   OFF / opt-in; only the live `.env` flips on).

Non-goals: changing the review prompt, the two-lens method, the evolution
discipline, the constitutional/human-gated handling, or the dedup/receipts.

## Design

### Selection score (replaces `slice_for_cursor` for the auto-pick)

Per-slice durable state gains `last_reviewed: {slice_name: iso}`. Each auto-pick:

**Step 1 — hard coverage floor (the rotation guarantee).** If any slice's age
(days since `last_reviewed[slice]`, or ∞ if never) ≥ `COVERAGE_MAX_DAYS`
(default 10), the candidate set is restricted to the **stalest** such slices and
signal is ignored — pick the oldest (tie-break by `REVIEW_SLICES` index). This
caps worst-case coverage at ~`COVERAGE_MAX_DAYS`, preserving V1's ~weekly intent
regardless of how loud other slices are.

**Step 2 — signal-promoted score (when nothing is past the floor).**

```
score(slice) = W_SIGNAL * signal(slice) + W_STALE * age_days(slice)
```

- **`age_days(slice)`** = **days** since `last_reviewed[slice]` (never-reviewed =
  `COVERAGE_MAX_DAYS`, so a fresh slice can't out-stale the floor). Monotonically
  increasing → the floor in Step 1 is what ultimately guarantees no starvation;
  this term just biases toward less-recently-seen slices among the eligible.
- **`signal(slice)`** = bounded count of recent signals attributed to the slice
  within `SIGNAL_WINDOW_DAYS` (default 7), capped at `SIGNAL_CAP` (default 5):
  - **churn**: files changed by commits in the window (`git log --since --name-only`)
    whose path is **under** one of the slice's `paths` (prefix-safe: a file `f`
    matches path `p` iff `p` is a file and `f == p`, or `p` is a dir/prefix and
    `f` starts with `p` on a path boundary). No basename matching.
  - **friction**: recent friction reports (bounded read, newest first) whose text
    contains one of the slice's `paths` as a normalized substring **or** the
    slice `name` matched on word boundaries. Coarse + best-effort by design.
- Pick `argmax`; deterministic tie-break by `REVIEW_SLICES` index.
- Env-tunable: `KERNOS_SMR_W_SIGNAL` (6.0), `KERNOS_SMR_W_STALE` (1.0 /day),
  `KERNOS_SMR_SIGNAL_WINDOW_DAYS` (7), `KERNOS_SMR_SIGNAL_CAP` (5),
  `KERNOS_SMR_COVERAGE_MAX_DAYS` (10). Within the floor, max signal (6·5=30) can
  jump the queue, but Step 1 still forces any slice older than the floor.

**Robustness (per-source isolation):** each signal source is independently
wrapped — no git, missing/empty friction dir, or a parse error yields `0` for
*that source only* and never raises into the review. With all sources dark,
selection is pure `age_days` (≈ rotation).

`cursor` is retained in state (unused by the auto-pick). Migration: a first V2
run with empty `last_reviewed` treats all slices as `COVERAGE_MAX_DAYS` old →
Step-1 floor fires → it sweeps every slice once (oldest-by-index) before signal
takes over. Rotational continuity with the old `cursor` is **intentionally
reset** (a one-time, self-healing effect).

### On-demand targeting

Three explicit wiring points (Codex spec review): (a) `run_self_review` schema
gains an optional `target` string; the dispatcher passes `tool_input["target"]`
through to `_handle_self_review_tool`. (b) `/selfreview <name>` parses the
trailing name. (c) `_run_self_review_now(instance_id, target=None)` carries it.

When `target` is present:
- resolve case-insensitively against `REVIEW_SLICES` names **before** any
  consult/state write/receipt; unknown → friendly error listing valid names,
  and **nothing runs** (no consult, no `last_reviewed` update, no receipt);
- review THAT slice directly, bypassing scoring;
- **bypass the `seen` dedup filter** — an explicitly targeted review returns the
  raw current findings (you asked about this slice; you get its real state, even
  if a finding was surfaced recently). Auto-picks keep dedup.
- still update `last_reviewed[slice]` on a successful (non-error, non-parse-error)
  review; still honour force + voiced render + constitutional/health guard.

### Parse-error / error handling

Mirrors V1's "don't advance the cursor on a non-clean review": on `error` or
`parse_error`, **do not** update `last_reviewed` for that slice (so a failed read
doesn't count as coverage and the slice stays eligible).

### Enablement

Live `.env` (Kernos-main): uncomment `KERNOS_SELF_MAINTENANCE_REVIEW=1`. Code
default in `is_enabled()` stays OFF (opt-in for any future deploy).
**Precondition:** the env flag enables the loop for *every* instance the process
hosts; the live bot is single-instance per process, so this is fine. An optional
`KERNOS_SMR_INSTANCE_ALLOWLIST` (comma-separated `instance_id`s) restricts the
loop when set — documented for any future multi-instance host. `start.sh`
untouched (human-only).

## Acceptance criteria

1. With recent signal on slice X (a commit under X's `paths`, or a friction
   report naming an X path/name) and all slices within the coverage floor, the
   auto-pick selects X.
2. With no signal anywhere, the auto-pick selects the **least-recently-reviewed**
   slice; repeated runs cycle the full set (≈ rotation).
3. **Coverage floor:** any slice whose age ≥ `COVERAGE_MAX_DAYS` is picked over a
   freshly-signalled but recently-reviewed slice — worst-case time-to-review is
   bounded by ~`COVERAGE_MAX_DAYS`, not 30+ days (no starvation).
4. Prefix-safe churn mapping: a commit to `kernos/kernel/workflows/x.py` maps to
   `workflows` but a commit to `kernos/kernel/gateway_other.py` does **not** map
   to `dispatch-gate` (no basename false-positive).
5. `run_self_review(target="dispatch-gate")` / `/selfreview dispatch-gate`
   reviews that slice; an unknown target returns a friendly list of valid names
   and runs **nothing** (no consult, no `last_reviewed` write, no receipt).
6. A targeted review **bypasses `seen` dedup** (returns raw findings); an
   auto-pick still applies dedup.
7. On `error` / `parse_error`, `last_reviewed` is **not** updated for that slice.
8. Per-source signal failure (no git binary / missing friction dir / unreadable
   report) → selection still works on `age_days` alone, no exception.
9. Constitutional slices remain human-gated; owner-gate + reflection-only +
   voiced render + constitutional/health guard all unchanged.
10. Existing V1 state (`{cursor,last_run_iso,seen}`) loads without error; missing
    `last_reviewed` defaults to `{}`; first-run sweep covers all slices once.
11. Default-off preserved at the code level; `KERNOS_SMR_INSTANCE_ALLOWLIST`,
    when set, restricts the loop to listed instances; only the live `.env`
    enables it.

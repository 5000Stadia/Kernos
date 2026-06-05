# FRICTION-RESPONSE-V1

Status: DRAFT v2 (founder direction 2026-06-05 — "two elements"; Codex spec review YELLOW → 9 findings folded, §9 binding)

> **§9 is binding.** The Codex spec pass found the trigger, the ledger key, the close-loop, and the auto-trigger "not safe enough as written." §9 supersedes any looser statement above.

## 1. Two elements, one spirit

KERNOS has two self-stewardship shapes, deliberately **separate**:

- **Shape A — SELF-MAINTENANCE-REVIEW-V1** (shipped, default-off): the **24h creative consideration** of code/system elements. Contemplative, generative, one slice/day. Stays pure; carries no friction.
- **Shape B — FRICTION-RESPONSE-V1** (this spec): the **immediate, reactive resolution** of operational friction and diagnosable errors. Diagnose → resolve (through the gate) → on resolve archive, on failure remember what was tried so the same wrong fix never loops.

A is reflection; B is response. They share **operational governors** (§7) — a maintenance mutex, a global budget, receipts, the whisper/System-space surface — but **never** share triggers, queues, or review content.

## 2. Trigger — at production time, debounced + non-reentrant

A friction report is written → a friction event fires → Shape B evaluates eligibility BEFORE any expensive work:

- **Self-friction denylist (critical).** NEVER respond to friction whose source is Shape B's own machinery — `friction_response`, `diagnose_issue`, `improve_kernos`, ledger writes, archive moves, the daily sweep. Friction reports carry a **source tag**; self-tagged friction is routed to human, never auto-handled. This kills the feedback loop where a response produces friction that triggers a response.
- **Durable in-flight reservation.** Before diagnosis, transactionally reserve a "response in flight" record for the friction **signature** (§4). A second event for an already-in-flight or pending-verification signature is dropped (or deduped to the existing attempt) — no concurrency, no double-spend.
- **Cooldown by SIGNATURE, not type.** Debounce on the stable `friction_signature` (§4), so unrelated failures sharing a coarse type aren't suppressed and novel signatures aren't churned. Type is a *secondary* budget bucket only.
- **Anti-loop** — never re-attempt a `resolution_fingerprint` that already FAILED for the same `friction_signature` (§4).
- **default-off kill switch** (`KERNOS_FRICTION_RESPONSE`) + **idle-aware**.

A **daily sweep backstop** (Shape B, not Shape A) catches signatures the event path missed.

## 3. Diagnose → decide → resolve (the human surface is CONVERSATIONAL)

KERNOS is for non-technical people: it presents situations simply, acts on its
own when it safely can, and when it needs a human it asks **once, in plain
conversation, with a single natural answer** — never a diff, a slash command,
or a back-and-forth. Technical artifacts (diagnosis, spec, the change, the
commit) are the OPERATOR/audit layer; the person just has a normal exchange.

1. **Diagnose** with `diagnose_issue` → cause + proposed fix + confidence + a
   `resolution_fingerprint` (§4).
2. **Decide whether the human is even needed** — proportional caution (the same
   judgement the dispatch gate uses): *low ambiguity + low loss-cost + high
   confidence* ⇒ act without asking; *ambiguous or higher-stakes* ⇒ ask.
3. **Resolve, two paths — both ACTIVE, never a passive note:**
   - **Auto (the obvious).** When it's clearly worth doing and low-risk, KERNOS
     runs the scoped fix itself through the existing gate. The everyday user is
     not shown a diff; at most a plain after-the-fact line in KERNOS's voice
     ("I tidied up the connection leak that was nagging us"). The diff/commit
     live in the operator/audit layer.
   - **Ask-once (judgement call).** KERNOS surfaces the situation
     CONVERSATIONALLY in its own voice — one plain sentence: *"I noticed the bot
     keeps dropping its database connections; I can fix that — want me to?"* — and
     a single natural affirmative ("yes" / "go ahead") carries the WHOLE thing:
     it then runs the fix invisibly. No special command, no second prompt, no
     diff. Deduped while an attempt is open/pending (no nagging).
4. **Engine guards (unchanged, operator-layer):** whichever path, the actual
   change runs through the scoped-`improve_kernos` guard set — allowlisted
   signatures, confidence threshold, deterministic repro where possible, clean
   protected worktree, guardrail/constitutional path denylist (reuse
   `recursive_self_heal`'s set), per-signature attempt cap, preflight, cancel.
   These bound the machinery; they are NOT shown to the everyday user.
5. **Close the loop — per signature, post-deploy, windowed (§5).**

> v1 ships the conversational ask-once path as default (safe, simple); the
> fully-autonomous "act without asking on the obvious" path is enabled
> deliberately once the discernment is trusted. Either way the user surface is
> the same: simple, conversational, one answer — or nothing at all.

## 3A. Authorization + audit (BINDING — Codex spec-review v3 §1–§3)

The conversational simplicity is the USER's view; underneath, nothing is
loosened. Three binding rules:

**(i) A natural "yes" binds to ONE pending-fix authorization, or it no-ops.**
The ask creates a durable single-use authorization object:
`{auth_id, friction_signature, resolution_fingerprint, ask_message_id,
user_id, space_id, created_at, ttl}`. A plain affirmative authorizes it ONLY
when ALL hold: same `user_id`, same `space_id`/thread, it is the direct reply
or the immediate next turn, within a short TTL, single-use, **exactly one**
pending fix outstanding, and no intervening prompt. A stale / ambiguous /
multiple-pending / out-of-context "yes" does NOT authorize anything — it no-ops
and (if still relevant) re-asks with a fresh prompt. (This is a thin product
framing over the existing REQUEST-APPROVAL-ACTION-V1 receipt — natural language
in, durable single-use receipt underneath.)

**(ii) Auto-without-asking is fail-closed against an EXPLICIT allowlist.** A fix
is auto-applied with no prompt ONLY if it provably meets ALL of: allowlisted
`friction_signature`; confidence ≥ threshold; deterministic repro/verification
available; small, bounded touched-path set (low blast radius); reversible; a
clean, protected worktree; no prior failed `resolution_fingerprint`; and it
touches NONE of — data deletion / migration, auth / security, guardrail or
constitutional machinery (reuse `recursive_self_heal`'s set), schema, or a user
preference / product trade-off. Anything not provably meeting every criterion
falls through to **ask-once**; anything touching guardrails is **operator-only**
(never auto, never a simple ask). "Proportional caution" is the intent; THIS
list is the boundary. **v1 ships auto-without-asking OFF — ask-once is the
default — until the discernment is trusted.**

**(iii) The commit binding is PRESERVED, not narrowed; the audit receipt is
mandatory.** The user never sees a diff, but the actual change STILL runs
through the existing commit-approval binding (parent SHA + `expected_diff_hash`
from IMPROVEMENT-LOOP-WORKFLOW-V1 / SUBSTRATE-SELF-TEST-V1) — the conversational
authorization simply SUPPLIES that gate's authorization instead of a separate
operator click. Every response writes a durable operator/audit receipt:
`{auth_id, friction_signature, resolution_fingerprint, trigger, auto_or_ask +
rationale, parent_sha, diff_hash, touched_paths, commit_sha, tests/preflight
result, verification_state, rollback_artifact}`. Operator gets the full
receipts; the user gets one sentence and one answer.

## 4. Durable memory — two keys, precise anti-loop

`diagnostics/friction_resolutions.jsonl`, append-only. The de-dupe rests on **two** keys, not one:

- **`friction_signature`** — a STABLE pattern ID where the detector provides one; else a normalized `detector/type + error-code + site + resource + stack-top`, **excluding** timestamp, report path, and prose noise. (What the problem *is*.)
- **`resolution_fingerprint`** — normalized `cause→fix plan + touched subsystem/files`. The commit/patch hash is *evidence*, not the primary key. (What we *tried*.)

```
{ts, friction_signature, resolution_fingerprint, attempted_resolution,
 outcome, state, commit_sha, source}
```

**Anti-loop rule:** *never repeat the same failed `resolution_fingerprint` for the same `friction_signature`.* (Not "never retry the same friction"; not "never retry the same fix globally.") The ledger survives report archival — it is the operator's durable point of reference.

## 5. Close-the-loop — real verification states

Resolution is judged **per signature**, **after** the fix's deployed/applied timestamp, over a **detector-opportunity window** (enough live activity for the detector to have fired again). States:

- `pending_verification` — fix applied, watching the window.
- `resolved` — no recurrence of the signature across a sufficient opportunity window.
- `recurred_failed` — the signature fired again post-fix → ledger the `resolution_fingerprint` as failed (feeds anti-loop).
- `unknown_no_observation` — not enough live activity to judge (bot idle/down). **Absence of reports while idle is NOT proof of resolution.**

Only `resolved` triggers archival.

## 6. Archive — by signature, with a manifest

On `resolved`, shadow-archive **only** the reports matching that `friction_signature` (never a whole coarse type — that could hide unrelated open reports) into `diagnostics/friction_resolved/`, write a **manifest** linking the archived reports → the resolution ledger entry, and ensure archived reports are **excluded** from the event + sweep readers. Never hard-delete.

## 7. Shared governors (Shape A ∩ Shape B ∩ recursive-heal)

Separate elements, but all remediation lanes share **operational governors** so they don't collide:

- **Maintenance mutex** — `max concurrent remediation = 1` across Shape A, Shape B, and recursive self-heal.
- **Global budget** — token / time / tool-call ceilings, shared, not per-lane count only.
- **Receipts** + the **whisper / System-space surface**.

They do NOT share triggers, queues, or review content.

## 8. Friction reading — fix the blind spot

Two live naming conventions exist (`FRICTION_<ts>_<TYPE>_<hash>.md` AND `<ts>_<TYPE>_<hash>.md`); the existing readers (`diagnostics.py:215`, `server.py:276`) glob only `FRICTION_*.md` and silently miss a whole class (the recurring `CONNECTION_POOL_LEAK`). Shape B's reader handles both; fix the existing globs too, or friction is produced but invisible to its consumers.

## 9. Codex spec hardening (folded — BINDING)

Verdict YELLOW → these are required before code:

1. **Reentrancy/self-friction:** durable in-flight reservation BEFORE diagnosis; friction source tags; denylist self-emitted friction → route to human (§2).
2. **Cooldown by stable signature**, type as secondary bucket (§2/§4).
3. **Budget = cost, not count:** token/time/tool-call ceilings, max-concurrent = 1, shared maintenance-busy exclusion across all lanes (§7).
4. **Ledger = two keys:** `friction_signature` + `resolution_fingerprint`; anti-loop is per-(signature, failed-fingerprint) (§4).
5. **Verification states** post-deploy over an opportunity window; idle ≠ resolved (§5).
6. **Archive by signature + manifest + reader exclusion** (§6).
7. **Ask-once is the v1 default** (auto-without-asking OFF until trusted); the conversational ask is deduped while an attempt is open/pending (§3, §3A-i). A natural "yes" binds to exactly ONE durable single-use pending-fix authorization or no-ops (§3A-i).
8. **Auto-without-asking is fail-closed** against the explicit allowlist (§3A-ii); the underlying change keeps the existing commit-binding + the mandatory audit receipt (§3A-iii) — allowlist + confidence + repro + clean/protected worktree + path denylist + per-signature cap + preflight + cancel.
9. **Shared governors, not isolation:** maintenance mutex + global budget + receipts + surface shared; triggers/queues/content not (§7).

## 10. Build sequence

1. Friction reader (both conventions) + `friction_signature`/`resolution_fingerprint` derivation + resolution ledger.
2. Maintenance mutex + cost budget (shared governor) + in-flight reservation + self-friction denylist + signature cooldown.
3. Diagnose → decide (auto-criteria §3A-ii) → ask-once conversational confirm (default) with the single-use natural-"yes" authorization (§3A-i) → on yes, the gated fix + mandatory audit receipt (§3A-iii) → verification-state machine.
4. Archive-by-signature + manifest + reader exclusion.
5. Daily sweep backstop; fix the existing reader globs.
6. (opt-in, later, OFF in v1) auto-without-asking on the obvious — gated by the explicit fail-closed allowlist (§3A-ii); the commit-binding + audit receipt (§3A-iii) are identical to the ask-once path.

Re-run Codex spec pass → GREEN before code. Then implement → Codex code review.

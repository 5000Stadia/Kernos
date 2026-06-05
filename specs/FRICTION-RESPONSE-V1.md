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

## 3. Diagnose → resolve → close the loop

1. **Diagnose** with `diagnose_issue` (runtime trace + source + friction reports) → structured cause + proposed fix + a `resolution_fingerprint` (§4).
2. **Resolve** (staged autonomy):
   - **(a) surface-first — v1 default.** Surface the diagnosis + proposed fix to the System space / whisper. **Deduped while an open or pending-verification attempt exists** for that signature (no repeated nagging).
   - **(b) auto-trigger — opt-in flag, after (a) soaks.** Fire a scoped `improve_kernos`. Beyond the commit-approval gate it ALSO requires: **allowlisted signatures** only; a **confidence threshold** on the diagnosis; **deterministic repro/verification** where possible; a **clean, protected worktree**; a **path denylist** for guardrail/constitutional machinery (reuse `recursive_self_heal`'s constitutional set); a **per-signature auto-attempt cap**; a **visible preflight**; and a **cancel switch**.
3. **Close the loop — per signature, post-deploy, windowed (§5).**

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
7. **Surface-first default**, deduped while an attempt is open/pending (§3a).
8. **Auto-trigger guards:** allowlist + confidence + repro + clean/protected worktree + path denylist + per-signature cap + preflight + cancel (§3b).
9. **Shared governors, not isolation:** maintenance mutex + global budget + receipts + surface shared; triggers/queues/content not (§7).

## 10. Build sequence

1. Friction reader (both conventions) + `friction_signature`/`resolution_fingerprint` derivation + resolution ledger.
2. Maintenance mutex + cost budget (shared governor) + in-flight reservation + self-friction denylist + signature cooldown.
3. Diagnose → resolve (a surface-first, deduped) → verification-state machine.
4. Archive-by-signature + manifest + reader exclusion.
5. Daily sweep backstop; fix the existing reader globs.
6. (opt-in, later) auto-trigger (b) with the §3b guard set.

Re-run Codex spec pass → GREEN before code. Then implement → Codex code review.

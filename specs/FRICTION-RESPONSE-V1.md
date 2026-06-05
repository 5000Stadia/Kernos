# FRICTION-RESPONSE-V1

Status: DRAFT (founder direction 2026-06-05 — "two elements": the 24h creative review is one shape; immediate resolution of a friction report / diagnosable error is a separate shape. Address friction when it comes up; on resolve remove it; on failure keep a record so we don't loop the same wrong fix.)

## 1. Two elements, one spirit

KERNOS has two self-stewardship shapes, and they are deliberately **separate**:

- **Shape A — SELF-MAINTENANCE-REVIEW-V1** (already shipped, default-off): the **24h creative consideration** of code/system elements. Contemplative, generative, one slice/day — "is there a better way, does this still serve the whole." Stays pure; carries no friction.
- **Shape B — FRICTION-RESPONSE-V1** (this spec): the **immediate, reactive resolution** of operational friction and diagnosable errors. When a friction report is produced, deal with it *right then* — diagnose → resolve (through the gate) → on resolve archive it, on failure remember what was tried so the same wrong fix never loops.

A is reflection. B is response. They share only the spirit (self-stewardship) and the action gate (`improve_kernos`).

## 2. Trigger — at production time, debounced

A friction report is written (`FrictionObserver` + detectors) → a friction event fires → Shape B evaluates **eligibility** before doing anything expensive:

- **per-TYPE cooldown** — a leak fires every ~30 min; respond to the *type* at most once per cooldown (default ~6h), never on every emission.
- **global daily budget** — cap responses/day (default 8) so it can't run away or blow cost.
- **anti-loop** — never re-attempt a resolution that already FAILED for the same friction signature (the resolution ledger, §4).
- **idle-aware** + **default-off kill switch** (`KERNOS_FRICTION_RESPONSE`).

A **daily sweep backstop** catches anything the event path missed (recurring patterns, a type that never got an event hook). This backstop belongs to Shape B — Shape A's creative rotation does NOT carry friction.

## 3. Diagnose → resolve → close the loop

1. **Diagnose** the friction with KERNOS's existing `diagnose_issue` (runtime trace + source + friction reports) → a structured cause + proposed fix.
2. **Resolve** (staged autonomy — v1 default is the cautious one, with an opt-in escalation):
   - **(a) surface-first (v1 default)** — surface the diagnosis + proposed fix (to the System space / a whisper) for the operator or agent to trigger `improve_kernos`.
   - **(b) auto-trigger (opt-in flag)** — fire a scoped `improve_kernos` for the diagnosed cause→fix automatically. It STILL stops at the human commit-approval gate — no autonomous code change. Recommended once (a) has soaked.
3. **Close the loop**:
   - **Resolved** — fix deployed AND the friction type stops recurring within a verification window → archive that type's open reports to `diagnostics/friction_resolved/` (shadow archive, never hard-delete) + ledger `resolved`.
   - **Failed / unresolved** — ledger the attempt (type + resolution fingerprint + outcome) so the next response proposes something DIFFERENT.

## 4. Durable memory — the anti-loop ledger

`diagnostics/friction_resolutions.jsonl`, append-only:
```
{ts, friction_type, fingerprint, attempted_resolution, outcome, commit_sha}
```
This is the operator's point-of-reference: what friction existed, what was tried, what happened. It **survives report archival** (a resolved type's reports are gone, but the record of how it was resolved persists), and it is what makes the cooldown + anti-loop work. Mirrors `recursive_self_heal`'s durable "never repeat a fix for the same signature" guard.

## 5. Friction reading — fix the blind spot

The friction folder has TWO live naming conventions — `FRICTION_<ts>_<TYPE>_<hash>.md` AND `<ts>_<TYPE>_<hash>.md`. The existing readers (`diagnostics.py:215`, `server.py:276`) glob only `FRICTION_*.md` and so **silently miss a whole class of reports** (the recurring `CONNECTION_POOL_LEAK`, `PREFERENCE_STATED_BUT_NOT_CAPTURED`). Shape B's reader handles both conventions; the existing globs should be fixed to match (otherwise friction is produced but invisible to the very consumers meant to act on it).

## 6. Bounds (non-negotiable)

- default-off kill switch; idle-aware.
- per-type cooldown + daily budget — bounded cost, no runaway.
- anti-loop — never repeat a failed fix for the same signature.
- every actual code change still flows through the commit-approval gate.
- shadow-archive, never hard-delete.
- v1 ships resolution mode (a) surface-first; (b) auto-trigger is opt-in after soak.

## 7. Relationship to Shape A (explicit)

Separate elements, separate modules. Shape A (`self_maintenance_review.py`) stays pure creative code/system review. Shape B lives in its own module (e.g. `friction_response.py`). The only shared seam is the gate. Do not re-introduce friction into Shape A's rotation.

## 8. Build sequence

1. Friction digest reader (both naming conventions) + resolution ledger + archive-on-resolve.
2. Eligibility (cooldown / daily budget / anti-loop) + the event hook (gated, idle-aware).
3. Diagnose → resolve wiring (`diagnose_issue` → surface or gated `improve_kernos`).
4. Close-the-loop: archive-on-resolve + ledger every attempt.
5. Daily sweep backstop.
6. Fix the existing friction-reader globs.

Codex spec review (this) → GREEN before code. Then implement → Codex code review.

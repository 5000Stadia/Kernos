# Runtime defaults — self-governance lanes

> **This table is the single authoritative source for these lanes' defaults.**
> It is machine-checked against the code by `tests/test_runtime_defaults_doc.py`,
> which fails if the doc and the live predicates disagree in either direction.
> Do not restate a default anywhere else — link here instead.

**Scope (deliberately narrow).** This documents the four **self-governance
lanes** only: the mechanisms by which KERNOS reviews, repairs, or modifies
itself. It is *not* an exhaustive environment-variable reference, and it should
not grow into one — an "exhaustive" claim would itself drift, which is the
failure this file exists to prevent.

| Key | Lane | Default | Env var(s) | Enabled when | Module |
|---|---|---|---|---|---|
| `self_maintenance_review` | Daily self-maintenance review (Shape A) | **ON** | `KERNOS_SELF_MAINTENANCE_REVIEW` | `not in {0,false,off,no}` | `kernos/kernel/self_maintenance_review.py` |
| `friction_response` | Friction response (Shape B) | **OFF** | `KERNOS_FRICTION_RESPONSE` | `in {1,true,on,yes}` | `kernos/kernel/friction_response.py` |
| `recursive_self_heal` | Recursive self-heal | **OFF** | `KERNOS_RECURSIVE_SELF_HEAL` | `not in {"",0,false,no,off}` | `kernos/kernel/recursive_self_heal.py` |
| `autonomy_loop` | improve_kernos autonomy loop (bring-up) | **OFF** | `KERNOS_ARCHITECT_ACTOR_ID`, `KERNOS_OPERATOR_ACTOR_ID` | `all_nonempty` | `kernos/setup/bring_up_substrate.py` |

## What each lane actually does when on

Stated here so the lanes are not conflated when the system is described
externally — the specific error this file was written after.

- **`self_maintenance_review`** — reviews ONE element of its own code per day
  through a corrective and a generative lens and surfaces a reflection to
  consider. **Reflection-only: it never changes code.** Costs roughly one
  bounded LLM call per day and defers to live turns.
- **`friction_response`** — reactively diagnoses the most-pressing open friction
  report and surfaces a diagnosis, with anti-loop two-key memory.
- **`recursive_self_heal`** — a **bounded one-child repair** when an attempt
  aborts on a bug in the loop machinery itself. This is *not* a general
  self-repair capability. Its database recursion bound and constitutional
  routing belong to **this lane only** and must not be attributed to the
  ordinary improvement loop.
- **`autonomy_loop`** — wires the spec → implement → review → approve → deploy
  loop at bring-up. Skipped entirely unless **both** identities are set, so the
  loop is either fully wireable or not started.

## `Enabled when` grammar

Machine-parsed. Exactly two forms are legal; anything else fails the parser
loudly rather than being skipped.

- **Single-variable membership** — `in {a,b,c}` or `not in {a,b,c}`, evaluated
  against the lowercased, stripped value of the row's single env var. A bare
  `""` inside the set denotes the empty string.
- **Multi-variable conjunction** — `all_nonempty`: every env var named in the
  row must be non-empty.

## Known inconsistency (pinned, not endorsed)

The three single-variable lanes use **three different truthiness conventions**,
so unrecognized values fail silently *in inconsistent directions*:

- `KERNOS_SELF_MAINTENANCE_REVIEW=disabled` leaves the lane **ON**.
- `KERNOS_FRICTION_RESPONSE=enabled` leaves the lane **OFF**.

An operator who believes they disabled a daily background LLM call may not
have. The parity test **pins this as observed compatibility, not endorsement**.

Normalizing it is a behavior change to constitutional machinery and is tracked
as its own follow-on: a shared `env_flag(name, *, default)` helper with a closed
vocabulary that fails loudly on an unrecognized value, matching the
loud-fail-over-silent-degradation posture used on the metered provider wire.

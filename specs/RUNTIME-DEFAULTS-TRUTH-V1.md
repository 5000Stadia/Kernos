# RUNTIME-DEFAULTS-TRUTH-V1 — one enforced source of truth for lane defaults

**Status:** Draft rev 4 (kreview rounds 1–3; all must-fixes folded)
**Origin:** External doc-defect report from `jobhunting` (2026-08-04), verified against
`origin/main`. Third instance of the same class; the novelty report's own methodology
already disclosed four documentation-vs-code mismatches.
**Modules:** `kernos/kernel/governance_lanes.py` (new),
`docs/reference/runtime-defaults.md` (new), `tests/test_runtime_defaults_doc.py` (new),
`kernos/setup/bring_up_substrate.py`, `kernos/messages/handler.py` (`_handle_dump`,
line ~6154), `tests/test_handler.py`, `docs/kernos-introduction.md`,
`docs/TECHNICAL-ARCHITECTURE.md`, `docs/identity/about-kernos.md`.

## Why

The self-governance lane defaults are restated in at least four places. When
SELF-MAINTENANCE-REVIEW-V3 flipped Shape A to default-ON, only
`TECHNICAL-ARCHITECTURE.md` was swept. The introduction, the identity doc, and the
module's own docstring continued to assert that all lanes ship default-off.

The failure this induces is not "a stale paragraph." It is that **the documentation is
capable of producing a confident false public claim about the system's operational
maturity.** Two concrete consequences already observed:

1. A cold application email to a VP was about to state that the self-improvement loop has
   "rollback at boot, recursion limits in the database, and approval bound to the exact
   diff." Those properties belong to the *default-off* recursive-self-heal lane, not to the
   ordinary loop. The sentence was pulled before sending.
2. `docs/identity/about-kernos.md` is the identity document **the agent itself reads**. A
   wrong default there means KERNOS misdescribes its own posture to its own users.

`jobhunting` proposed a single referenced table. Necessary but not sufficient: a referenced
doc still rots, it just rots in one place instead of four, and prose cannot fail CI. This
spec pairs the table with an executable parity check. kreview confirmed this is justified
rather than over-engineering.

**Non-goals.** Documenting every environment variable in KERNOS (a claim that would itself
drift). Changing any runtime default. Normalizing flag-parsing semantics (see
**Deferred follow-on**).

## Part A — A production-owned lane registry

*Revised twice. Rev 1 compared the table to a test-local registry (proves membership only
against itself). Rev 2 claimed rendering the registry into operator diagnostics closed
code-only omission; kreview correctly refuted that too — a lane implemented entirely
outside the registry is absent from table, registry, **and** status, and every parity test
still passes. A missing line is not observably missing unless something else knows it
should be there.*

New module `kernos/kernel/governance_lanes.py`:

```python
@dataclass(frozen=True)
class GovernanceLane:
    key: str                     # stable machine key, e.g. "self_maintenance_review"
    title: str                   # human label for the table + diagnostics
    module: str                  # repo-relative path, e.g. "kernos/kernel/..."
    env_vars: tuple[str, ...]    # one, or two for the bring-up gate
    predicate: Callable[[], bool]   # the live is_enabled / enable predicate
```

`default_on` is **removed** per kreview must-fix 3 — it was a third independent declaration
of the same fact. The live default is whatever `predicate()` returns with the environment
cleared; the table is asserted against that, so there are exactly two declarations (doc and
code) and one comparison between them.

**The omission claim, stated honestly.** Taking kreview's narrower option rather than
mediating runtime activation through the registry: making an unregistered lane *unable to
run* would be a behavior change to constitutional startup machinery, which does not belong
as a rider on a documentation-integrity spec.

So the claim is: **the registry pins the declared set; it cannot discover a lane
implemented entirely outside the convention.** AC wording matches that and no stronger.

As a **best-effort heuristic guardrail** — kreview's round-3 ruling on the label, and
explicitly *not* an "equally enforceable discovery convention" nor a completeness
guarantee — a **convention-discovery test** walks `kernos/kernel/` and `kernos/setup/`
**recursively**, parsing each module with **AST** (not text matching) to find a
module-level `is_enabled()` that reads a `KERNOS_*` environment variable. Each hit must be
either in `GOVERNANCE_LANES` or in an exemption list where **every entry carries a written
reason**.

It catches the realistic case — a new lane written to the existing convention — and is
honest about what it misses: a lane that invents another shape. That limit is stated in a
comment at the test and in AC1.

The registry is still consumed by operator diagnostics (Part F) — useful, but no longer
load-bearing for the omission argument.

## Part B — Extract the autonomy-loop predicate

*Per kreview must-fix 2. Testing the bring-up row by grepping for two strings in the source
can pass while the boolean logic is inverted or one value is ignored.*

The gate currently lives inline in `bring_up_substrate.py` as
`if _architect_actor_id_si and _operator_actor_id_si:` with two `elif`/`else` skip branches.

Extract a pure predicate — `autonomy_loop_enabled(env: Mapping[str, str]) -> bool` — and
have bring-up call it. Pure, injectable, no I/O. Test the full matrix: neither set,
architect only, operator only, both set. Preserve the three existing log branches exactly;
this is a refactor of the condition, not of the behavior.

The table records this lane's effective default as **OFF**, not the non-boolean
"opt-in" — kreview is right that "opt-in" is a description, not a value a test can assert.

## Part C — The canonical table

New file `docs/reference/runtime-defaults.md`, scoped deliberately and narrowly to the
**self-governance lanes** — the drift-prone set where a wrong default becomes a false
public claim. kreview endorsed this scope and explicitly warned against expanding it into
an all-env-var registry. The scope statement appears prominently in the document.

**Machine-stable schema** (per kreview must-fix 4) so the parser is not ad hoc prose
scraping. One row per lane; the columns carry normalized values:

| Column | Format |
|---|---|
| `Key` | the `GovernanceLane.key`, in backticks |
| `Lane` | free prose (human label; not parsed) |
| `Default` | exactly `ON` or `OFF`, bold |
| `Env var(s)` | backticked names, comma-separated; two names allowed for the bring-up gate |
| `Enabled when` | one of the grammar forms below, in backticks |
| `Module` | repo-relative path from repo root, in backticks |

**`Enabled when` grammar** (per kreview must-fix 2 — rev 2 declared "a set literal" and then
wrote plain prose `both non-empty` in the `autonomy_loop` row, a contract the loud parser
could not implement). Exactly two forms are legal, and the parser **rejects anything else**
rather than skipping the row:

- **Single-variable membership:** `in {a,b,c}` or `not in {a,b,c}`. Bare `""` inside the set
  denotes the empty string.
- **Multi-variable conjunction:** `all_nonempty` — every env var in the row must be
  non-empty.

Seed rows (verified against code; note `kernos/` prefixes — kreview must-fix 3 caught that
rev 1's paths did not exist from repo root):

| Key | Lane | Default | Env var(s) | Enabled when | Module |
|---|---|---|---|---|---|
| `self_maintenance_review` | Daily self-maintenance review (Shape A) | **ON** | `KERNOS_SELF_MAINTENANCE_REVIEW` | `not in {0,false,off,no}` | `kernos/kernel/self_maintenance_review.py` |
| `friction_response` | Friction response (Shape B) | **OFF** | `KERNOS_FRICTION_RESPONSE` | `in {1,true,on,yes}` | `kernos/kernel/friction_response.py` |
| `recursive_self_heal` | Recursive self-heal | **OFF** | `KERNOS_RECURSIVE_SELF_HEAL` | `not in {"",0,false,no,off}` | `kernos/kernel/recursive_self_heal.py` |
| `autonomy_loop` | `improve_kernos` autonomy loop (bring-up) | **OFF** | `KERNOS_ARCHITECT_ACTOR_ID`, `KERNOS_OPERATOR_ACTOR_ID` | `all_nonempty` | `kernos/setup/bring_up_substrate.py` |

Each row also carries a one-line **"what it actually does when on"** so a reader cannot
repeat the conflation in consequence (1) above — specifically that the recursive-self-heal
lane is a bounded one-child repair on loop-machinery aborts, not a general self-repair
capability.

## Part D — The parity test

New `tests/test_runtime_defaults_doc.py`:

1. Parse the table per the Part C schema and grammar; **fail loudly** on any row that does
   not conform rather than silently skipping it.
2. **Set equality** between table keys and `GOVERNANCE_LANES` keys, both directions.
3. **Full-field parity** for every machine-owned field (kreview must-fix 3): table
   `Env var(s)` == `lane.env_vars` exactly, and table `Module` == `lane.module` exactly —
   not merely "some displayed path exists." Otherwise diagnostics metadata drifts while the
   test stays green.
4. For each lane, exercise `predicate` across a fixed value matrix — unset, `""`, `0`, `1`,
   `false`, `true`, `off`, `on`, `no`, `yes`, plus one unrecognized value — via
   `monkeypatch`. Assert the observed default (all env cleared) matches the `Default`
   column. With `default_on` removed, this is the single default comparison.
5. Assert every value the `Enabled when` grammar claims flips the lane actually does.
6. Assert each `Module` path exists **from repo root**.
7. For `autonomy_loop`, assert the extracted predicate over the four-way matrix from Part B.
8. The Part A convention-discovery test (best-effort, with its stated limit).

Points 2 and 4 close drift-by-omission-within-the-declared-set and
drift-by-contradiction — the two ways the current defect arose. Point 8 is a heuristic
supplement, not a completeness guarantee.

## Part F — Operator diagnostics surface

*Per kreview must-fix 4: "consumed by operator diagnostics" was an unscoped integration
assertion. Naming the exact target surfaced a mistake in rev 2.*

Rev 2 said `/status` operator view. **That surface no longer exists.**
SURFACE-DISCIPLINE-PASS D5 deliberately made `/status` a concise user-readable summary with
*"no internal identifiers, no file paths"* and moved the operator state view to `/dump`.
Rendering env-var names and module paths into `/status` would violate that discipline and
the layered-surface principle (operator gets receipts; the user gets the sentence).

Correct target: **`MessageHandler._handle_dump`** (`kernos/messages/handler.py:6154`), the
operator diagnostic surface that already writes internals to a diagnostic file. Add a
**Governance lanes** section rendering, per registry entry: key, title, env var(s), live
`predicate()` result, and module path.

Tests in `tests/test_handler.py`: the section renders every `GOVERNANCE_LANES` entry, and
reflects a monkeypatched flip of at least one lane (proving live state, not a static echo).

## Part E — Collapse the restatements

- `docs/kernos-introduction.md` and `docs/identity/about-kernos.md`: keep prose describing
  *what each lane is for*; replace every **defaults claim** with a link to the canonical
  table. Purpose prose is safe to restate; state is not.
- `docs/TECHNICAL-ARCHITECTURE.md` §11b: keep its detailed per-lane sections (this is the
  as-built reference), but have default/env-var assertions defer to the table.

The already-staged corrections from the `jobhunting` report stand; this part supersedes
them by removing the restatement rather than fixing it in place.

## Deferred follow-on (named, urgent) — flag-parsing normalization

The three lane flags use **three different truthiness conventions**:

- `KERNOS_SELF_MAINTENANCE_REVIEW` — deny-list; empty string means **ON**.
- `KERNOS_FRICTION_RESPONSE` — allow-list; empty string means **OFF**.
- `KERNOS_RECURSIVE_SELF_HEAL` — deny-list including empty string; empty means **OFF**.

Unrecognized values therefore fail silently *in inconsistent directions*.
`KERNOS_SELF_MAINTENANCE_REVIEW=disabled` leaves the lane **ON** — an operator who believes
they stopped a daily background LLM call has not. `KERNOS_FRICTION_RESPONSE=enabled` leaves
it **OFF**.

kreview agreed deferral is correct: normalization is a behavior change to constitutional
machinery and must not be smuggled into a documentation-integrity patch. Per their
instruction, Part D pins these semantics as **observed compatibility, not endorsement** —
the test comments say so explicitly, so the eventual fix reads as a deliberate change
rather than a test someone broke.

Recommended follow-on spec: one shared `env_flag(name, *, default)` helper with a closed
vocabulary that warns loudly (or fails) on an unrecognized value, matching the
loud-fail-over-silent-degradation posture already used on the metered provider wire.

## Acceptance criteria

1. `kernos/kernel/governance_lanes.py` declares all four lanes. **Claim boundary:** the
   registry pins the declared set and enforces doc↔code parity for it; it does **not**
   claim to discover a lane implemented entirely outside the `is_enabled()` convention.
   The convention-discovery test is a best-effort supplement with that limit stated in a
   comment.
2. `autonomy_loop_enabled` is a pure predicate; bring-up calls it; the three existing skip
   log branches are unchanged in behavior.
3. `docs/reference/runtime-defaults.md` exists, states its narrow scope prominently, and
   every row conforms to the Part C schema **and `Enabled when` grammar**; a
   non-conforming row fails the parser loudly.
4. `tests/test_runtime_defaults_doc.py` passes, and **fails** when: a default is edited in
   doc or code but not both; a lane is added to `GOVERNANCE_LANES` without a row; a row is
   added without a lane; a table `Module` or `Env var(s)` value diverges from the registry;
   a row uses an ungrammatical `Enabled when`. All verified by temporary mutation during
   implementation.
5. **Lane-scoped** documentation audit — a global `default-on`/`default-off` grep is
   unsatisfiable, since unrelated Kernos features legitimately use those words. Scoped to
   the four lane keys and their env var names, with the allowlist applying to *mentions*
   only (kreview round-3: an allowlist for default assertions would recreate the original
   drift):
   - **identifier/lane mentions** may be allowlisted as purpose prose;
   - **default/enablement assertions** for these four lanes may occur **only** in the
     canonical table — no allowlist, no exceptions;
   - links to the table are allowed, but no restated ON/OFF value and no restated
     truthiness semantics anywhere outside it.
6. `_handle_dump` renders a Governance lanes section from the registry, covered by
   `tests/test_handler.py` including a live-flip assertion.
7. No runtime default changes. Full `pytest` green on touched modules.

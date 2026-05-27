# USER-INITIATED-IMPROVEMENT-TRIGGER-V1

## Plain-English overview

This section is for human readers and for Kernos itself, doing
a perspective check before implementation begins. The spec
body that follows is the technical contract; this is what the
feature actually does in language a person can hold.

### The gap this closes

Today, the self-improvement loop only fires when accumulated
friction crosses a threshold. A pattern has to recur enough
times for the autonomy signal to trip. That works for the
slow-burn case — a recurring shape of failure that the
substrate notices over many turns. It does not work for the
case where the user notices a single failure and wants it
fixed now.

There is no first-class path for a user, in the moment, to
authorize a fix on first occurrence. The user has to either
wait for the threshold (slow, frustrating, sometimes never
crosses), or open an architect spec themselves (high friction,
breaks the conversational flow), or accept that the failure
sits in the substrate until something else escalates it.

This spec adds the path that should exist: user authority opens
the investigation immediately. "Just fix it" becomes a
first-class loop-initiation signal.

### The new flow, plainly

A scraper that used to work starts returning errors. The user
notices and writes "can you fix it please." That sentence
authorizes a substrate-style investigation that opens at the
moment the user speaks, not threshold-units later.

Kernos accesses its investigation tools — the coding-session
bridge, source-read tools, runtime trace, friction observer —
and figures out what changed. The investigation reaches far
enough to identify external causes (a page restructured, an
API response shape changed) as well as internal causes (a
Kernos bug). It drafts the fix. It tests the fix. Then it
applies the fix to its correct gate weight: a config tweak
applies and surfaces immediately; a substrate-tier code
change is brought to the architect at the standard gate.

The hard boundary stays exactly where it is. Investigation and
implementation run autonomously and fast — they always have.
The merge of any substrate-tier change is gated, always — that
is the load-bearing safety property. This spec does not
touch the gate; it adds the on-ramp.

### What the user sees differently

- A new slash command: `/fix [optional target]`. Says clearly
  "I authorize the investigation of this." Works with or
  without an explicit target — when used inline against a
  recent failure observation OR a Kernos-proposed fix
  proposal, the target is inferred from recent space context.
- A new Kernos-side initiation pattern. When Kernos notices
  something off in the course of conversation (a tool
  returned an unexpected error, an API call failed, an
  observation looks broken), Kernos can surface the
  observation with "would you like me to investigate a
  resolution?" The user's `/fix` (or natural "yes" via
  the v1.1 classifier) authorizes the same loop against
  that proposed target. Kernos becomes a proactive partner
  in repair without bypassing user authority: nothing
  investigates until the user authorizes.
- An immediate acknowledgement when the loop fires:
  "I'm investigating. I'll surface what I find." Not a stub
  message — a real signal that the loop is active.
- A resolution message when the loop completes: either "I
  fixed X — here's what I changed" (for config / external-
  cause fixes that don't need a gate) or "I drafted a patch
  for X — bringing it to architect review" (for substrate-
  tier fixes). Includes rollback affordance for the
  light-apply path: the diff is preserved on disk so the
  user can revert.

### What this opens up

User-initiated repair becomes a stable surface the user can
reach for. The threshold-based loop continues to do its work
on slow-burn patterns; this surface handles the moment-of-
failure case the threshold-based loop cannot reach. Together
they cover the full repair space: substrate notices on its
own AND user notices and authorizes.

A follow-up spec layer can add LLM-driven intent classification
so natural-language fix requests ("just fix it", "can you sort
that out") trigger the same path as the explicit slash command.
v1 ships the deterministic slash-command path because that's
the contract that has to hold against drift; the natural-
language layer rides on top of the same primitive.

---

## Why this spec exists

The autonomy loop's threshold-based trigger is the right
default for accumulating substrate friction. The threshold
filters noise from signal — a real pattern requires multiple
observations before it's worth investigating.

But user authority is a higher-confidence signal than any
threshold can be. When the user says "fix this", the
investigation should open immediately. Waiting for the
substrate to independently accumulate enough friction to
agree is wrong-shaped. The user knows the failure happened;
they're authorizing the work.

Three concrete scopes the architect identified:

1. **The trigger.** A conversational-request trigger (the
   deferred Spec 6 follow-up) that lets explicit user fix-
   authorization initiate the loop on first occurrence,
   not threshold. How does user authority register as a
   loop-initiation event? What distinguishes "just fix it"
   from ordinary conversation cleanly enough to be
   deterministic on the write path?

2. **Gate-weight classification.** A scraper repair might
   be config/data-level (selector update, retry, param
   change → light or no gate) or code-level to Kernos's own
   tooling (substrate-tier → full gate). Route each fix to
   its correct gate weight so a config tweak doesn't drag
   through full substrate ratification, while genuine
   substrate changes still hit the architect gate.

3. **The investigation reach.** "A previously-working tool
   now errors" requires actually diagnosing what changed,
   which is a real investigation task, not a pattern match.
   Confirm the coding-session bridge has enough reach to
   inspect a live failure and determine external causes, or
   scope what it needs.

The hard boundary that does not move: investigation and
implementation run autonomously and fast; the merge of any
substrate-tier change is gated. The loop cannot self-approve
its own substrate modifications.

---

## Audit findings (what exists, what's missing)

**Exists today (load-bearing for this spec):**

- `friction.pattern_frequency_threshold_exceeded` event +
  `self_improvement` workflow (the threshold-based loop).
  Architectural template the new workflow mirrors.
- `coding_session_bridge` primitives: `ask_coding_session`,
  `read_coding_session_response`. Real reach to claude_code +
  codex for investigation + implementation.
- `consult` tool: reads source, runs subprocess investigation,
  returns structured findings.
- `DispatchGate.classify_tool_effect` returns
  `read | soft_write | hard_write | unknown` — substrate-tier
  classification primitive the gate-weight router can extend.
- `SELF-IMPROVEMENT-CLOSURE-V1` machinery: ClosureAttempt,
  probe, invariant. Provides the closure pattern this spec
  can compose with for substrate-tier fixes that violate a
  known invariant.
- Slash command infrastructure: `/dump`, `/restart`, `/fix me`
  pattern. Adding `/fix` is a small surface addition.
- Approval-gate workflow primitive with `gate_ref` +
  `approval_event_predicate`. Architect gate already exists.

**Missing (what this spec ships):**

- `/fix` slash command + handler.
- `user.fix_authorization_received` event type with
  documented payload schema.
- `user_initiated_improvement.workflow.yaml` workflow
  definition.
- A fix-scope classifier (`classify_fix_scope`) that maps a
  proposed-fix description / diff to one of
  `external_only | config_data | substrate_tier`.
- A surfacing primitive that posts investigation-started and
  investigation-outcome messages to the source space.
- A kernel tool surface for the agent to record + look up
  fix authorizations.

---

## Design principles (load-bearing)

1. **User authority is a first-class loop-initiation signal.**
   The threshold-based path stays; the user-initiated path
   joins it. Neither replaces the other.

2. **Deterministic on the write path.** v1 ships the slash
   command as the only trigger source, recorded as
   `trigger_surface="slash:/fix"` on the event. The LLM-
   driven intent classifier (natural-language detection of
   "just fix it" shapes) is a v1.1 follow-up that MUST emit
   the same event shape through a reserved-verb-table
   schema check before posting; this spec pins the contract
   the classifier will adhere to.

3. **Gate weight scales with fix scope, not with who
   authorized it.** A user-authorized config fix still gets
   light gating; a user-authorized substrate change still
   gets the full architect gate. User authorization opens
   the investigation; it does not pre-approve the merge.

4. **The hard boundary does not move.** Substrate-tier
   changes (mutations to Kernos's own code, schema, or
   architecture) require architect approval at merge time,
   always. No "user said so" bypass. This is the load-
   bearing safety property and the spec must protect it.

5. **Fail-closed classification.** When the classifier
   cannot determine scope from the evidence CC returned
   (missing diff, malformed touches_paths, diff/paths
   mismatch, unknown in-repo path), the classifier MUST
   route to `substrate_tier` (the conservative side).
   Never `external_only` or `config_data` by default;
   missing evidence is treated as "could be substrate, gate
   it." Empty-everything (no diff, no paths, no
   external_action description) aborts the workflow with a
   surfaced error rather than applying anything.

6. **Diff is authoritative over self-reported paths.** When
   CC returns both a `proposed_fix_diff` and `touches_paths`,
   the classifier extracts paths from the diff itself and
   takes the UNION with self-reported paths. If the diff-
   derived paths and CC's self-reported paths disagree
   (CC says only `data/` but diff touches `kernos/`), the
   classifier picks the more conservative side. CC cannot
   smuggle a kernel mutation past the gate by misreporting
   touches_paths.

7. **Investigation reach is real.** External-cause
   identification (the scraper site changed, the API shape
   shifted) is part of the investigation. The coding-session
   bridge needs the right prompt + context to consider
   external causes, not just internal-Kernos causes. The
   investigation response is required to populate structured
   evidence fields, not just free-form text.

8. **Surfacing is the default, not the exception.** The user
   asked Kernos to fix something. The user wants to know
   what happened. Both investigation-started and
   investigation-outcome are surfaced to the source space
   as ordinary messages with rollback affordance metadata
   where applicable.

9. **Composes with closure machinery where applicable.**
   When the investigation identifies that the fix relates
   to a known friction pattern with a linked invariant
   (via `related_pattern_id` in the investigation
   response), the workflow composes with SELF-IMPROVEMENT-
   CLOSURE-V1: records a ClosureAttempt + runs the closure
   probe to verify the fix actually holds. When no pattern
   link, the workflow records `closure_outcome=
   no_invariant_fallback` per closure-v1 convention.

---

## New primitives

### `user.fix_authorization_received` event

Payload schema:

```python
{
    # Stable identifier for the authorization. Used to bind the
    # workflow execution to the source event and as the dedup
    # key for "fix already in progress for this target".
    "request_id": str,

    # Member who authorized the fix. Used by the workflow's
    # surfacing step to route messages back to the right
    # member's view.
    "requester_member_id": str,

    # The verbatim user message that triggered the
    # authorization. Carried through to the investigation
    # prompt so CC has the exact phrasing the user used.
    "request_text": str,

    # Optional explicit target hint. When the user said
    # "/fix the scraper", target_hint="the scraper". When
    # the user just said "/fix" (no arg), target_hint="" and
    # the workflow falls back to recent space context.
    "target_hint": str,

    # The space the authorization was issued in. Surfacing
    # routes back to this space.
    "source_space_id": str,

    # Recent failure context observed in this space. Lets
    # the investigation prompt include "here's what was
    # happening when the user authorized the fix" without
    # the workflow having to re-derive it. Bounded to the
    # last N events from the space's recent activity.
    "surfaced_context": list[dict],

    # ISO-8601 UTC. Carried for audit + dedup.
    "authorized_at": str,

    # Trigger-surface pin (Codex round-1 finding #2). v1
    # always sets this to the literal string "slash:/fix"
    # because the slash command is the only trigger path
    # shipped. The v1.1 LLM-classifier follow-up MUST also
    # set this field through its reserved-verb-table schema
    # check — emissions with missing / unrecognized
    # trigger_surface are rejected at the workflow trigger
    # selector (added as an event_selector predicate).
    "trigger_surface": str,
}
```

Emitted by:
- The `/fix` slash command handler in `kernos/messages/handler.py`
  (always emits `trigger_surface="slash:/fix"`).
- (v1.1 deferred) An LLM-driven intent classifier that
  observes turn messages and emits the same event shape
  when natural-language fix-request patterns are detected.
  v1.1 emissions MUST set `trigger_surface="classifier:<rule_id>"`
  through the reserved-verb-table contract this spec pins.

### `/fix` slash command

```
/fix                     # no arg — uses recent space context
/fix <target string>     # explicit target hint
```

Handler is added to the existing slash-command dispatch in
`kernos/messages/handler.py`. The handler:

1. Resolves `requester_member_id` from the message envelope.
2. Resolves `source_space_id` from the message envelope.
3. Generates `request_id = uuid4().hex`.
4. Collects `surfaced_context` from the space's recent N
   events (default N=10, configurable via
   `KERNOS_FIX_TRIGGER_CONTEXT_WINDOW`). Filters to events
   relevant to fix authorization: TOOL_ERROR, PROVIDER_ERROR,
   FRICTION_OBSERVED, the prior user message that prompted
   the fix request, AND any recent `surface_to_user` calls
   with `message_kind="fix_proposal"` (Kernos-proposed fix
   pending user authorization).
5. Proposal-recognition: when `target_hint` is empty AND
   the recent context contains a `fix_proposal` from
   Kernos, the handler:
   - Sets `target_hint` to the proposal's metadata
     `target_hint`.
   - Sets `trigger_surface = "slash:/fix:from_proposal"`
     (vs the default `"slash:/fix"`).
   - Adds `responding_to_proposal_id` to the event payload's
     metadata field so audit can trace back to the proposal.
6. Emits `user.fix_authorization_received` with the payload
   above via `event_stream.emit`.
7. Returns a short acknowledgement to the user (the
   workflow itself will surface deeper progress via its
   surfacing step).

### `classify_fix_scope` (new kernel function)

```python
@dataclass(frozen=True)
class FixScopeResult:
    scope: str                      # one of SCOPE_*
    gate_weight: str                # "no_gate" | "light" | "full"
    requires_architect_gate: bool   # native bool for workflow branching
    sensitive_path_detected: bool   # see SENSITIVE_PATH_PATTERNS
    sensitive_paths: list[str]      # paths under sensitive lattice
    diff_path_disagreement: bool    # touches_paths vs diff-derived
    derived_paths: list[str]        # union of self-reported + diff-derived
    reasoning: str                  # human-readable explanation


def classify_fix_scope(
    *,
    proposed_fix_summary: str,
    proposed_fix_diff: str | None = None,
    touches_paths: list[str] | None = None,
    external_action: str | None = None,
) -> FixScopeResult:
    """Classify a proposed fix's scope for gate-weight routing.

    FAIL-CLOSED contract (per design principle 5): when the
    evidence CC returned is missing or ambiguous, the result
    is ALWAYS substrate_tier. The classifier never returns
    external_only or config_data on partial / missing
    evidence.
    """
```

**Scope vocabulary (4 levels, narrower than v1 draft):**

```python
SCOPE_EXTERNAL_ONLY  = "external_only"  # no Kernos changes
SCOPE_CONFIG_DATA    = "config_data"    # safe runtime knobs / non-DB data files
SCOPE_SENSITIVE      = "sensitive"      # config-shaped but security/state-bearing
SCOPE_SUBSTRATE_TIER = "substrate_tier" # kernos/ source, specs/, workflow defs
```

`SCOPE_SENSITIVE` is new in v1.1 fold (Codex round 1 finding
#3) — it carves out the "config-shaped but DANGEROUS" class
of paths from the unsafe-default `config_data` bucket.

**Gate weight mapping:**

| Scope             | `gate_weight` | `requires_architect_gate` |
|-------------------|---------------|---------------------------|
| `external_only`   | `no_gate`     | False                     |
| `config_data`     | `light`       | False                     |
| `sensitive`       | `full`        | True                      |
| `substrate_tier`  | `full`        | True                      |

`sensitive` routes through the architect-gate path; the
distinction from `substrate_tier` is reported in the
surfacing metadata so the operator knows the gate was
triggered by sensitivity rather than substrate-mutation.

**Path lattice (precedence: sensitive > substrate > config > external):**

```python
# SENSITIVE: secrets, credentials, live state. Architect gate
# required regardless of whether the diff "looks like config."
SENSITIVE_PATH_PATTERNS = (
    ".env",
    "**/.env",
    ".credentials/**",
    "secrets/**",
    "data/**/*.db",          # live state — never auto-mutate
    "data/**/*.db-wal",
    "data/**/*.db-shm",
    "data/**/instance.db*",
    "data/**/kernos.db*",
)

# SUBSTRATE: Kernos's own code, specs, workflow definitions,
# build-system, top-level scripts. Architect gate required.
SUBSTRATE_PATH_PATTERNS = (
    "kernos/**",
    "specs/**",            # ALL specs including workflow YAMLs
    "tests/**",
    "pyproject.toml",
    "requirements.txt",
    "requirements*.txt",
    "*.workflow.yaml",     # workflow defs are substrate
    "start.sh",
    "scripts/**",
    "**/*.workflow.yaml",
    "docs/architecture/**", # canonical architecture docs
    "DECISIONS.md",
    "CLAUDE.md",
)

# CONFIG_DATA: safe runtime knobs + non-DB data adjustments.
# Light apply allowed.
CONFIG_DATA_PATH_PATTERNS = (
    "data/**/*.json",       # excluding .db, .db-wal, .db-shm
    "data/**/*.yaml",       # NOT *.workflow.yaml (substrate)
    "data/**/*.md",
    "data/**/*.txt",
    "data/**/*.log",        # log adjustments OK
)

# Anything under the repo not matching any of the above →
# UNKNOWN → fail-closed to substrate_tier.
```

**Classification algorithm:**

```python
def classify_fix_scope(...) -> FixScopeResult:
    # Step 1: extract diff-derived paths if a diff is provided.
    diff_paths = extract_paths_from_unified_diff(proposed_fix_diff)
    self_reported = list(touches_paths or [])
    derived = sorted(set(diff_paths) | set(self_reported))
    disagreement = bool(diff_paths) and bool(self_reported) \
        and set(diff_paths) != set(self_reported)

    # Step 2: empty-everything fail-closed.
    if not derived and not external_action and not proposed_fix_diff:
        return FixScopeResult(
            scope=SCOPE_SUBSTRATE_TIER,
            gate_weight="full", requires_architect_gate=True,
            sensitive_path_detected=False, sensitive_paths=[],
            diff_path_disagreement=False, derived_paths=[],
            reasoning=(
                "fail-closed: no diff, no paths, no external_action "
                "in investigation response — refusing to apply "
                "without evidence"
            ),
        )

    # Step 3: external-only path (no in-repo touches AND an
    # external_action description).
    if not derived and external_action:
        return FixScopeResult(
            scope=SCOPE_EXTERNAL_ONLY,
            gate_weight="no_gate", requires_architect_gate=False,
            sensitive_path_detected=False, sensitive_paths=[],
            diff_path_disagreement=False, derived_paths=[],
            reasoning=(
                f"external_only: no in-repo paths touched; "
                f"recommended external action recorded for user"
            ),
        )

    # Step 4: walk derived paths through the lattice
    # (sensitive precedence > substrate > config > unknown).
    sensitive_hits = [p for p in derived
                       if match_any(p, SENSITIVE_PATH_PATTERNS)]
    if sensitive_hits:
        return FixScopeResult(
            scope=SCOPE_SENSITIVE,
            gate_weight="full", requires_architect_gate=True,
            sensitive_path_detected=True,
            sensitive_paths=sensitive_hits,
            diff_path_disagreement=disagreement,
            derived_paths=derived,
            reasoning=(
                f"sensitive: paths in security/state lattice "
                f"{sensitive_hits}"
            ),
        )

    substrate_hits = [p for p in derived
                      if match_any(p, SUBSTRATE_PATH_PATTERNS)]
    if substrate_hits:
        return FixScopeResult(
            scope=SCOPE_SUBSTRATE_TIER,
            gate_weight="full", requires_architect_gate=True,
            sensitive_path_detected=False, sensitive_paths=[],
            diff_path_disagreement=disagreement,
            derived_paths=derived,
            reasoning=(
                f"substrate_tier: paths in Kernos source/specs "
                f"{substrate_hits}"
            ),
        )

    config_hits = [p for p in derived
                   if match_any(p, CONFIG_DATA_PATH_PATTERNS)]
    if config_hits and len(config_hits) == len(derived):
        # All paths are explicit config_data matches — no
        # unknowns in the set.
        return FixScopeResult(
            scope=SCOPE_CONFIG_DATA,
            gate_weight="light", requires_architect_gate=False,
            sensitive_path_detected=False, sensitive_paths=[],
            diff_path_disagreement=disagreement,
            derived_paths=derived,
            reasoning=(
                f"config_data: paths confined to safe runtime / "
                f"data adjustments {config_hits}"
            ),
        )

    # Step 5: any unknown-in-repo path → fail-closed to
    # substrate_tier per design principle 5.
    unknown = [p for p in derived
               if not match_any(p, CONFIG_DATA_PATH_PATTERNS)
               and not match_any(p, SUBSTRATE_PATH_PATTERNS)
               and not match_any(p, SENSITIVE_PATH_PATTERNS)]
    return FixScopeResult(
        scope=SCOPE_SUBSTRATE_TIER,
        gate_weight="full", requires_architect_gate=True,
        sensitive_path_detected=False, sensitive_paths=[],
        diff_path_disagreement=disagreement,
        derived_paths=derived,
        reasoning=(
            f"fail-closed substrate_tier: unknown in-repo paths "
            f"{unknown} — routing conservatively"
        ),
    )
```

**Diff-path extraction:**

```python
def extract_paths_from_unified_diff(diff: str | None) -> list[str]:
    """Parse a unified-diff string and return the set of file
    paths it modifies. Handles:
      - 'diff --git a/<path> b/<path>' headers
      - '+++ b/<path>' lines (for diffs without git headers)
      - '/dev/null' (skip — represents creation/deletion)
    Returns deduplicated sorted list. Empty on None / empty
    input. Never raises — malformed diffs return whatever
    paths were extractable, NOT empty (don't let a parser
    error become an unsafe-default).
    """
```

This is the load-bearing parser for principle 6: even if CC
self-reports `touches_paths=[]`, a non-empty diff that
touches `kernos/` produces diff-derived paths that route
substrate_tier. CC cannot smuggle a kernel mutation through.

### `record_fix_authorization` kernel tool

Workflow-callable. Persists the authorization to a
`fix_authorization` table for audit + dedup.

```python
async def record_fix_authorization(
    *,
    instance_id: str,
    request_id: str,
    requester_member_id: str,
    source_space_id: str,
    target_hint: str,
    request_text: str,
) -> dict:
    """Insert a fix_authorization row. Returns
    {"authorization_id": str, "newly_created": bool}.
    Idempotent on (instance_id, request_id) — second call
    returns the existing row's id."""
```

Gate classification: `soft_write` (bounded SQLite insert).

### `classify_fix_scope` kernel tool wrapper

Exposes the classifier to the workflow via `call_tool`.

```python
async def classify_proposed_fix(
    *,
    instance_id: str,
    proposed_fix_summary: str,
    proposed_fix_diff: str,
    touches_paths: list[str],
    external_action: str = "",
) -> dict:
    """Returns the full FixScopeResult fields as a dict:
        scope, gate_weight, requires_architect_gate,
        sensitive_path_detected, sensitive_paths,
        diff_path_disagreement, derived_paths, reasoning.
    """
```

Gate classification: `read` (pure classification, no writes).

### Investigation response schema (CC contract)

The `investigate` step's CC response MUST populate the
following structured fields (in addition to the existing
`investigation_outcome` field). The workflow's
`read_investigation_response` step validates the shape and
aborts the workflow with a surfaced error when required
fields are missing — preventing the workflow from advancing
to classification with malformed evidence.

```python
{
    # Existing field (from coding-session-bridge contract).
    "investigation_outcome": str,   # "completed" | "partial" | "unable_to_investigate"

    # NEW required fields for USER-INITIATED-IMPROVEMENT-TRIGGER-V1:

    "failure_mode": str,            # 1-line classification of WHAT broke
    "external_cause_evidence": str, # what suggests an external cause
    "internal_cause_evidence": str, # what suggests an internal cause
    "evidence_refs": list[str],     # log paths, runtime trace refs,
                                    # commit SHAs, etc.

    "proposed_fix_summary": str,    # 1-3 sentences
    "proposed_fix_diff": str,       # unified-diff format if internal;
                                    # empty if external_only
    "touches_paths": list[str],     # self-reported paths (the
                                    # classifier ALSO walks the diff
                                    # — diff is authoritative)
    "external_action": str,         # description for external_only;
                                    # empty if internal

    # OPTIONAL — composes with SELF-IMPROVEMENT-CLOSURE-V1:
    "related_pattern_id": str,      # friction_pattern_id this fix
                                    # remediates (empty when no
                                    # known pattern matches)
}
```

**Validation rules** (enforced by `read_investigation_response`):

1. `investigation_outcome` must be one of
   `{"completed", "partial", "unable_to_investigate"}`.
2. If `investigation_outcome == "completed"`: ALL of
   `failure_mode`, `proposed_fix_summary`, and AT LEAST ONE
   of `proposed_fix_diff` / `external_action` MUST be
   non-empty. Failure → workflow aborts with a surfaced
   "investigation_response_malformed" error.
3. If `investigation_outcome == "unable_to_investigate"`:
   workflow aborts with the investigation's report surfaced
   to user. No fix is applied.
4. `touches_paths` must be a list (possibly empty). Not a
   string, not None. The classifier handles the empty case
   safely; null/non-list is malformed → fail-closed abort.

### Closure composition (when applicable)

When the investigation response includes a non-empty
`related_pattern_id`, the workflow composes with
SELF-IMPROVEMENT-CLOSURE-V1 to verify the fix actually
holds:

1. `lookup_pattern_invariants` — find the invariant linked
   to this pattern.
2. If `has_invariants=True`: after the fix is applied
   (light) or ratified-and-applied (substrate-tier),
   `record_closure_attempt` + `run_closure_probe`. Pattern
   transitions to `resolved` on probe pass; stays in
   current state + `closure.probe_failed` event on probe
   fail.
3. If `has_invariants=False` or `related_pattern_id` is
   empty: workflow records `closure_outcome=
   no_invariant_fallback` in the outcome event payload —
   matches the convention SELF-IMPROVEMENT-CLOSURE-V1
   established.

This adds two optional workflow steps that only run when
the investigation links to a friction pattern.

### `maybe_run_closure_for_fix` kernel tool

Convenience orchestration: composes the three closure-v1
primitives (`lookup_pattern_invariants`,
`record_closure_attempt`, `run_closure_probe`) when the
investigation links the fix to a known friction pattern.
Keeps the workflow YAML flat instead of nesting branch
verbs.

```python
async def maybe_run_closure_for_fix(
    *,
    instance_id: str,
    related_pattern_id: str,    # "" when no pattern link
    active_epoch: int,          # pattern's current epoch
) -> dict:
    """Returns one of:
      - {"closure_outcome": "no_invariant_fallback",
         "closure_id": "", "invariant_id": ""}
        (when related_pattern_id is empty OR the pattern
        has no linked invariants)
      - {"closure_outcome": "passed" | "failed",
         "closure_id": str, "invariant_id": str}
        (when closure machinery composed and probe ran)
    """
```

Gate classification: `soft_write` (composes with
`run_closure_probe` which is soft_write — same wrapper-
level effect).

### `surface_to_user` kernel tool

Posts a structured message to a space.

```python
async def surface_to_user(
    *,
    instance_id: str,
    space_id: str,
    member_id: str,
    message_kind: str,    # see SURFACING_KINDS below
    body: str,
    metadata: dict,
) -> dict:
    """Routes the message through the same path the agent's
    own response would take. Returns {"surfaced_at": str}."""
```

Gate classification: `soft_write` (writes to channel; the
write is bounded + observable).

**Surfacing kinds (v1):**

```python
SURFACING_KINDS = frozenset({
    "investigation_started",   # workflow fired; investigating
    "investigation_outcome",   # workflow terminated; result
    "fix_proposal",            # Kernos proposes; user authorizes
                               # via /fix or v1.1 natural-yes
})
```

`fix_proposal` is the Kernos-side initiation surfacing kind.
Kernos uses this when it notices something off in the
course of conversation and wants to ask "would you like me
to investigate?" The body of a `fix_proposal` message
includes a `target_hint` in its metadata so that when the
user replies `/fix` (no arg) in the same space, the
slash-command handler's recent-context fallback picks up
the proposal and forwards the `target_hint` into the
`user.fix_authorization_received` event payload.

This makes Kernos-proposed-then-user-confirmed initiation
a first-class path WITHOUT adding a separate event type:
the same authorization event fires, just with
`trigger_surface="slash:/fix:from_proposal"` and the
metadata field `responding_to_proposal_id` so audit can
distinguish user-initiated from Kernos-proposed.

### `fix_authorization` table

```sql
CREATE TABLE fix_authorization (
    instance_id           TEXT NOT NULL,
    authorization_id      TEXT NOT NULL,
    request_id            TEXT NOT NULL,
    requester_member_id   TEXT NOT NULL,
    source_space_id       TEXT NOT NULL,
    target_hint           TEXT NOT NULL DEFAULT '',
    request_text          TEXT NOT NULL,
    authorized_at         TEXT NOT NULL,
    PRIMARY KEY (instance_id, authorization_id)
);

CREATE UNIQUE INDEX idx_fix_authorization_request_id
    ON fix_authorization (instance_id, request_id);
```

Lives in `instance.db` (same as the closure-v1 tables).

---

## Modified workflow: `user_initiated_improvement.workflow.yaml`

```yaml
workflow_id: user_initiated_improvement
instance_id: '{installer.instance_id}'
name: User-initiated improvement loop
description: |
  Opens an investigation immediately on explicit user fix
  authorization. Investigates via the coding-session bridge,
  classifies the proposed fix's scope, routes to the
  appropriate gate weight, and surfaces the outcome back to
  the source space.
version: "1.0"
owner: architect
instance_local: true

bounds:
  iteration_count: 1
  wall_time_seconds: 86400

verifier:
  flavor: deterministic
  check: terminated

triggers:
  - event_type: user.fix_authorization_received
    event_selector:
      op: AND
      operands:
        - op: eq
          path: event_type
          value: user.fix_authorization_received
        - op: eq
          path: instance_id
          value: '{installer.instance_id}'
        - op: exists
          path: payload.request_id

action_sequence:
  - id: record_authorization
    action_type: call_tool
    parameters:
      tool_id: record_fix_authorization
      args:
        request_id: '{idea_payload.request_id}'
        requester_member_id: '{idea_payload.requester_member_id}'
        source_space_id: '{idea_payload.source_space_id}'
        target_hint: '{idea_payload.target_hint}'
        request_text: '{idea_payload.request_text}'
    continuation_rules:
      on_failure: abort

  - id: surface_investigation_started
    action_type: call_tool
    parameters:
      tool_id: surface_to_user
      args:
        space_id: '{idea_payload.source_space_id}'
        member_id: '{idea_payload.requester_member_id}'
        message_kind: investigation_started
        body: 'I am investigating. I will surface what I find.'
        metadata:
          request_id: '{idea_payload.request_id}'
          target_hint: '{idea_payload.target_hint}'
    continuation_rules:
      on_failure: continue   # surfacing failure shouldn't abort

  - id: investigate
    action_type: call_tool
    parameters:
      tool_id: ask_coding_session_for_workflow
      args:
        target: claude_code
        question: |
          User authorized a fix via `/fix`. Their request:
            "{idea_payload.request_text}"
            target_hint: "{idea_payload.target_hint}"

          Investigate what is broken. Consider BOTH:
            - External causes (the target system changed:
              webpage restructured, API response shape
              shifted, third-party rate limit hit, etc.)
            - Internal causes (a Kernos bug, a stale
              dependency, a configuration drift in
              data/, etc.)

          When you identify the failure, draft a fix. Report:
            - proposed_fix_summary: 1-3 sentence summary
            - proposed_fix_diff: the actual diff if internal,
              or the recommended external action if external
            - touches_paths: list of file paths the fix
              modifies (empty list for external-only fixes)

          DO NOT apply substrate-tier changes yourself; this
          workflow routes substrate-tier changes through the
          architect gate.
        context:
          request_id: '{idea_payload.request_id}'
          surfaced_context: '{idea_payload.surfaced_context}'
          _workflow_execution_id: '{workflow.execution_id}'
          _workflow_gate_nonce: '{workflow.gate_nonce}'
    continuation_rules:
      on_failure: abort
    gate_ref: await_investigation_response

  - id: read_investigation_response
    action_type: call_tool
    parameters:
      tool_id: read_coding_session_response_for_workflow
      args:
        request_id: '{step.investigate.value.request_id}'
    continuation_rules:
      on_failure: abort

  # NEW v1.1 fold: validate the investigation response shape
  # BEFORE the classifier runs. Aborts with a surfaced error
  # when required fields are missing — closes the
  # malformed-response loophole Codex finding #1 + #10
  # flagged. Tool returns {"valid": true} on pass, raises
  # on failure (workflow aborts via continuation_rules).
  - id: validate_investigation_response
    action_type: call_tool
    parameters:
      tool_id: validate_investigation_response
      args:
        investigation_outcome: '{step.read_investigation_response.value.investigation_outcome}'
        failure_mode: '{step.read_investigation_response.value.failure_mode}'
        proposed_fix_summary: '{step.read_investigation_response.value.proposed_fix_summary}'
        proposed_fix_diff: '{step.read_investigation_response.value.proposed_fix_diff}'
        external_action: '{step.read_investigation_response.value.external_action}'
        touches_paths: '{step.read_investigation_response.value.touches_paths}'
    continuation_rules:
      on_failure: abort

  - id: classify_scope
    action_type: call_tool
    parameters:
      tool_id: classify_proposed_fix
      args:
        proposed_fix_summary: '{step.read_investigation_response.value.proposed_fix_summary}'
        proposed_fix_diff: '{step.read_investigation_response.value.proposed_fix_diff}'
        touches_paths: '{step.read_investigation_response.value.touches_paths}'
        external_action: '{step.read_investigation_response.value.external_action}'
    continuation_rules:
      on_failure: abort

  - id: branch_on_gate_weight
    action_type: branch
    parameters:
      condition: '{step.classify_scope.value.requires_architect_gate}'
      branch_on_true: request_architect_gate
      branch_on_false: 'terminal:light_apply:apply_fix'
    continuation_rules:
      on_failure: abort

  # ─── SUBSTRATE-TIER PATH (main sequence continues) ─────────
  - id: request_architect_gate
    action_type: call_tool
    parameters:
      tool_id: ask_coding_session_for_workflow
      args:
        target: claude_code
        question: |
          Substrate-tier patch from user-initiated fix
          authorization. Request architect ratification.

          Proposed fix:
            {step.read_investigation_response.value.proposed_fix_summary}

          Diff:
            {step.read_investigation_response.value.proposed_fix_diff}

          Files touched:
            {step.read_investigation_response.value.touches_paths}

          User who authorized:
            {idea_payload.requester_member_id}
        context:
          request_id: '{idea_payload.request_id}'
          _workflow_execution_id: '{workflow.execution_id}'
          _workflow_gate_nonce: '{workflow.gate_nonce}'
    continuation_rules:
      on_failure: abort
    gate_ref: await_architect_ratification

  - id: read_architect_response
    action_type: call_tool
    parameters:
      tool_id: read_coding_session_response_for_workflow
      args:
        request_id: '{step.request_architect_gate.value.request_id}'
    continuation_rules:
      on_failure: abort

  # Closure composition (v1.1 fold): if the investigation
  # linked the fix to a known friction pattern with a known
  # invariant, run the closure probe to verify the fix
  # actually holds. No-op when no pattern link.
  - id: maybe_closure_substrate_path
    action_type: call_tool
    parameters:
      tool_id: maybe_run_closure_for_fix
      args:
        related_pattern_id: '{step.read_investigation_response.value.related_pattern_id}'
        active_epoch: '{step.read_investigation_response.value.related_pattern_active_epoch}'
    continuation_rules:
      on_failure: continue

  - id: surface_architect_outcome
    action_type: call_tool
    parameters:
      tool_id: surface_to_user
      args:
        space_id: '{idea_payload.source_space_id}'
        member_id: '{idea_payload.requester_member_id}'
        message_kind: investigation_outcome
        body: |
          Investigation complete. The fix touches Kernos
          substrate, so I brought the patch to the architect
          gate. Result: {step.read_architect_response.value.investigation_outcome}.
          Summary: {step.read_investigation_response.value.proposed_fix_summary}
          Closure: {step.maybe_closure_substrate_path.value.closure_outcome}
        metadata:
          request_id: '{idea_payload.request_id}'
          authorization_id: '{step.record_authorization.value.authorization_id}'
          target_hint: '{idea_payload.target_hint}'
          scope: '{step.classify_scope.value.scope}'
          sensitive_path_detected: '{step.classify_scope.value.sensitive_path_detected}'
          sensitive_paths: '{step.classify_scope.value.sensitive_paths}'
          touches_paths: '{step.classify_scope.value.derived_paths}'
          architect_outcome: '{step.read_architect_response.value.investigation_outcome}'
          closure_outcome: '{step.maybe_closure_substrate_path.value.closure_outcome}'
          closure_id: '{step.maybe_closure_substrate_path.value.closure_id}'
          invariant_id: '{step.maybe_closure_substrate_path.value.invariant_id}'
    continuation_rules:
      on_failure: continue

# ─── TERMINAL BRANCH: light_apply (config_data / external_only) ─
terminal_branches:
  light_apply:
    - id: apply_fix
      action_type: call_tool
      parameters:
        tool_id: ask_coding_session_for_workflow
        args:
          target: claude_code
          question: |
            Apply the proposed fix (config_data or external_only
            scope — no substrate-tier changes). Report applied=true
            on success.

            Proposed fix:
              {step.read_investigation_response.value.proposed_fix_summary}
            Diff:
              {step.read_investigation_response.value.proposed_fix_diff}
          context:
            request_id: '{idea_payload.request_id}'
            _workflow_execution_id: '{workflow.execution_id}'
            _workflow_gate_nonce: '{workflow.gate_nonce}'
      continuation_rules:
        on_failure: abort
      gate_ref: await_light_apply_response

    - id: read_apply_response
      action_type: call_tool
      parameters:
        tool_id: read_coding_session_response_for_workflow
        args:
          request_id: '{step.apply_fix.value.request_id}'
      continuation_rules:
        on_failure: abort

    # Closure composition on light-apply path too.
    - id: maybe_closure_light_path
      action_type: call_tool
      parameters:
        tool_id: maybe_run_closure_for_fix
        args:
          related_pattern_id: '{step.read_investigation_response.value.related_pattern_id}'
          active_epoch: '{step.read_investigation_response.value.related_pattern_active_epoch}'
      continuation_rules:
        on_failure: continue

    - id: surface_light_outcome
      action_type: call_tool
      parameters:
        tool_id: surface_to_user
        args:
          space_id: '{idea_payload.source_space_id}'
          member_id: '{idea_payload.requester_member_id}'
          message_kind: investigation_outcome
          body: |
            Investigation complete. Fix applied directly
            ({step.classify_scope.value.scope} scope, no
            architect gate needed).

            Summary:
              {step.read_investigation_response.value.proposed_fix_summary}

            Files touched:
              {step.classify_scope.value.derived_paths}

            Closure: {step.maybe_closure_light_path.value.closure_outcome}

            To roll this back, see the apply response —
            CC's diff is preserved at:
              data/<instance>/diagnostics/fix_authorizations/{idea_payload.request_id}/
          metadata:
            request_id: '{idea_payload.request_id}'
            authorization_id: '{step.record_authorization.value.authorization_id}'
            target_hint: '{idea_payload.target_hint}'
            scope: '{step.classify_scope.value.scope}'
            touches_paths: '{step.classify_scope.value.derived_paths}'
            diff_summary_hash: '{step.read_apply_response.value.diff_summary_hash}'
            apply_outcome: '{step.read_apply_response.value.investigation_outcome}'
            closure_outcome: '{step.maybe_closure_light_path.value.closure_outcome}'
            closure_id: '{step.maybe_closure_light_path.value.closure_id}'
            invariant_id: '{step.maybe_closure_light_path.value.invariant_id}'
            rollback_artifact_dir: 'data/<instance>/diagnostics/fix_authorizations/{idea_payload.request_id}/'
      continuation_rules:
        on_failure: continue

approval_gates:
  - gate_name: await_investigation_response
    pause_reason: awaiting investigation response
    approval_event_type: coding_consult.response_received
    approval_event_predicate:
      op: eq
      path: payload.request_id
      value: '{step.investigate.value.request_id}'
    timeout_seconds: 86400
    bound_behavior_on_timeout: abort_workflow

  - gate_name: await_architect_ratification
    pause_reason: awaiting architect ratification of substrate patch
    approval_event_type: coding_consult.response_received
    approval_event_predicate:
      op: eq
      path: payload.request_id
      value: '{step.request_architect_gate.value.request_id}'
    timeout_seconds: 86400
    bound_behavior_on_timeout: abort_workflow

  - gate_name: await_light_apply_response
    pause_reason: awaiting light-apply response
    approval_event_type: coding_consult.response_received
    approval_event_predicate:
      op: eq
      path: payload.request_id
      value: '{step.apply_fix.value.request_id}'
    timeout_seconds: 86400
    bound_behavior_on_timeout: abort_workflow
```

---

## Acceptance criteria

**AC1 — `/fix` slash command emits the event.** Sending
`/fix` (no arg) and `/fix the scraper` (with arg) both emit
`user.fix_authorization_received` with the payload shape
documented above. The `target_hint` field carries the verbatim
post-`/fix` text or empty string when no arg.

**AC2 — `fix_authorization` table + record tool.** Inserting
a row succeeds; idempotent on `(instance_id, request_id)`
(second call returns same `authorization_id` with
`newly_created=False`). Unique index on `request_id` blocks
two authorizations with the same `request_id`.

**AC3 — Workflow triggers on the event.** Workflow registry
contains `user_initiated_improvement`; emitting
`user.fix_authorization_received` with the right
`instance_id` + `payload.request_id` fires a workflow
execution whose first step is `record_authorization`.

**AC4 — Workflow surfaces investigation_started.** Step
`surface_investigation_started` runs after `record_authorization`
and posts a message of kind `investigation_started` to the
source space, addressed to the requester member.

**AC5 — Investigation prompt includes external-cause guidance.**
The `investigate` step's `question` parameter contains explicit
text directing CC to consider both external and internal
causes. (Pin against the YAML literal so a regression that
drops external-cause guidance is caught.)

**AC6 — `classify_fix_scope` routes correctly.**
- `touches_paths=["kernos/kernel/foo.py"]` → `substrate_tier`,
  `requires_architect_gate=True`.
- `touches_paths=["data/config.yaml"]` → `config_data`,
  `requires_architect_gate=False`.
- `touches_paths=[]` + `proposed_fix_summary="update the
  scraper selector"` → `external_only`,
  `requires_architect_gate=False`.
- Mixed `touches_paths=["data/foo.yaml", "kernos/x.py"]` →
  `substrate_tier` (conservative — any kernel/ touch wins).
- `specs/workflows/*.yaml` touched → `substrate_tier`
  (workflow defs are substrate).

**AC7 — Branch routes to substrate path on substrate_tier.**
`classify_scope` returning `requires_architect_gate=True` →
`branch_on_gate_weight` routes to `request_architect_gate`
(main sequence continues). `False` → routes to
`terminal:light_apply:apply_fix` (terminal branch).

**AC8 — Architect-gate path requires the full gate.**
Substrate-tier path goes through `await_architect_ratification`
approval gate before any application. The gate predicate
binds to `step.request_architect_gate.value.request_id`.

**AC9 — Light-apply path does NOT request architect gate.**
Light-apply terminal branch goes through
`await_light_apply_response` (just the response-read gate,
not an architect-ratification gate). The architect's
`approval_event_type` is the same `coding_consult.response_received`
but the predicate request_id differs from
`await_architect_ratification`.

**AC10 — Surfacing kind is preserved.** Both surface_to_user
calls receive distinct `message_kind` values:
`investigation_started` (after authorization) and
`investigation_outcome` (after the path's terminal step).
Metadata on the outcome message includes `scope` plus the
path-specific outcome reference.

**AC11 — `surface_to_user` failure does not abort workflow.**
Both surfacing steps use `continuation_rules: on_failure:
continue` so a transient surfacing failure (channel down,
member not addressable) doesn't prevent the fix from
landing. Workflow logs the surfacing failure and proceeds.

**AC12 — `target_hint` empty falls back to recent context.**
When `/fix` is invoked with no arg, the surfaced_context list
in the payload is non-empty (collected from the source space's
recent N events). The investigation prompt receives this
context so CC has enough to work with even without an
explicit target.

**AC13 — Hard boundary preserved.** When the proposed fix
touches kernos/ source, the workflow MUST reach the
`await_architect_ratification` gate before applying. There
is no path in the YAML that bypasses this gate for
substrate-tier scope. (Static analysis on the workflow YAML:
no branch target reachable from `branch_on_gate_weight=True`
applies a fix without first hitting the architect gate.)

**AC14 — Dedup: same request_id is a no-op.** Two emits of
`user.fix_authorization_received` with the same
`(instance_id, request_id)` produce exactly one workflow
execution. (The workflow's `record_authorization` step is
idempotent; the engine's existing trigger-dedup machinery
prevents double-firing.)

**AC15 — Event payload fields documented in source.** The
event schema is exported from a Python module
(`kernos/kernel/user_fix_trigger.py` or similar) as a
constant + dataclass so future spec authors don't have to
reverse-engineer the shape from YAML refs.
`trigger_surface` field present + set to literal
`"slash:/fix"` for all v1 emissions.

**AC16 — Fail-closed: empty-everything CC response aborts.**
Investigation response with empty `proposed_fix_diff`,
empty `external_action`, AND empty `touches_paths` and
`investigation_outcome="completed"` → workflow aborts at
`validate_investigation_response` with surfaced
"investigation_response_malformed" error. No `apply_fix`,
no architect-gate request, no surfaced
investigation_outcome.

**AC17 — Fail-closed: classifier walks diff regardless of
self-reported paths.** CC returns `touches_paths=[]` but
`proposed_fix_diff` contains `diff --git a/kernos/foo.py
b/kernos/foo.py` → classifier extracts `kernos/foo.py`
from the diff, routes `substrate_tier`,
`requires_architect_gate=True`. The CC self-report cannot
smuggle a kernel mutation past the gate.

**AC18 — Fail-closed: diff/paths disagreement picks
conservative side.** CC returns
`touches_paths=["data/foo.json"]` AND
`proposed_fix_diff` touches `kernos/bar.py` → derived_paths
union → substrate hit → routes substrate_tier;
`diff_path_disagreement=True` reported in classifier
result + carried into surfacing metadata.

**AC19 — Fail-closed: unknown in-repo path → substrate_tier.**
`touches_paths=["random/unknown/path.txt"]` (not under any
lattice pattern) → routes `substrate_tier`,
`requires_architect_gate=True`, `reasoning` includes
"fail-closed substrate_tier: unknown in-repo paths".

**AC20 — Fail-closed: malformed touches_paths.** Workflow
receives `touches_paths=None` / `touches_paths="single
string"` / `touches_paths=42` → `validate_investigation_response`
detects non-list and aborts the workflow with surfaced
error. Classifier is never called with malformed input.

**AC21 — Sensitive-path lattice routes to architect gate.**
Each of these `touches_paths` lists routes
`scope="sensitive"`, `requires_architect_gate=True`,
`sensitive_path_detected=True`:
- `[".env"]`
- `[".credentials/openai-codex.json"]`
- `["data/discord_123/instance.db"]`
- `["secrets/foo"]`
Even though some of these are "config-shaped", the
sensitive lattice wins because of secret/state-bearing
content.

**AC22 — Substrate-path lattice catches Codex round-1 gaps.**
Each of these routes substrate_tier:
- `["pyproject.toml"]`
- `["requirements.txt"]`
- `["specs/workflows/foo.workflow.yaml"]`
- `["scripts/manage-kernos-service.sh"]`
- `["DECISIONS.md"]`
- `["CLAUDE.md"]`

**AC23 — Closure composition: no link → no_invariant_fallback.**
Investigation response with `related_pattern_id=""` →
`maybe_run_closure_for_fix` returns
`{closure_outcome: "no_invariant_fallback", closure_id: "",
invariant_id: ""}`. emit_outcome surfacing carries
`closure_outcome: no_invariant_fallback`. No
`record_closure_attempt` call; no probe run.

**AC24 — Closure composition: linked pattern + invariant
runs the probe.** Investigation response with
`related_pattern_id="provider-error-repeated"` (assuming
this pattern is linked to an invariant in the fixture's
test instance) → `maybe_run_closure_for_fix` invokes
`lookup_pattern_invariants` → finds invariant →
`record_closure_attempt` → `run_closure_probe`. probe
result lands in surfacing metadata as
`closure_outcome=passed | failed`, `closure_id=<id>`,
`invariant_id=<id>`.

**AC25 — Investigation response schema validation rejects
malformed shapes.** Each of these aborts the workflow at
`validate_investigation_response`:
- Missing `failure_mode` when outcome="completed"
- Both `proposed_fix_diff` and `external_action` empty
  when outcome="completed"
- `investigation_outcome` value outside the allowed enum
- `touches_paths` not a list

**AC26-prop — Kernos-proposed fix is a recognized
initiation path.** Test:
1. Kernos calls `surface_to_user` with
   `message_kind="fix_proposal"` + metadata
   `{proposal_id, target_hint}`.
2. User replies `/fix` (no arg) in the same space within
   the recent-context window.
3. Slash handler's recent-context fallback finds the
   `fix_proposal` and emits
   `user.fix_authorization_received` with:
     - `trigger_surface = "slash:/fix:from_proposal"`
     - `target_hint` = proposal's target_hint
     - metadata reference to `responding_to_proposal_id`.
4. The workflow fires with the right target.

**AC27 — Hard-boundary preservation (architect gate
mandatory for substrate-tier).** Static-analysis pin on
the workflow YAML: any path reachable from
`branch_on_gate_weight=True` (substrate_tier path) MUST
include `await_architect_ratification` as a gate before
any step whose tool_id applies code changes. There is no
YAML edit that can ship a substrate-tier fix without
hitting that gate. Test asserts this by parsing the YAML
and walking the action_sequence DAG.

**AC28 — Sensitive scope still triggers architect gate but
labels distinctly.** `scope="sensitive"` routes through
the SAME `await_architect_ratification` path as
`scope="substrate_tier"`, but the surfacing metadata
distinguishes them via `scope` + `sensitive_path_detected`
+ `sensitive_paths` fields. Operator can see at a glance
that the gate fired due to sensitivity rather than
substrate mutation (different operational disposition).

---

## Out of scope (deferred)

- **LLM-driven intent classifier** for natural-language fix
  requests ("just fix it", "can you sort that out"). Lands
  as `USER-INITIATED-IMPROVEMENT-CLASSIFIER-V1` after
  operator evidence informs the right detection rules.
  v1's slash command is the substrate hook the classifier
  will emit into.

- **Recovery from fix-application failure.** If the
  light-apply path's `apply_fix` step fails mid-application
  (CC starts the change, can't complete), recovery semantics
  live in `IMPROVEMENT-LOOP-RECOVERY-V1` (already parked).
  v1 aborts the workflow and surfaces the failure to the
  user; recovery is a separate concern.

- **Multi-user authorization arbitration.** When multiple
  members fire `/fix` against overlapping targets in
  parallel, v1 lets both workflow executions run; the
  per-`request_id` dedup blocks exact duplicates but does
  not coordinate across distinct request_ids. Cross-member
  arbitration is a follow-up.

- **Pre-flight cost estimation.** v1 doesn't tell the user
  "this investigation will cost ~$X". Cost surfacing is a
  cross-cutting concern for ALL CC-bridged workflows
  (closure, threshold-loop, this one) and belongs in its
  own spec.

- **Auto-rollback for light-apply fixes that don't actually
  fix.** v1 ships a USER-visible rollback affordance for
  light-apply: the apply step's diff is preserved at
  `data/<instance>/diagnostics/fix_authorizations/<request_id>/`,
  surfacing metadata carries `diff_summary_hash` and
  `rollback_artifact_dir`, and the closure-composition step
  reports `closure_outcome=passed | failed` when an
  invariant is linked. AUTOMATIC rollback on probe-fail
  (revert the applied diff without operator action) is
  deferred to follow-up sub-spec —  the v1 path lets the
  operator manually revert using the preserved diff +
  probe-failed evidence.

- **Target-level in-flight dedup.** v1 dedupes exact
  `request_id` duplicates (engine-level + AC14 +
  fix_authorization unique index). Cross-`request_id`
  target-fingerprint dedup (e.g. two users firing `/fix
  the scraper` within 60s) is a v1.1 follow-up; v1 lets
  both workflow executions run.

- **LLM-classifier-emitted natural-language trigger.** v1
  pins the contract the classifier MUST follow
  (`trigger_surface="classifier:<rule_id>"` + reserved-verb-
  table schema check before emission); the classifier
  implementation itself ships as
  `USER-INITIATED-IMPROVEMENT-CLASSIFIER-V1` follow-up.

- **Restart resume semantics.** v1 relies on the existing
  engine resume-after-restart machinery (closure-v1 and
  self_improvement workflows already exercise it). No new
  restart-resume primitives in this spec; the workflow's
  three approval gates inherit the engine's existing
  gate-resume behavior. A future follow-up may add
  per-workflow restart-resume tests if needed.

---

## Test plan (scoped per [[feedback-test-scope-proposal]])

New tests:

- `tests/test_fix_trigger_slash_command.py` — `/fix` handler
  unit tests: payload assembly, target_hint extraction, event
  emission, recent-context collection. (AC1, AC12, AC15.)

- `tests/test_fix_authorization_store.py` — table + record
  tool CRUD, idempotent retry semantics, unique index on
  request_id. (AC2.)

- `tests/test_classify_fix_scope.py` — classifier routes
  every documented `touches_paths` shape correctly; keyword
  fallback when paths absent. (AC6.)

- `tests/test_user_initiated_improvement_workflow.py` — YAML
  parses; both paths reach their terminal step under
  fakeable substrate; gate predicates bind correctly;
  surfacing-failure continuation works. (AC3, AC4, AC5, AC7,
  AC8, AC9, AC10, AC11, AC13, AC14.)

Regression touch:
- `tests/test_self_improvement_workflow.py` — unchanged; this
  spec adds a new workflow alongside, doesn't modify the
  threshold-based one.
- `tests/test_kernel_tool_registry_parity.py` — extends with
  three new kernel tools.
- `tests/test_dispatch_gate.py` — extends with three new
  classifications.

---

## Open questions for architect ratification

1. **Should `/fix` without arg refuse, or fall back to
   recent context?** Spec defaults to "fall back to recent
   context" (more forgiving, matches user's intuitive-
   sophistication framing). Alternative: refuse with a
   prompt like "Please specify what to fix." Architect call.

2. **Should the workflow auto-suppress when a same-target
   fix is already in-flight?** Spec defaults to "no, let
   both run" (per-request_id dedup is the only coordination).
   Architect call on whether target-level dedup is needed
   in v1 or fine as a follow-up.

3. **Should investigation_started surface be a DM or a
   channel message?** Spec defaults to channel (uses the
   normal response path). Architect call if DM-by-default
   is preferable for high-noise channels.

4. **Should the architect-gate path's `surface_to_user`
   message wait for the architect's decision (current
   shape) or fire immediately on gate-entry?** Spec defaults
   to "wait for decision" so the user sees the final
   outcome, not a stub. Architect call if early-surfacing
   ("I'm at the gate now") is preferable.

5. **Is the `external_only` scope sufficient as a label, or
   should it record the recommended external action somewhere
   the user can act on it later?** Spec defaults to
   recording the recommendation in the
   `investigation_outcome` message body. Architect call if
   structured persistence (e.g., a follow-up trigger) is
   needed.

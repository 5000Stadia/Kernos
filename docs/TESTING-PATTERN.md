# Testing pattern — substrate-fidelity assertion shape

**Standard for new tests:** every test pins a real architectural
invariant via two-class assertion. Coverage without invariants is
anti-pattern.

The standard codifies the lesson from the COGNITIVE-CONTEXT-V1
audit: a 5,200-test green suite was insufficient to catch 17 of 19
substrate classes silently dropping on the production decoupled path.
The structural blind spot was that tests pinned "code ran without
exception," not "the right substrate reached the model." Future
work avoids that failure mode by following the pattern below.

## The pattern

Every test, soak scenario, or verification probe asserts in two
classes:

1. **Behavioral signal in the console / event stream / audit log.**
   The right tool / event / log line surfaced. Confirms the right
   function ran. Examples:
   * `FILE_WRITE.*_procedures\.md` regex against captured stdout.
   * `audit_category=cohort.fan_out` field against the audit JSON.
   * `source=reasoning_service.turn_runner` event source — proves
     the decoupled path executed (the synthetic emitter only fires
     from `_run_via_turn_runner_provider`).

2. **Substrate state in the packet / context / dump / model-call args.**
   The right substrate is present where it should be (and absent
   where it shouldn't). Examples:
   * `## PROCEDURES` substring in the `/dump` system prompt.
   * `request_tool` in the tools list passed to the model.
   * `RELATIONSHIPS:` line + non-default permission text in
     `## STATE`.

Both classes together identify the function or feature, confirm it
remains intact, and confirm it works in the way expected.

## Where this pattern lives in the codebase

* `tests/test_cognitive_context_contract.py` — 14 contract tests.
  Each captures the model-call seam (system=, tools=, messages=)
  and asserts on substrate content. Behavioral class is implicit
  (the call happened); substrate class is the primary pin.
* `tests/test_cognitive_context_seam.py` — 5 seam tests. Each
  passes a sentinel packet and asserts identity preservation
  through the ReasoningRequest → TurnRunnerInputs →
  IntegrationInputs → Briefing chain.
* `tests/test_pdi_equivalence.py` — equivalence-suite extensions
  (PDI C7 + CCV1 C6). Three input-fidelity dimensions assert
  cross-path substrate equivalence as a structural protection
  for future migrations.
* `tests/test_repl_boot_smoke.py` — 8 boot smoke tests. Each
  exercises the real boot path (`build_dev_handler`) with a mock
  provider so boot-time issues can't hide behind unit-test mocks.
* `kernos/soak.py` — operator-runnable soak harness. Each
  `Scenario` declares console assertions (regex against captured
  log) and dump assertions (substring/regex against `/dump`
  output). Real provider, real boot, real path.

## When to apply

**For new tests:** ask if the test would still pass after deleting
the assertion line. If yes, the assertion isn't pinning anything
— add a real one. Prefer the model-call seam or the typed packet
over internal handler state when verifying substrate fidelity.

**For new soak scenarios:** add a `Scenario` to `kernos/soak.py`'s
`SCENARIOS` list. Declare the input lines, the console patterns
that must appear (or must not), and the dump content that must
appear. Set `automated=False` if the scenario needs operator
interaction (Discord, OAuth, multi-member setup).

**When folding a Codex BLOCKER:** add a NEW pin test that
captures the specific failure mode so it can never regress
silently. Every CCV1 BLOCKER-fold commit shipped with new pin
tests; preserve that pattern.

**When reviewing existing tests:**
* If the test mocks the seam it's supposed to verify, the test
  isn't verifying that seam — it's verifying the mock.
* If the test asserts only on user-facing response text, it's
  not pinning substrate fidelity (response variance under model
  variance defeats it).
* If the test runs without ever checking what reached the model,
  it's not pinning the substrate-fidelity invariant.

## Soak harness usage

```
# List available scenarios
./scripts/run-soak.sh --list

# Run all automated scenarios end-to-end
./scripts/run-soak.sh

# Run a specific scenario
./scripts/run-soak.sh --scenario probe_c_procedures

# Run only automated (skip operator-driven)
./scripts/run-soak.sh --auto-only
```

Each run produces an artifact directory at
`data/soak-runs/<timestamp>/` with per-scenario log files,
`/dump` snapshots, a JSON results blob, and a markdown summary
report. Exit code is 0 when every automated scenario passes;
non-zero when any fails. Operator-driven scenarios are listed
but skipped in automation mode.

## Architectural enforcement

Per the design review's CCV1 closure verdict (2026-05-02), the substrate-
fidelity invariant is now load-bearing in the design review primer.
Future migrations of the assembly-to-model pipeline must assert
model-call inputs (system, messages, tools) at the seam, not
user-facing response text. The three PDI input-fidelity
dimensions (content, tool-surface, context-zone) are required at
architectural-review time. Coverage without invariants is an
anti-pattern at the review layer.

The test suite is the standard. Test count is not.

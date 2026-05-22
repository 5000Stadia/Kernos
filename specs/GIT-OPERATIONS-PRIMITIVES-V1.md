# GIT-OPERATIONS-PRIMITIVES-V1

**Date:** 2026-05-22
**Status:** Draft for review (sub-spec #3 of `KERNOS-AUTONOMOUS-IMPROVEMENT-LOOP-V1`)
**Scope:** Six agent-callable git kernel tools that operate on
  an already-created worktree (worktree create/remove owned by
  IMPROVEMENT-WORKSPACE-V1):
    - `git_fetch` (read)
    - `git_rev_parse` (read)
    - `git_status` (read)
    - `git_diff_for_review` (read)
    - `git_commit` (hard_write, receipt-bound)
    - `git_push` (hard_write + external_agent_read, receipt-bound)
  Each consumes `validate_workspace_path()` from
  IMPROVEMENT-WORKSPACE-V1. Mutations bind to receipts (which
  carry `expected_parent_sha`, `expected_diff_hash`, and the
  written-back `commit_sha`).
**Estimated size:** ~450 LOC source + ~250 LOC tests.

## Why this spec exists

Per parent spec D5: gate classification + audit fidelity demand
explicit git primitives, not `execute_code` shell-outs. The
workspace guard catches the case where a coding agent in a
worktree tries to commit/push from a different path (e.g., the
live Kernos source tree).

`git_commit` + `git_push` are receipt-bound. The receipt
carries the pre-commit state (`expected_parent_sha`,
`expected_diff_hash`); commit refuses on drift; push verifies
`origin/main` hasn't drifted under it AND that the worktree's
HEAD matches the receipt's `commit_sha` (written back by
`git_commit` on success).

## Design principles (load-bearing)

Per [[agent-facing-natural-simplicity]]: every git tool returns
natural-prose responses to the agent (success summary or
error explanation). The substrate keeps structured data
(receipt mutations, audit entries, git stdout/stderr) for
operator inspection but the agent reads sentences.

Per [[kernos-dispatch-gate-design-input]]: the dispatch gate's
amortization layer collapses repeated reads (e.g., `git_status`
checks) within TTL; mutations always re-evaluate.

## Current state

- IMPROVEMENT-WORKSPACE-V1 ships `validate_workspace_path()` —
  every git tool calls it as the first gate.
- DURABLE-APPROVAL-RECEIPTS-V1 ships `request_approval` /
  `approve` / `get_receipt`. `binding_payload_json` is generic;
  this spec adds a `git_commit_authorization` kind that
  carries the commit-prep payload.
- No git kernel tools exist today. The catalog has shell-out
  via `execute_code` but no first-class git primitives.

## Design

### Tool surfaces (input schemas, agent-facing)

All schemas use natural-language descriptions per
[[agent-facing-natural-simplicity]]. Field names read as intent.

**`git_fetch`**
```python
{
  "name": "git_fetch",
  "description": (
    "Update remote-tracking refs in the improvement worktree. "
    "Reads from `origin`. No mutations to local branches."
  ),
  "input_schema": {
    "type": "object",
    "properties": {
      "workspace_dir": {"type": "string"},
      "remote":        {"type": "string", "default": "origin"},
    },
    "required": ["workspace_dir"],
  },
}
```

**`git_rev_parse`** — returns SHA of a ref. Used to capture
base + verify expected-HEAD.
```python
properties: {workspace_dir, ref}; required: [workspace_dir, ref]
```

**`git_status`** — verifies clean state. Returns prose summary
("clean working tree" or "5 modified files, 2 untracked").

**`git_diff_for_review`** — returns the diff. `base` default
`origin/main`, `head` default `HEAD`. Output is the raw diff
(may be large; spec caps at 64KB and notes truncation).

**`git_commit`** — `hard_write`. Receipt-bound. Schema:
```python
properties: {
  workspace_dir, message, approval_id,
  files: {"type": "array", "items": {"type": "string"}},
}
```
On approval-id verification: reads the receipt, verifies
`kind == "git_commit_authorization"`, verifies its
`expected_parent_sha` matches current HEAD parent, verifies
the staged diff hash matches `expected_diff_hash`. Stages
only the listed files (NEVER `git add -A`); rejects files
outside the worktree. On success, writes `commit_sha` to
the receipt's `outcome_payload_json`.

**`git_push`** — `hard_write` + `external_agent_read`. Receipt-
bound (same approval_id as the commit). Verifies the receipt's
`expected_parent_sha` still matches `origin/main` (no drift
under us). Verifies the worktree's HEAD matches the receipt's
`outcome_payload_json["commit_sha"]`. Refuses `--force`
always. Refuses if receipt is not in `state="approved"` with
`commit_sha` populated.

### Receipt extension: `git_commit_authorization` kind

Receipts gain a new `kind` value. Binding payload:
```json
{
  "kind": "git_commit_authorization",
  "workspace_dir": "...",
  "expected_parent_sha": "abc123...",
  "expected_diff_hash": "sha256:...",
  "target_branch": "main",
  "summary": "human-readable summary of what's being committed"
}
```

Outcome payload (written on `git_commit` success):
```json
{
  "commit_sha": "def456...",
  "committed_at": "ISO-8601"
}
```

A new helper `approval_receipts.set_outcome_field(...)` lets
`git_commit` atomically populate `commit_sha` without racing.

### Workspace-guard integration

Every tool's handler calls
`validate_workspace_path(claimed_path=workspace_dir, ...)`
as the first step. On failure, returns natural-prose error
to the agent (the guard's reason text doubles as agent prose
per spec contract).

### Layered design: agent vs operator surfaces

**Agent (prose):**
- `git_fetch` success → *"Fetched origin. 0 new commits."* or *"Fetched origin. 3 new commits on main since last fetch."*
- `git_status` clean → *"Working tree is clean. No changes."*
- `git_status` dirty → *"5 files modified, 2 untracked. Run git_diff_for_review to see what changed."*
- `git_commit` success → *"Committed `{sha[:12]}` on improvement/{attempt}. Message: '{first line of msg}'"*
- `git_commit` drift → *"The diff has changed since the operator approved. Re-issue the approval for the current state."*
- `git_push` success → *"Pushed `{sha[:12]}` to origin/main. Cycle complete."*
- `git_push` origin-drifted → *"origin/main has new commits since the approval. Operator needs to decide whether to abort or rebase."*

**Operator (structured):**
- Audit entries via canonical `ToolInvocationAuditEntry`.
- Receipt mutations (commit_sha write-back).
- gate event log (covenant checks, amortization, etc.).
- friction reports on activation failures.

### Hash discipline

`expected_diff_hash` is `sha256(<staged diff bytes>)`. The
helper `kernos/kernel/git_operations.py:_compute_staged_diff_hash`
runs `git diff --cached` and hashes the bytes. The orchestrator
captures this hash BEFORE issuing the receipt (Step 5 of the
parent workflow). `git_commit` recomputes and refuses on mismatch.

### Tool registration / wiring

- Schemas in `kernos/kernel/git_operations.py`.
- Registered in `kernos/kernel/kernel_tool_registry.py`.
- Names added to `_KERNEL_TOOLS` in `reasoning.py`.
- Read tools (`git_fetch`, `git_rev_parse`, `git_status`,
  `git_diff_for_review`) → gate classification `read`.
- `git_commit` → `hard_write`.
- `git_push` → `hard_write` (network egress folded in;
  no separate external_agent_read classification in v1 — the
  receipt requirement is the primary guard).
- Dispatch elifs in `reasoning.execute_tool`.

## Acceptance criteria

### Read tools

| AC | Description |
|---|---|
| AC1 | `git_fetch(workspace_dir, remote)` runs `git fetch <remote>` in the worktree. Returns prose summary. |
| AC2 | `git_fetch` rejects invalid workspace_dir with the guard's natural-prose error. |
| AC3 | `git_rev_parse(workspace_dir, ref)` returns the SHA as a string. |
| AC4 | `git_rev_parse` returns natural-prose error when ref doesn't exist. |
| AC5 | `git_status(workspace_dir)` returns "clean" prose when no changes; structured-but-natural summary on dirty. |
| AC6 | `git_diff_for_review(workspace_dir, base, head)` returns the diff text. |
| AC7 | `git_diff_for_review` truncates output at 64KB with a clear "diff continues" note. |

### Mutation tools

| AC | Description |
|---|---|
| AC8 | `git_commit` rejects when no `approval_id` provided. |
| AC9 | `git_commit` rejects when receipt isn't `kind="git_commit_authorization"`. |
| AC10 | `git_commit` rejects when receipt isn't in `state="approved"`. |
| AC11 | `git_commit` rejects on `expected_parent_sha` drift (current HEAD parent has changed). |
| AC12 | `git_commit` rejects on `expected_diff_hash` drift (staged diff has changed). |
| AC13 | `git_commit` stages ONLY the listed files (never `-A`). |
| AC14 | `git_commit` rejects files outside the worktree. |
| AC15 | `git_commit` writes `commit_sha` to receipt outcome on success. |
| AC16 | `git_push` rejects when receipt's `commit_sha` not populated. |
| AC17 | `git_push` rejects when `origin/main` has drifted since approval. |
| AC18 | `git_push` rejects when worktree HEAD doesn't match receipt's `commit_sha`. |
| AC19 | `git_push` never uses `--force`. |

### Cross-cutting

| AC | Description |
|---|---|
| AC20 | Every tool calls `validate_workspace_path` first; rejection prose surfaces from the guard. |
| AC21 | All tools' error responses are natural prose — assertable absence of `{`, `}`, JSON markers. |
| AC22 | Gate classifications: read for the 4 read tools, hard_write for commit + push. |

## Soak gate

1. **Automated**: ACs pin tool wiring + receipt verification + drift detection + guard integration.
2. **Operator soak**: full Step-5/Step-6 cycle against a real worktree:
   - Operator approves receipt with expected_parent_sha + expected_diff_hash.
   - Agent calls git_commit; verify commit_sha written back.
   - Agent calls git_push; verify push happens + receipt consumed.

## Out of scope

- Worktree create/remove → IMPROVEMENT-WORKSPACE-V1.
- Orchestrator workflow → IMPROVEMENT-LOOP-WORKFLOW-V1.
- Self-test gate → SELF-TEST-GATE-V1.
- Force-push capability → explicitly disallowed by `git_push`.

## Risks

- **Risk:** Diff-hash mismatch on benign whitespace
  normalizations (e.g., trailing newline added by editor).
  - **Mitigation:** Hash captures the literal staged bytes.
    If the orchestrator's hash capture is consistent with
    `git_commit`'s, drift won't false-fire. Tests pin the
    hash function used.

- **Risk:** Receipt's `commit_sha` write-back races with
  another `git_commit` reusing the same approval_id.
  - **Mitigation:** receipt is single-use (state machine
    enforces approved → consumed); second git_commit refuses
    with the "not in approved state" error.

- **Risk:** `git_push` succeeds but the operator's terminal
  shows stale prompt info — operator doesn't realize the
  push happened.
  - **Mitigation:** ledger event + operator-facing prose
    on the push tool's response carry the pushed SHA.
    `/improvement_status <id>` shows the commit row.

## Dependencies

- IMPROVEMENT-WORKSPACE-V1 (shipped) — `validate_workspace_path`
  guard.
- DURABLE-APPROVAL-RECEIPTS-V1 (shipped) — receipt issue +
  approve + get + new `set_outcome_field` helper.
- IMPROVEMENT-ATTEMPT-LEDGER-V1 (shipped) — `record_commit`
  called by `git_push` to log per-cycle truth.

## Migration

- **Schema**: receipts schema unchanged. New `kind` value is
  additive (existing receipts with other kinds keep working).
- **New helper**: `set_outcome_field` added to
  `approval_receipts.py`.
- **Catalog**: 6 new kernel tools, additive.

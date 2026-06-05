# Capability: Tool Surface

The full kernel-tool catalog as of 2026-06-05. 76 kernel tools registered through the [canonical registry](../architecture/kernel-tool-registry.md) (authoritative source: `ReasoningService._KERNEL_TOOLS` in `kernos/kernel/reasoning.py`; the live surface is also reachable at runtime via `inspect_tools`). MCP (Model Context Protocol) tools layer on top per-space; this page covers the kernel surface only.

This is a navigation document. Each tool below has its own dedicated page or lives inside a capability page covering its domain.

## Always-pinned tools

These appear in every turn's tool surface regardless of context (`ALWAYS_PINNED` set in `kernos/kernel/tool_catalog.py`):

| Tool | Effect | Page |
|---|---|---|
| `remember` | read | [`memory-tools.md`](memory-tools.md) |
| `remember_details` | read | [`memory-tools.md`](memory-tools.md) |
| `read_file` | read | [`file-system.md`](file-system.md) |
| `write_file` | soft_write | [`file-system.md`](file-system.md) |
| `list_files` | read | [`file-system.md`](file-system.md) |
| `execute_code` | soft_write | [`file-system.md`](file-system.md) |
| `register_tool` | soft_write | [`memory-tools.md`](memory-tools.md) |
| `request_tool` | read | [`memory-tools.md`](memory-tools.md) |
| `inspect_state` | read | [`memory-tools.md`](memory-tools.md) |
| `manage_workspace` | read/soft_write | [`memory-tools.md`](memory-tools.md) |
| `send_to_channel` | soft_write | [`channels.md`](channels.md) |
| `manage_plan` | read/soft_write | [`memory-tools.md`](memory-tools.md) |
| `send_relational_message` | soft_write | [`relational-messaging.md`](relational-messaging.md) |
| `resolve_relational_message` | soft_write | [`relational-messaging.md`](relational-messaging.md) |
| `manage_members` | read/soft_write | [`relational-messaging.md`](relational-messaging.md) |
| `consult` | soft_write | [`external-agents.md`](external-agents.md) |
| `ask_coding_session` | soft_write | [`external-agents.md`](external-agents.md) |
| `read_coding_session_response` | read | [`external-agents.md`](external-agents.md) |
| `inspect_tools` | read | (lists the live tool surface) |
| `dump_context` | read | (snapshots the assembled context) |
| `restart_self` | hard_write | (restarts the running process) |
| `improve_kernos` | soft_write | (entry point to the self-improvement loop) |
| `run_self_review` | read | (owner-gated self-maintenance review — tool form of `/selfreview`) |

(23 always-pinned tools total; `ALWAYS_PINNED` set in `kernos/kernel/tool_catalog.py`.)

## Reference primitive (REFERENCE-PRIMITIVE-V1)

Documentation reach + agent-stored project-deep reference. See [`references.md`](references.md).

| Tool | Effect | Notes |
|---|---|---|
| `request_reference` | read | Brief-driven canonical content retrieval |
| `store_reference` | soft_write | Persist project-deep reference material |
| `create_reference_collection` | soft_write | Start a coherent reference set |
| `move_reference_to_canvas` | soft_write | Recovery: realize material is workspace-shaped |
| `mark_reference_superseded` | soft_write | Recovery: explicit version replacement |
| `quarantine_reference` | soft_write | Recovery: flag suspect content |
| `restore_reference_from_quarantine` | soft_write | Recovery: undo quarantine |

## Identity + soul

| Tool | Effect | Page |
|---|---|---|
| `read_soul` | read | [`../identity/who-you-are.md`](../identity/who-you-are.md) |
| `update_soul` | soft_write | [`../identity/soul-system.md`](../identity/soul-system.md) |
| `read_source` | read | (reads Python source under `kernos/`; not a doc tool) |

`read_doc` was retired in REFERENCE-PRIMITIVE-V1; canonical documentation now reaches via `request_reference`.

## Canvas (CANVAS-V1)

| Tool | Effect | Page |
|---|---|---|
| `canvas_list` | read | (canvas architecture under `architecture/canvas.md`) |
| `canvas_create` | hard_write | |
| `page_read` | read | |
| `page_write` | soft_write | |
| `page_list` | read | |
| `page_search` | read | |
| `canvas_preference_extract` | soft_write | |
| `canvas_preference_confirm` | soft_write | |

## Covenants + behavior

| Tool | Effect | Page |
|---|---|---|
| `manage_covenants` | read/soft_write | [`../behaviors/covenants.md`](../behaviors/covenants.md) |
| `dismiss_whisper` | read | [`../behaviors/proactive-awareness.md`](../behaviors/proactive-awareness.md) |

## Channels + scheduling

| Tool | Effect | Page |
|---|---|---|
| `manage_channels` | read/soft_write | [`channels.md`](channels.md) |
| `send_to_channel` | soft_write | [`channels.md`](channels.md) |
| `manage_schedule` | read/soft_write | [`../behaviors/scheduler.md`](../behaviors/scheduler.md) |

## Cross-space requests

| Tool | Effect | Page |
|---|---|---|
| `request_space_action` | soft_write | [`cross-space-requests.md`](cross-space-requests.md) |

(`consult` and the coding-session tools are listed under "Coding sessions + external agents" above.)

## Diagnostics

| Tool | Effect | Page |
|---|---|---|
| `read_runtime_trace` | read | [`diagnostics.md`](diagnostics.md) |
| `diagnose_issue` | read | [`diagnostics.md`](diagnostics.md) |
| `propose_fix` | soft_write | [`diagnostics.md`](diagnostics.md) |
| `submit_spec` | soft_write | [`diagnostics.md`](diagnostics.md) |
| `set_chain_model` | soft_write | [`diagnostics.md`](diagnostics.md) |
| `diagnose_llm_chain` | read | [`diagnostics.md`](diagnostics.md) |
| `diagnose_messenger` | read | [`diagnostics.md`](diagnostics.md) |

## Capabilities (manage external services)

| Tool | Effect | Page |
|---|---|---|
| `manage_capabilities` | read/soft_write | [`overview.md`](overview.md) |

## Coding sessions + external agents

Hand work to, or get a second opinion from, an external coding agent (Codex / Claude Code / Gemini). See [`external-agents.md`](external-agents.md).

| Tool | Effect | Notes |
|---|---|---|
| `consult` | soft_write | Synchronous second opinion from an external agent |
| `ask_coding_session` | soft_write | Dispatch a longer coding task to an external session |
| `read_coding_session_response` | read | Poll/collect a dispatched session's result |

## Long-horizon projects (LONG-HORIZON-PROJECT-V1)

| Tool | Effect | Notes |
|---|---|---|
| `start_project` | soft_write | Open a long-horizon project record |
| `record_project_decision` | soft_write | Append a decision to a project |
| `surface_project_status` | read | Summarize a project's current state |
| `manage_plan` | read/soft_write | Manage the project's plan (also always-pinned) |

## Self-administration

| Tool | Effect | Notes |
|---|---|---|
| `inspect_tools` | read | List the live tool surface (source of truth for this catalog) |
| `dump_context` | read | Snapshot the assembled context for diagnostics |
| `note_this` | soft_write | Capture a quick note into memory |
| `delete_file` | soft_write | Soft-delete a file (shadow archive) |
| `restart_self` | hard_write | Restart the running process |
| `run_self_test_suite` | soft_write | Run the substrate self-test suite |

## Git operations (GIT-OPERATIONS-PRIMITIVES-V1)

Used by the self-improvement loop; read-only inspection plus gated mutation. `start.sh` is off-limits to autonomous modification.

| Tool | Effect | Notes |
|---|---|---|
| `git_fetch` | read | Fetch from origin |
| `git_status` | read | Working-tree status |
| `git_rev_parse` | read | Resolve refs/SHAs |
| `git_diff_for_review` | read | Diff (including uncommitted worktree work) for review |
| `git_commit` | hard_write | Commit (identity auto-injected on identity-less deploy clones) |
| `git_push` | hard_write | Push to origin |

## Self-improvement loop + recovery (AUTONOMOUS-IMPROVEMENT-LOOP arc)

The autonomous spec→implement→review→approve→deploy→self-test loop and its post-restart recovery/closure machinery. See [`../TECHNICAL-ARCHITECTURE.md`](../TECHNICAL-ARCHITECTURE.md) §11b.

| Tool | Effect | Notes |
|---|---|---|
| `improve_kernos` | soft_write | Entry point to the self-improvement loop (also always-pinned) |
| `proceed_with_recovery` | soft_write | After a failed post-restart self-test, attempt a bounded fix-up |
| `abandon_attempt` | soft_write | Abandon the attempt and roll back |
| `record_closure_attempt` | soft_write | Record a closure attempt against an issue |
| `run_closure_probe` | read | Probe whether an issue is closed |
| `lookup_pattern_invariants` | read | Retrieve known invariants for a failure pattern |
| `record_fix_authorization` | soft_write | Record a single-use fix authorization (approval receipt) |
| `classify_proposed_fix` | read | Classify a proposed fix (mechanical vs architectural) |
| `validate_investigation_response` | read | Validate an investigation/bridge response |
| `maybe_run_closure_for_fix` | soft_write | Conditionally run closure for an applied fix |
| `surface_to_user` | soft_write | Surface a situation/decision to the user conversationally |

## Effect classification (the gate)

Every kernel tool is classified by [the dispatch gate](../behaviors/dispatch-gate.md):

- **read** → bypasses the gate (no friction).
- **soft_write** → gate evaluates against active covenants; reactive user-requested actions are typically approved.
- **hard_write** → gate always evaluates; third-party impact + proactive moves require explicit reasoning.

Action-aware tools (`manage_covenants`, `manage_capabilities`, `manage_channels`, `manage_members`, `manage_plan`, `manage_schedule`, `manage_workspace`) classify per-action: `list` actions are read; mutation actions are soft_write.

Unknown tools default to `hard_write` (safe default).

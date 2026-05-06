# Capability: Tool Surface

The full kernel-tool catalog as of REFERENCE-PRIMITIVE-V1 (2026-05-05). 48 kernel tools registered through the [canonical registry](../architecture/kernel-tool-registry.md). MCP (Model Context Protocol) tools layer on top per-space; this page covers the kernel surface only.

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

## Cross-space + external agents

| Tool | Effect | Page |
|---|---|---|
| `request_space_action` | soft_write | [`cross-space-requests.md`](cross-space-requests.md) |
| `consult` | soft_write | [`external-agents.md`](external-agents.md) |

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

## Effect classification (the gate)

Every kernel tool is classified by [the dispatch gate](../behaviors/dispatch-gate.md):

- **read** → bypasses the gate (no friction).
- **soft_write** → gate evaluates against active covenants; reactive user-requested actions are typically approved.
- **hard_write** → gate always evaluates; third-party impact + proactive moves require explicit reasoning.

Action-aware tools (`manage_covenants`, `manage_capabilities`, `manage_channels`, `manage_members`, `manage_plan`, `manage_schedule`, `manage_workspace`) classify per-action: `list` actions are read; mutation actions are soft_write.

Unknown tools default to `hard_write` (safe default).

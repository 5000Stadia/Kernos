# Kernel Tool Registry

KERNEL-TOOL-REGISTRY-V1 (shipped 2026-05-03 + workshop-prep fold 2026-05-04). The canonical compiled list of kernel-tool descriptors. Schema constants in their owning modules are the canonical source; this registrar imports them by name and produces one canonical list every consumer derives views from.

## The problem this fixed

Pre-V1, three hardcoded registries drifted apart silently:

- `ReasoningService._KERNEL_TOOLS` knew 42 tools (dispatch authority).
- The thin-path catalog hand-maintained 27 (surfacer LLM input).
- The legacy `assemble.py:_all_kernel` aggregation hand-maintained 19.

Fifteen tools dispatched-but-invisible to one or both surfacing paths. The agent had substrate awareness of canvases / parcels but no callable tools to act on them. Schema constants already carried name + description as data; both consumer dicts re-stated the same information by hand.

The registry honors the structure that already exists — schemas live in their owning modules; this registrar imports them by name and gives every consumer one canonical surface.

## Implementation discipline

Per Kit's caution (2026-05-03):

- **Explicit schema imports**, NOT blind module walking. Each schema lives in its owning module; the registrar imports them by name. Filesystem / introspection discovery is deliberately avoided — the goal is automatic derivation, not magical scanning.
- **Policy read-through with disciplined facade.** Surfacer-policy metadata (`always_pinned`, etc.) lives at its natural owner (`kernos.kernel.tool_catalog.ALWAYS_PINNED`); the registrar is the normalized read model. The Policy Source Map below documents field → owner → accessor → mutability.
- **CRB parcels are an explicit architectural exclusion**, not drift. Parcel tools gate separately on parcel-applicable turns; they have their own parity pin in CI. Kept on a separate path by design — see `crb_parcel_schemas()`.

## Where it lives

`kernos/kernel/kernel_tool_registry.py`. Two main entry points:

| Function | Purpose |
|---|---|
| `kernel_tool_schemas() -> list[dict]` | The canonical list of kernel-tool schema dicts. Anthropic-style `{name, description, input_schema}` shape. |
| `kernel_tool_descriptors() -> list[KernelToolDescriptor]` | The fully-typed descriptor list with policy metadata. |

A parity pin (`tests/test_kernel_tool_registry_parity.py`) verifies every tool name in `_KERNEL_TOOLS` (dispatch) matches a schema in the registrar (surface), and that every entry has policy metadata accessible.

## KernelToolDescriptor

Six fields per the workshop-tool prep design note's contract:

| Field | Type | Notes |
|---|---|---|
| `name` | str | Tool name (Anthropic / OpenAI tool identity) |
| `description` | str | Agent-visible description |
| `input_schema` | dict | JSON Schema for tool input |
| `schema` | dict | Full original schema dict (Anthropic-style `{name, description, input_schema}`) so consumers passing schema verbatim into the LLM tool call have the source-of-truth |
| `policy_metadata` | dict | `always_pinned` and future fields |
| `dispatch_reference` | Any (None for kernel) | Workshop-tool contract field; `None` for kernel tools (dispatch is encoded in `ReasoningService.execute_tool`'s elif chain). For workshop tools (future spec) it carries an importable callable reference or service-id string. |

Codex review (2026-05-04 fold) caught the mismatch where the design note listed `dispatch_reference` but the dataclass omitted it; field added so kernel and workshop tools share the exact contract.

## Policy source map

| Field | Owner | Accessor | Mutability |
|---|---|---|---|
| `always_pinned` | `kernos.kernel.tool_catalog.ALWAYS_PINNED` | `_policy_for` | Stable (module-load constant) |

Adding a new policy field: pick the natural owner module, add a read-through accessor here, document the row above, add a parity-pin entry asserting "no orphan policy" (every entry references a real tool name; every tool with policy has it accessible here).

## Workshop-tool prep contract

> **Status:** This contract specifies the shape for a future capability. **No production tools currently use it**; the contract is preserved as design intent for when workshop tools land. Reading this section in 2026-05 means looking at a forward-looking spec, not at code that's running.

The future workshop-tool spec (out of scope here; design-only) plugs into the same registrar / provider interface. Minimum future contract:

- **Descriptor fields:** `name`, `description`, `input_schema`, `schema`, `policy_metadata`, `dispatch_reference`. Workshop tools provide the same six fields kernel tools provide today; the registrar treats both the same way at the read seam.
- **Namespace / collision rules:** workshop tool names share the kernel-tool namespace. Collisions reject at registration time — workshop registration may not shadow a kernel tool. The registrar asserts uniqueness across both surfaces.
- **Security / gate-classification boundary:** workshop tools must declare effect class (`read` / `soft_write` / `hard_write`) at registration. The dispatch gate enforces at call time per the gate-at-dispatch principle (the registration-time declaration is metadata, not authority).
- **Persistence / home-space expectation:** workshop descriptors persist instance-scoped (per-instance workshop directory). On instance restart, the registrar enumerates persisted descriptors alongside kernel-tool schema exports.
- **Plug-in interface:** workshop descriptors implement the same `KernelToolDescriptor` shape via `register_workshop_tool(...)` (future). The registrar's `kernel_tool_descriptors()` returns a unified list; consumers see one canonical surface regardless of origin.

Don't overbuild this; the future spec implements. The contract above is what that future spec is held to.

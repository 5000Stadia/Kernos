# Workshop external-service primitive

## Mental model — the AppData pattern

This is the Windows AppData pattern, applied to a multi-member agentic operating system.

- **Tool definition is like an installed application.** One copy on the host, accessed by all members. The descriptor and implementation files are shared; the registry is shared.
- **Tool state is like AppData.** Per-member directory, runtime-enforced isolation. Each invocation gets its own `data_dir` under `<install>/<instance>/members/<member>/tools/<tool_id>/`.
- **Credentials are like Credential Manager entries.** Per-user (per-member) store, scoped by member identity. The accessor handed to the tool returns only the invoking member's credentials for the tool's bound service.
- **Cross-member aggregation is like ProgramData.** Deliberately shared, deliberately rare, deliberately requires explicit declaration. v1 reserves the enum value but rejects registration; the v2 spec (`WORKSHOP-CROSS-MEMBER-AGGREGATION`) lands the actual behaviour with per-member opt-in confirmation.

The boundary statement is short: **tools are universal capabilities; invocations are member contexts.** The surfacing layer plus the runtime context together enforce that no tool can see anything not belonging to the invoking member.

## What this primitive lands

Six modules under `kernos/kernel/`:

- `credentials_member.py` — per-member credentials store, encrypted at rest with Fernet.
- `services.py` — service descriptor model, registry, auth-by-channel matrix.
- `tool_descriptor.py` — extended tool descriptor with per-operation classification.
- `tool_validation.py` — authoring-pattern validator (registration time).
- `tool_runtime.py` — invocation-scoped runtime context.
- `tool_runtime_enforcement.py` — four runtime checks at invocation time.

Plus `tool_audit.py` for the audit-log integration and `tool_gate_routing.py` for the dispatch-gate bridge.

The dispatcher integration that wires these together lands as part of the first stock integration (`STOCK-INTEGRATIONS-NOTION`). The primitive ships the substrate; each stock integration ships the wiring + service-specific descriptor.

## Credentials

### Storage layout

```
data/
└─ <instance>/
   └─ credentials/
      ├─ <member_id>/
      │  └─ <service_id>.enc.json
      └─ .key
```

Each `<service_id>.enc.json` file is a Fernet-encrypted JSON record holding the token, refresh token, expiry, scopes, and metadata. File mode is 0600.

### Key management

The encryption key is resolved in this order:

1. `KERNOS_CREDENTIAL_KEY` environment variable (operator-supplied, urlsafe base64 32-byte key).
2. `<install>/<instance>/credentials/.key` file (mode 0600, auto-generated on first call if absent and no env var present).

On first auto-generation, a one-line `CREDENTIAL_KEY_GENERATED` notice logs at WARNING level reminding the operator to back the key up or set it explicitly. Same posture as the browser-profile threat-surface note that shipped in `BROWSER-PERSISTENT-PROFILE`.

### Boundary with install-level credentials

Kernos already has an install-level credentials surface in `kernos/kernel/credentials.py` for Kernos-itself dependencies: LLM provider OAuth and API keys, Brave search key, Google OAuth credential path used by the calendar capability, Voyage embedding key. Those resolve at startup and have one copy per Kernos install.

The new primitive does not subsume them. Boundary:

- **Install-level:** Kernos-itself dependencies. One copy per Kernos install. Resolved at startup. Existing module continues to own them.
- **Member-level:** member-specific service tokens (Notion, GitHub, Slack, etc.). One copy per member-and-service pair. Resolved at invocation time via the new accessor.

The Google OAuth currently used by the calendar capability sits awkwardly in the middle today — it is per-install but conceptually per-member. Calendar continues to use the install-level path for now; future work may migrate it to the member-level primitive when the multi-member calendar story lands.

## Service descriptors

A service descriptor declares an external service the workshop tools can bind to via `service_id`. Six fields:

- `service_id` — stable machine identifier (`notion`, `github`).
- `display_name` — human label.
- `auth_type` — `api_token` or `oauth_device_code` in v1. `cookie_upload` is reserved for `BROWSER-COOKIE-IMPORT` and intentionally absent from the enum until that spec ships substrate.
- `operations` — the operation vocabulary the service exposes. Tool descriptors declare their `authority` as a subset.
- `audit_category` — operator-readable category for invocations (defaults to `service_id`).
- `required_scopes` — auth-flow-required scope strings.

Stock service descriptors live at `kernos/kernel/services/<service_id>.service.json`. The Notion sample landed in C2 alongside the registry.

### Auth-by-channel matrix

| Auth type           | CLI | SMS | Discord DM | Telegram |
|---------------------|-----|-----|------------|----------|
| `api_token` paste   | yes | no  | no         | no       |
| `oauth_device_code` | yes | yes | yes        | yes      |

**Why CLI-only for `api_token`:** the token is a long-lived secret. Discord, SMS, and Telegram retain message contents outside Kernos's threat boundary. A token pasted into adapter chat sits in adapter history indefinitely. CLI delivery means the secret arrives on the operator's terminal and lands in the encrypted store; no third-party retention.

**Why every channel for `oauth_device_code`:** the device code is short-lived and not itself a secret. The user confirms on their own device.

The matrix is machine-readable (`AUTH_CHANNEL_MATRIX` constant in `kernos/kernel/services.py`). The auth onboarding flow refuses incompatible channel-and-auth combinations with a clear pointer to the alternative — no silent acceptance of unsafe combos.

## Tool descriptor model

Existing four required fields preserved:

- `name` (snake_case)
- `description`
- `input_schema` (JSON Schema)
- `implementation` (`.py` filename)

Extension fields (optional; existing tools parse without modification):

- `service_id` — links the tool to a registered service. Tools without a `service_id` do not receive the credentials accessor.
- `authority` — list of operation names the tool is allowed to invoke. Validated as a subset of the service's declared operations at registration.
- `gate_classification` — tool-level shorthand: `read`, `soft_write`, `hard_write`, or `delete`.
- `operations` — list of per-operation classifications. Each entry: `{operation: "...", classification: "..."}`. Per-operation overrides the tool-level shorthand at routing time.
- `audit_category` — operator-readable category. Defaults to `service_id`'s `audit_category` for service-bound tools, else to the tool name.
- `domain_hints` — optional list of strings for relevance-based surfacing (consumed by the future `TOOL-SURFACING-RELEVANCE` spec; service-bound tools surface based on credential presence and don't need hints).
- `aggregation` — `per_member` (default) or `cross_member`. The `cross_member` value is reserved-but-rejected in v1 with a clear error pointing at the `WORKSHOP-CROSS-MEMBER-AGGREGATION` follow-on.

### Per-operation classification (the design review edit 1)

A tool that exposes multiple operations classifies each independently:

```
read_pages   → read         (no gate)
write_pages  → hard_write   (gate fires confirmation)
delete_pages → delete       (maps to hard_write in v1; runtime
                             confirmation is the safety net)
```

Operations not classified per-op fall back to the tool-level shorthand. Tools with neither fall back to the **fail-closed default of `soft_write`** (design review revision 1: missing classification fails closed, not open). Workshop tools that genuinely only read declare it explicitly via the `gate_classification` shorthand.

## Authoring-pattern validation (registration time)

`tool_validation.validate_tool_source` scans the implementation for patterns that would bypass the runtime context:

- Hardcoded absolute paths (`/home/...`, `/data/...`, `C:\...`)
- Bare `open()` against a hardcoded absolute path
- Instance identifier literals (`discord:12345`, `sms:+1...`)
- Member identifier literals (`mem_alice`, `member_bob`)
- Direct reads of secret env vars (LLM provider keys, `KERNOS_CREDENTIAL_KEY`, etc.)

Findings render with the AppData analogy concretely: *"this is the equivalent of an app writing to System32 instead of AppData."* The author either fixes the tool to use the runtime-context accessors or registers with `force=True`.

### Force-register (the design review edit 5)

Force-registered tools surface only to the author and bypass authoring-pattern validation. Force does **not** bypass member isolation — runtime enforcement (the four invocation-time checks) still applies. Force is for the legitimate edge cases (a tool deliberately reading from a fixed shared file the operator manages) without leaking unsafe authoring patterns to the wider member surface.

## Runtime context (invocation-scoped)

When a tool is invoked, it receives a `ToolRuntimeContext` derived from the invoking member's identity at call time:

- `member_id` — invoking member.
- `instance_id` — invoking install.
- `space_id` — active space.
- `tool_id` — descriptor name (used as the AppData-style sub-folder).
- `data_dir` — `<install>/<instance>/members/<member>/tools/<tool_id>/`. Created on demand. The tool's persistent state lives here.
- `credentials` — `ToolCredentialAccessor` scoped to `(member_id, service_id)`. Tools without a `service_id` get a no-credentials accessor whose `get()` raises `ToolCredentialUnavailable`.

Tools cannot pin to registration-time identity. The authoring-pattern validator catches the obvious bypass attempts at registration; runtime enforcement is the backstop.

## Runtime enforcement (the design review edit 2)

Four checks at every invocation, before the tool's implementation receives control. First failure raises a specific subclass of `RuntimeEnforcementError`; later checks are not run.

1. **Hash unchanged since registration.** SHA-256 of (descriptor JSON ‖ NUL ‖ implementation source) recorded at `register_tool` time, recomputed and compared at invocation. Catches post-registration edits to either file. Mismatch → `HashMismatchError`.
2. **Operation authority re-check.** Invoked operation must be in the tool's declared `authority` list. Service-bound tools also re-validate that the operation is still in the service's declared `operations`, catching descriptor-edit drift between tool and service. Service deregistration is detected. Failure → `AuthorityViolationError`.
3. **Credential scope re-check.** Service-bound tools must have a non-expired credential for the invoking member. Failure → `CredentialUnavailableError`. Surfaces "credential missing or expired" cleanly before the tool's HTTP call has a chance to fail later.
4. **Data-dir sandbox.** `check_sandbox_path(target, context)` verifies a path resolves under `context.data_dir` (symlinks are resolved before comparison). Used by the dispatcher for user-supplied path inputs and by post-invocation review for paths the tool returns. Best-effort in-process check; full subprocess-level isolation is a future spec.

Force-registered tools go through the same path. Per the design review edit 5: force bypasses authoring-pattern validation only, never runtime isolation.

## Audit-log integration

Tool invocations produce audit entries with the existing `tool_call` shape plus two category fields:

- `audit_category` — operator-readable, free-form. Surfaced in operator filters.
- `normalized_category` — fixed vocabulary. Two values today: `tool.invocation.internal` (no `service_id`) and `tool.invocation.external_service` (service-bound). Downstream processors filter on this without parsing operator-readable strings.

Payload digest is **SHA-256 of canonicalised JSON** approximating RFC 8785 (JCS): keys sorted, no insignificant whitespace, UTF-8, no `NaN`/`Infinity`. The digest is sufficient for after-the-fact integrity checks without retaining sensitive request bodies. Tokens, refresh tokens, and credential values inside payloads cannot leak through the audit log.

## Composition with existing primitives

- **Credentials** key by `(member_id, service_id)`. The pattern matches the canvas member-ownership model.
- **Covenants** and **standing orders** already scope to the agent serving a member. Tools inherit the same pattern; tools do not need covenant awareness directly. The agent that calls the tool is covenant-aware, and the agent decides what tool outputs to share with whom in its replies.
- The agent layer governs what tool outputs the agent shares with whom under the covenant disclosure rules. Tools themselves are pure capability surfaces.

## Future hooks

- `STOCK-INTEGRATIONS-NOTION` — first stock instance of the primitive. Read + write through Notion's API. Solves the harness audit's write-blocker.
- `TOOL-SURFACING-RELEVANCE` — consumes `domain_hints` and credential presence to decide per-agent visibility. The primitive ships the metadata; surfacing logic ships separately.
- `BROWSER-COOKIE-IMPORT` — when it lands, the `cookie_upload` auth type returns to the matrix.
- `WORKSHOP-CROSS-MEMBER-AGGREGATION` — v2 surface for tools that aggregate data across members, with per-member opt-in confirmation.

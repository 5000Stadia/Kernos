# Capabilities Overview

Capabilities are external tools that Kernos connects to via the Model Context Protocol (MCP). Each capability is an MCP server that the system connects to, discovers tools from, and routes tool calls through.

> **Looking for the full kernel-tool list?** The kernel provides 75 tools that ship with Kernos itself (memory, files, references, canvas, covenants, channels, scheduling, diagnostics, coding sessions, git, the self-improvement loop, etc.). See [`tool-surface.md`](tool-surface.md) for the complete catalog with effect classifications and per-tool documentation links.

## Unified Capability Registry

All capabilities — pre-installed defaults and user-added tools — live in one registry with the same enable/disable mechanics. There is no "built-in" vs "external" distinction. Everything is an MCP server. Everything can be enabled, disabled, installed, or removed through the same `manage_capabilities` interface.

The only difference between a pre-installed capability (like Google Calendar) and one the user adds later is the `source` field:
- **default** — shipped with Kernos, cannot be removed (only disabled)
- **user** — installed at runtime, can be fully removed

## Pre-Installed Capabilities

These come ready to connect:

| Capability | What it does | Universal? |
|-----------|-------------|-----------|
| Google Calendar | Read schedule, create/update/delete events, find availability | Yes |
| Gmail | Read, categorize, draft, and send email | No |
| Web Search (Brave) | Structured web search returning titles, URLs, snippets | No |
| Web Browser (Lightpanda) | Navigate and read any web page, run JavaScript | Yes |

**Universal** means the capability is available in every context space by default. Non-universal capabilities must be activated per-space.

## Capability Status

Each capability has a status:

- **AVAILABLE** — registered but not connected
- **CONNECTED** — connected and tools discovered (enabled)
- **DISABLED** — MCP server still running, but tools hidden from the agent. Re-enable is instant.
- **ERROR** — connection failed
- **SUPPRESSED** — user removed (config preserved)

## Managing Capabilities

The `manage_capabilities` kernel tool provides a unified interface:

- **list** — show all capabilities with source and status
- **enable** — re-enable a disabled capability (instant, no reconnection)
- **disable** — hide capability tools from the agent (MCP server stays warm)
- **install** — add a new MCP server
- **remove** — uninstall a user-added capability (pre-installed defaults can only be disabled)

## How Connection Works

1. **Registration** — capabilities are registered in the `CapabilityRegistry` with server command, args, and credentials requirements.
2. **Connection** — at startup, registered servers connect via MCP and their tools are discovered.
3. **Tool Discovery** — each MCP server exposes a set of tools. These are added to the tool list available to the agent.
4. **Credential Setup** — some capabilities require API keys or OAuth tokens. The secure credential handoff intercepts the next user message as a secret (never entering the LLM context) and uses it to connect.

## Disable vs Remove

- **Disable**: MCP server process keeps running. Tools are hidden from the agent's tool list. Re-enable restores tools instantly with no reconnection needed.
- **Remove**: MCP server is disconnected. Only works for user-installed capabilities. Pre-installed defaults cannot be removed, only disabled.

## Startup Migration

When new pre-installed capabilities are added to the manifest (`known.py`), existing tenants see them as "available" on their next interaction. No manual migration needed.

## Lazy Tool Loading

Tools use a lazy loading model to minimize token usage. Instead of injecting all ~11,800 tokens of tool schemas on every message, the system uses a three-tier approach:

1. **Always loaded** — Kernel tools (~1,500 tokens). Small schemas, used constantly.
2. **Pre-loaded** — Calendar read tools (list-events, search-events, get-event, etc.). Most-called MCP tools, always have full schemas in context.
3. **Lazy-loaded** — All other MCP tools (calendar writes, browser, search, future Gmail). The system prompt includes a compact directory (~400 tokens) listing available tools by name and description. When the agent calls an unloaded tool, the system loads its full schema and the call executes normally. The tool stays loaded for the rest of the session.

**Token savings:** ~71% reduction on typical messages. The worst case (all tools loaded) is identical to before.

**Session boundary:** Loaded tools reset on compaction or server restart. Fresh session = fresh directory.

## Tool Scoping

Tools are scoped per context space:

- **Universal capabilities** (like calendar) are available in every space
- **Non-universal capabilities** must be explicitly activated for a space
- The `request_tool` meta-tool activates a capability for the current space

## Effect Classification

Every tool call is classified by its effect level:

- **read** — information retrieval, bypasses dispatch gate
- **soft_write** — creates/modifies data, dispatch gate evaluates
- **hard_write** — high-impact action (send email, delete), dispatch gate evaluates with higher scrutiny

Effect classifications come from `tool_effects` on each `CapabilityInfo`, or default to `hard_write` if unknown.

## Code Locations

| Component | Path |
|-----------|------|
| CapabilityRegistry | `kernos/capability/registry.py` |
| MCPClientManager | `kernos/capability/client.py` |
| Pre-installed capabilities manifest | `kernos/capability/known.py` |
| Tool scoping | `kernos/capability/registry.py` (get_tools_for_space) |
| manage_capabilities kernel tool | `kernos/kernel/reasoning.py` |
| Runtime install flow | `kernos/messages/handler.py` |

# CWA Reference: Bridge/Remote Control Architecture

**Source:** ClaudeWebagent (Research/ClaudeWebagent/bridge/)
**Relevance:** Reference architecture for Kernos Phase 5 (Web Interface)

---

## Overview

The CWA bridge connects a web UI (claude.ai) to a local CLI process via a relay server. Two protocol versions exist:

- **V1 (Environment-based)**: Environment registration → work polling → WebSocket session
- **V2 (Environment-less)**: Direct OAuth → session creation → SSE + HTTP

V2 is simpler and recommended for new implementations.

---

## Protocol Summary (V2 — Recommended)

### Session Creation
```
1. POST /v1/code/sessions (OAuth-authenticated)
   → session_id

2. POST /v1/code/sessions/{id}/bridge (OAuth → JWT exchange)
   → worker_jwt, expires_in, api_base_url, worker_epoch
```

### Transport
- **Inbound (server → CLI)**: SSE at `/v1/code/sessions/{id}/events`
  - Query: `from_sequence_num` (for resume after reconnect)
  - Auth: worker_jwt
- **Outbound (CLI → server)**: HTTP POST to `/v1/code/sessions/{id}/worker/events`
  - Auth: worker_jwt
  - Header: `X-Session-Worker-Epoch` (bumped per reconnect)
- **Heartbeat**: POST `/v1/code/sessions/{id}/worker/heartbeat` (extends 300s lease)

### Message Types
- **User messages**: `{type: "user", message: {content: ...}, uuid: "..."}`
- **Assistant messages**: `{type: "assistant", message: {content: [text, tool_use]}}`
- **Control requests** (server → CLI): `{type: "control_request", request: {subtype: "can_use_tool"|"initialize"|"set_model"|"interrupt"}}`
- **Control responses** (CLI → server): `{type: "control_response", response: {subtype: "success"|"error", request_id: "..."}}`

### Permission Relay
```
1. CLI tool hits permission gate
2. CLI emits: control_request {subtype: "can_use_tool", tool_name, input}
3. Server forwards to web UI
4. User approves/denies in browser
5. Server sends: control_response {behavior: "allow"|"deny", updatedInput?, message?}
6. CLI resumes tool execution
```

### Reconnection
- **401 on SSE**: JWT expired → POST /bridge with fresh OAuth → rebuild transport
- **Proactive refresh**: Schedule 5min before JWT expiry → rebuild transport
- **Permanent close**: Reconnect loop with backoff (gives up after 15min)
- **State preserved**: UUID dedup rings, sequence numbers, queued messages

### Session End
```
1. Send result message (usage summary)
2. Flush transport queue
3. Close SSE connection
4. POST /v1/sessions/{id}/archive
```

---

## Security Layers

| Layer | Token | Purpose | Lifetime |
|-------|-------|---------|----------|
| OAuth | User's Claude OAuth | Session creation, bridge registration | Refreshable |
| Worker JWT | Session-scoped | SSE auth, heartbeat, events | ~1 hour, proactive refresh |
| Worker Epoch | Integer, bumped per /bridge call | Prevents stale workers from sending events | Per-connection |
| Trusted Device | Machine-scoped | Elevated auth for sensitive environments | 90 days rolling |

---

## Key Data Structures

### BoundedUUIDSet (Echo Dedup)
Circular ring buffer + Set for O(1) lookup. Prevents:
- Echoing own messages back (recentPostedUUIDs)
- Replaying server re-deliveries (recentInboundUUIDs)

### FlushGate
Queues live messages during initial history flush. Server receives [history..., live...] in order. Deactivates on transport swap.

### BridgePointer (Crash Recovery)
```json
{
  "sessionId": "session_...",
  "environmentId": "env_...",
  "source": "standalone"
}
```
Stored at `~/.claude/projects/{dir}/bridge-pointer.json`, 4-hour TTL. Enables `--continue` resume after crash.

---

## Phase 5 Design Implications

### What Kernos Needs

1. **Session endpoint**: Create/join sessions, exchange OAuth for session JWT
2. **SSE inbound channel**: Server pushes user messages + control requests to Kernos
3. **HTTP outbound channel**: Kernos posts assistant messages + tool results to server
4. **Permission relay**: Gate blocks → control_request to web UI → user approves/denies → control_response back to Kernos
5. **Heartbeat**: Keep session alive (300s TTL default)
6. **Reconnection**: Proactive JWT refresh, sequence-number-based SSE resume
7. **Echo dedup**: Ring buffer prevents message loops

### What Kernos Already Has

- **NormalizedMessage abstraction**: Web adapter would produce NormalizedMessages like Discord/SMS adapters
- **Handler/adapter isolation**: Web bridge is just another adapter
- **Gate with tool effects**: Permission relay maps directly to gate results
- **Turn serialization**: Per-(tenant, space) mailbox handles concurrent sessions

### What's New

- **WebSocket/SSE transport layer**: New infrastructure (not in current adapters)
- **Bidirectional control protocol**: initialize, interrupt, set_model, permission relay
- **Session management**: Create/resume/archive lifecycle
- **Token management**: JWT exchange, proactive refresh, epoch tracking
- **Client-side state**: Web UI needs to render messages, handle permissions, show progress

### Recommended Approach

Start with V2 (environment-less) — simpler, fewer moving parts. The flow:
1. Web UI authenticates user → creates session via API
2. Kernos process connects to session via SSE (inbound) + HTTP (outbound)
3. Messages flow bidirectionally through the session relay
4. Permissions relay through control_request/control_response
5. Session ends → archive + cleanup

This maps cleanly to Kernos's existing adapter pattern: `WebBridgeAdapter` would be a new adapter alongside `DiscordAdapter` and `TwilioSMSAdapter`.

# Capability: Relational Messaging

RELATIONAL-MESSAGING v5 (shipped 2026-04-20). Member-to-member message routing with declared relationships gating disclosure.

## Tools

| Tool | Effect | Purpose |
|---|---|---|
| `send_relational_message` | soft_write | Send a message from your member to another member (envelope-based; routed through the relationship gate). |
| `resolve_relational_message` | soft_write | Mark a previously-surfaced relational message as resolved (so it stops showing up in your awareness block). |
| `manage_members` | read/soft_write | List members, declare a relationship, list relationships, invite, connect_platform, remove. |

## How it composes with relationships

Members declare pairwise relationships with one of four permission profiles:

| Profile | Disclosure permission |
|---|---|
| `full-share` | All sensitivity classes pass |
| `work-only` | Work-context information passes; personal/contextual blocked |
| `coordination-only` | Schedule + logistics pass; substantive content blocked |
| `minimal` | Almost nothing passes — explicit confirm required for each disclosure |

The relational message envelope carries the sender, the recipient, the message content, and a sensitivity hint. The disclosure gate composes the envelope's sensitivity against the (declarer, other) relationship's permission profile and decides whether to surface.

Conservative defaults until explicitly confirmed. The 24 Escalation pattern for abuse prevention applies — repeated failed disclosure attempts escalate at increasing intervals (24s → 24m → 24h → 24d → 24y → 24c).

## Recent-surfaced reference window

Surfaced messages remain reference-able for a 1h window via `collect_recent_surfaced_for_member`. After that they age out of the active surface but remain in the message-relay table. `resolve_relational_message` is a soft-write that flips the surface-state flag; the original message stays in the relay log for audit.

## When to use

- A user asks you to relay something to another member: use `send_relational_message`.
- You see a message in your awareness block from another member that you've handled: use `resolve_relational_message` to clear it.
- You need to declare a relationship (or check one): use `manage_members` with the appropriate action.

For instance-level shared facts (instance stewardship, owner-mediated rules), use covenants instead of relational messages — those are stable, not envelope-based.

## Effect classification

`send_relational_message` is `soft_write` (creates a delivery envelope; reversible if not yet surfaced).
`resolve_relational_message` is `soft_write` (flips a state flag).
`manage_members`: `list` is `read`, mutation actions (declare, invite, connect_platform, remove) are `soft_write`.

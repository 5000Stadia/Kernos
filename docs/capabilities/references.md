# Capability: References

The reference primitive is your reach mechanism for canonical Kernos documentation and for project-deep reference material you accumulate while working. See [`architecture/reference-primitive.md`](../architecture/reference-primitive.md) for the substrate; this page covers what the tools do and when to reach for them.

## Tools at a glance

| Tool | Use it when |
|---|---|
| `request_reference` | You need to know how Kernos works (gates, covenants, a tool, an architectural choice). Provide a brief natural-language description; the matching section content arrives in the tool result. |
| `store_reference` | You've gathered material that you'll want to retrieve later — vendor API docs, research notes, project context. Provide markdown content + collection name. |
| `create_reference_collection` | You want to start a new coherent reference set (e.g., "Acme vendor API"). Run once before storing files in it. |
| `quarantine_reference` | A stored reference's reliability is suspect (source URL tampered with, content disputed). Reversible. |
| `restore_reference_from_quarantine` | Undo a quarantine. |
| `mark_reference_superseded` | A new reference replaces an older one (e.g., vendor API v2 published). Tombstones old; links new in old's provenance. |
| `move_reference_to_canvas` | You stored something that should have been canvas-shaped (mutable workspace). Tombstones the reference; you write to the canvas as a follow-up. |

All seven tools dispatch through the kernel. `request_reference` is classified `read` (no gate); the other six are `soft_write` (reversible mutations).

## When to use `request_reference`

Use it when you need canonical information about Kernos itself — capabilities, behaviors, architecture, identity, install, roadmap.

```
request_reference("how does the dispatch gate decide what's destructive")
request_reference("what are covenants and how do they compose")
request_reference("how does memory selective injection work")
```

Be specific. The cohort navigates by signal-matching against catalog metadata (section titles + one-line descriptions). A specific brief lands a more accurate match than a vague one.

The result either:

- Surfaces section content with a trust-tier annotation (canonical content has no annotation; agent-authored / external-snapshot content carries a brief framing line).
- Returns `status: ok_collection` for a collection-level match — purpose + member-file count, not content. Refine your brief and re-ask if you need a specific member file.
- Returns `status: no_match` if nothing in the visible catalog meets the brief.
- Returns `status: unavailable` with a "recataloging in progress; please retry" message if the catalog drifted from the source. Retry the next turn — the cohort will have caught up.

## When to use `store_reference`

Use it when you've encountered material that you'll want to retrieve later and that doesn't belong in canvas (mutable workspace) or memory (interaction-derived). Common cases:

- **Vendor API docs** the user shared or you fetched. Trust tier: `external_snapshot`. Include `source_url` and `fetched_at` in metadata.
- **Research notes** you compiled from multiple sources. Trust tier: `agent_authored`.
- **Project deep-reference** — design constraints, accumulated decisions, technical specs you want to refer back to.

```
store_reference(
  content="## Authentication\n\nThe API uses Bearer tokens...",
  collection="acme-vendor-api",
  filename="auth.md",
  trust_tier="external_snapshot",
  metadata={"source_url": "https://acme.example/docs/auth", "fetched_at": "2026-05-05"}
)
```

Cataloging is async — the entry becomes retrievable in one or two turns, not immediately. The store call returns success as soon as the file is on disk and the cataloging request is enqueued.

## When to create a collection first

A collection groups related files. Create one when you're starting a coherent reference set — multiple files about the same vendor's API, multiple research notes on the same topic, etc.

```
create_reference_collection(
  name="acme-vendor-api",
  purpose="Acme vendor API documentation",
  trust_tier="external_snapshot",
  refresh_policy="snapshot",
  provenance={"source_url": "https://acme.example/docs"}
)
```

If you skip the collection step and call `store_reference` with a collection name that doesn't exist, the file lands in a directory that has no `_collection.json`. That's still cataloged at the file level, but the collection-level catalog entry won't exist (so signal-matching against the collection's purpose won't surface it as a map). Create the collection when the material is genuinely a set; skip it when you have a single one-off note.

## Trust tiers — choosing one

Pick `external_snapshot` if the content was pulled from a specific URL at a specific time. The framing line "Snapshot from {url} on {fetched_at}; not canonical live truth" surfaces with retrievals so future you knows it's a moment-in-time copy.

Pick `agent_authored` if the content is your own observations or compiled notes. The framing line "Agent-authored reference (stored by {member_id})" surfaces with retrievals.

`canonical` is reserved for `docs/`-derived content and isn't selectable through `store_reference`. `quarantined` is set via the recovery primitive, not directly.

## Scope — references are domain-owned

References stored from space A are visible only to retrievals from agents acting in space A. References stored from space B are invisible to A. The `docs/` content is instance-scoped — visible to every space.

This is enforced mechanically, not policy. If you try to store from a context with no domain, the call fails with an error. If you guess an `entry_id` from another domain and try to quarantine it, the call refuses with `error: entry not visible in the caller's domain`.

You don't pass `domain_id` as a parameter. It's bound from your active space at dispatch time.

## Recovery primitives — when to reach for them

Bounded mis-classification is acceptable as long as recovery is easy. The four recovery primitives keep `references/` from becoming a junk drawer.

- **`quarantine_reference`** — flag suspect content. Auto-induction stops firing for the entry; explicit `request_reference` still surfaces it but with a quarantine caveat. Reversible.
- **`restore_reference_from_quarantine`** — undo quarantine; restore the prior trust tier.
- **`mark_reference_superseded`** — explicit replacement. Use when v2 of something published and v1 should no longer surface. Both old and new must already exist; new must be non-tombstoned. The old entry stays traceable in the catalog (tombstoned, but `provenance_metadata.superseded_by` points to the new entry).
- **`move_reference_to_canvas`** — you realize the material should have been canvas-shaped. Tombstones the reference catalog entry and stamps the canvas target in provenance. The actual canvas write is your follow-up step on the next turn.

## What references are NOT for

Reference is for **canonical** material (read-mostly, citation-shaped). It's not for:

- **Mutable workspace content** — that's canvas. If you find yourself wanting to edit a stored reference's content in place, the right move is `move_reference_to_canvas` and write to the canvas instead.
- **Interaction-derived facts** — preferences, things the user told you, decisions made together. Those go in memory, not as references.
- **Per-member private notes** — references are domain-owned, shared across members in the domain. If you need private scratch material, reach for memory or canvas.
- **Live-aliased external sources** — references are snapshots-with-provenance. The annotation makes the time-bound nature visible. If you need live data, that's a capability call (web browsing, an MCP tool), not a reference.

## How retrieval flows under the hood

When you call `request_reference("brief")`:

1. The dispatch layer binds your `domain_id` from your current space. You don't override this.
2. The visible catalog is filtered to entries with scope `instance` (docs/) or `domain:<your_space>`.
3. The cheap-tier reference cohort sees the visible catalog (entry IDs + categories + section titles + one-liners) and picks the best match for your brief — one LLM call.
4. Algorithmic injection takes over: stat the source file, recompute the hash, compare to the catalog's recorded `source_hash`. Match → read the line range, build the trust-tier annotation, return. Mismatch → fail closed with the "recataloging" message + enqueue async re-cataloging.
5. The result lands in the same tool call's response — no async hop, no waiting for the next turn.

You don't see this flow as the agent. From your perspective: you ask, you get content (or a clean failure), you continue.

## Auto-induction (passive surfacing)

The integration layer also runs a mechanical signal-match against the visible catalog when assembling each turn's briefing. If a catalog entry's metadata (one-liner, section title, category) overlaps with current conversation tokens, the entry is a candidate for auto-induction. Defaults are conservative: at most 2 sections per turn, capped at 8000 tokens, no auto-induction of `quarantined` entries, stronger overlap required for `external_snapshot` entries.

You don't reach for auto-induction explicitly — it just happens. If you notice that a relevant reference didn't surface, your brief was probably more specific than the auto-match heuristic; reach for `request_reference` directly.

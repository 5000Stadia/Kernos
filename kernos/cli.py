"""KERNOS CLI — inspect the kernel's event stream and state store.

Run via the wrapper (recommended — no venv activation needed):
  ./kernos-cli tenants
  ./kernos-cli costs <instance_id>
  ./kernos-cli events <instance_id> [--type message.received] [--limit 10] [--after 2026-03-01]
  ./kernos-cli profile <instance_id>
  ./kernos-cli knowledge <instance_id> [--subject "John"] [--category entity]
  ./kernos-cli contract <instance_id> [--capability calendar]
  ./kernos-cli contracts <instance_id>
  ./kernos-cli soul <instance_id>
  ./kernos-cli spaces <instance_id>
  ./kernos-cli entities <instance_id> [--include-inactive]
  ./kernos-cli capabilities [--tenant <instance_id>]
  ./kernos-cli files <instance_id> <space_id>

Or manually with the venv active:
  source .venv/bin/activate
  python -m kernos.cli <command>
"""
import argparse
import asyncio
import sys
import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _data_dir() -> str:
    return os.getenv("KERNOS_DATA_DIR", "./data")


_OPEN_STORES: list = []


def _state_store():
    """The state store the running bot uses (SQLite unless KERNOS_STORE_BACKEND=json)."""
    if os.getenv("KERNOS_STORE_BACKEND", "sqlite") == "json":
        from kernos.kernel.state_json import JsonStateStore
        return JsonStateStore(_data_dir())
    from kernos.kernel.state_sqlite import SqliteStateStore
    store = SqliteStateStore(_data_dir())
    _OPEN_STORES.append(store)  # closed after the command, or the process can't exit
    return store


async def _dispatch_and_close(args) -> None:
    try:
        await _dispatch(args)
    finally:
        for store in _OPEN_STORES:
            await store.close_all()


def _fmt(obj) -> str:
    return json.dumps(obj, indent=2, default=str)


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


async def cmd_events(args) -> None:
    from kernos.kernel.events import JsonEventStream

    stream = JsonEventStream(_data_dir())
    event_types = [args.event_type] if args.event_type else None
    events = await stream.query(
        instance_id=args.instance_id,
        event_types=event_types,
        after=args.after,
        limit=args.limit,
    )
    if not events:
        print(f"No events found for instance '{args.instance_id}'.")
        return
    print(f"{'─' * 60}")
    print(f"  Events for {args.instance_id}  ({len(events)} shown)")
    print(f"{'─' * 60}")
    for e in events:
        print(f"\n[{e.timestamp}] {e.type}  ({e.id})")
        print(f"  source: {e.source}")
        if e.payload:
            for k, v in e.payload.items():
                if k == "content" and isinstance(v, str) and len(v) > 80:
                    v = v[:80] + "…"
                print(f"  {k}: {v}")


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------


async def cmd_profile(args) -> None:

    state = _state_store()
    profile = await state.get_instance_profile(args.instance_id)
    if profile is None:
        print(f"No profile found for instance '{args.instance_id}'.")
        return
    from dataclasses import asdict
    print(f"{'─' * 60}")
    print(f"  Tenant Profile: {args.instance_id}")
    print(f"{'─' * 60}")
    print(_fmt(asdict(profile)))


# ---------------------------------------------------------------------------
# knowledge
# ---------------------------------------------------------------------------


async def cmd_knowledge(args) -> None:
    from datetime import datetime, timezone
    from kernos.kernel.retrieval import compute_quality_score

    state = _state_store()
    entries = await state.query_knowledge(
        instance_id=args.instance_id,
        subject=args.subject,
        category=args.category,
        active_only=not args.include_archived,
        limit=args.limit,
    )
    # Newest first
    entries = sorted(entries, key=lambda e: e.created_at, reverse=True)
    if not entries:
        print(f"No knowledge entries found for '{args.instance_id}'.")
        return
    print(f"{'─' * 60}")
    print(f"  Knowledge: {args.instance_id}  ({len(entries)} entries)")
    print(f"{'─' * 60}")
    now_iso = datetime.now(timezone.utc).isoformat()
    for e in entries:
        status = "" if e.active else "[archived] "
        q = compute_quality_score(e, "", now_iso)
        # Component breakdown
        from kernos.kernel.retrieval import _days_since
        days_old = _days_since(e.created_at, now_iso)
        recency = max(1.0 - (days_old / 90.0), 0.1)
        conf_map = {"stated": 1.0, "observed": 0.8, "inferred": 0.6, "high": 0.9, "medium": 0.7, "low": 0.5}
        conf = conf_map.get(e.confidence, 0.6)
        reinf = min(e.reinforcement_count / 5.0, 1.0)
        print(f"\n  {status}[{e.confidence}] {e.category}: \"{e.content}\" ({e.created_at[:10]})")
        print(f"    subject: {e.subject} | archetype: {e.lifecycle_archetype} | Q={q:.2f} (recency={recency:.2f} conf={conf:.1f} reinf={reinf:.1f})")
        if e.foresight_signal:
            expires = f" (expires {e.foresight_expires[:10]})" if e.foresight_expires else ""
            print(f"    foresight: {e.foresight_signal}{expires}")
        if e.supersedes:
            print(f"    supersedes: {e.supersedes}")
        if not e.active:
            print(f"    (inactive)")


# ---------------------------------------------------------------------------
# contract (backwards-compatible) + contracts (new grouped display)
# ---------------------------------------------------------------------------


async def cmd_contract(args) -> None:

    state = _state_store()
    rules = await state.get_contract_rules(
        instance_id=args.instance_id,
        capability=args.capability,
        active_only=not args.include_inactive,
    )
    if not rules:
        print(f"No contract rules found for '{args.instance_id}'.")
        return
    print(f"{'─' * 60}")
    print(f"  Behavioral Contract: {args.instance_id}  ({len(rules)} rules)")
    print(f"{'─' * 60}")
    for r in rules:
        status = "✓" if r.active else "✗"
        print(f"\n  [{status}] {r.rule_type.upper()} ({r.capability})")
        print(f"      {r.description}")
        print(f"      source: {r.source} | id: {r.id}")


async def cmd_contracts(args) -> None:
    """Display behavioral contract rules grouped by type."""

    state = _state_store()
    rules = await state.get_contract_rules(instance_id=args.instance_id, active_only=True)
    if not rules:
        print(f"No contract rules found for '{args.instance_id}'.")
        return

    print(f"{'─' * 60}")
    print(f"  Covenant Rules for {args.instance_id}")
    print(f"{'─' * 60}")

    order = ["must", "must_not", "preference", "escalation"]
    labels = {
        "must": "MUST",
        "must_not": "MUST NOT",
        "preference": "PREFERENCE",
        "escalation": "ESCALATION",
    }
    grouped: dict[str, list] = {k: [] for k in order}
    for r in rules:
        if r.rule_type in grouped:
            grouped[r.rule_type].append(r)

    for key in order:
        group_rules = grouped[key]
        if not group_rules:
            continue
        print(f"\n  {labels[key]}:")
        for r in group_rules:
            source_label = f"[{r.source}]"
            layer_label = f"[{r.layer}]"
            tier_label = f"tier:{r.enforcement_tier}"
            print(f"    - {r.description} {source_label} {layer_label} {tier_label}")
            if r.graduation_eligible:
                print(f"      graduation eligible ({r.graduation_positive_signals}/{r.graduation_threshold} signals)")


# ---------------------------------------------------------------------------
# soul
# ---------------------------------------------------------------------------


async def cmd_soul(args) -> None:
    """Display the hatched soul for an instance."""

    state = _state_store()
    soul = await state.get_soul(args.instance_id)
    if soul is None:
        print(f"No soul found for instance '{args.instance_id}'. (Not yet hatched.)")
        return

    print(f"{'─' * 60}")
    print(f"  Soul for {args.instance_id}")
    print(f"{'─' * 60}")
    print(f"  Hatched:          {soul.hatched_at if soul.hatched else 'not yet'}")
    print(f"  Bootstrap:        {'graduated' if soul.bootstrap_graduated else 'active'}")
    print(f"  Interactions:     {soul.interaction_count}")
    print(f"  User:             {soul.user_name if soul.user_name else '(not yet known)'}")
    print(f"  Agent name:       {soul.agent_name if soul.agent_name else '(default)'}")
    print(f"  Style:            {soul.communication_style if soul.communication_style else '(not yet determined)'}")
    if soul.personality_notes:
        trunc = soul.personality_notes[:120] + "…" if len(soul.personality_notes) > 120 else soul.personality_notes
        print(f"  Personality:      {trunc}")
    if soul.user_context:
        trunc = soul.user_context[:120] + "…" if len(soul.user_context) > 120 else soul.user_context
        print(f"  User context:     {trunc}")


# ---------------------------------------------------------------------------
# spaces
# ---------------------------------------------------------------------------


async def cmd_spaces(args) -> None:
    """Display context spaces for an instance."""

    state = _state_store()
    spaces = await state.list_context_spaces(args.instance_id)
    if not spaces:
        print(f"No context spaces found for '{args.instance_id}'.")
        return

    print(f"{'─' * 60}")
    print(f"  Context Spaces: {args.instance_id}  ({len(spaces)} spaces)")
    print(f"{'─' * 60}")
    for s in spaces:
        default_label = " [default]" if s.is_default else ""
        print(f"\n  [{s.status.upper()}] {s.name}{default_label}  ({s.space_type})")
        print(f"    id: {s.id}")
        if s.description:
            print(f"    {s.description}")
        if s.posture:
            print(f"    posture: {s.posture}")
        if s.last_active_at:
            print(f"    last active: {s.last_active_at[:10]}")


# ---------------------------------------------------------------------------
# entities
# ---------------------------------------------------------------------------


async def cmd_entities(args) -> None:
    """Display entity nodes for an instance."""

    state = _state_store()
    entities = await state.query_entity_nodes(args.instance_id, active_only=not args.include_inactive)
    if not entities:
        print(f"No entities found for '{args.instance_id}'.")
        return

    print(f"{'─' * 60}")
    print(f"  Entities: {args.instance_id}  ({len(entities)} entities)")
    print(f"{'─' * 60}")
    for e in entities:
        status = "" if e.active else "[inactive] "
        type_label = f" ({e.entity_type})" if e.entity_type else ""
        rel_label = f" — {e.relationship_type}" if e.relationship_type else ""
        print(f"\n  {status}{e.canonical_name}{type_label}{rel_label}")
        print(f"    id: {e.id}")
        if e.aliases:
            print(f"    aliases: {', '.join(e.aliases)}")
        if e.contact_phone:
            print(f"    phone: {e.contact_phone}")
        if e.contact_email:
            print(f"    email: {e.contact_email}")
        if e.contact_address:
            print(f"    address: {e.contact_address}")
        if e.contact_website:
            print(f"    website: {e.contact_website}")
        if e.summary:
            trunc = e.summary[:120] + "…" if len(e.summary) > 120 else e.summary
            print(f"    summary: {trunc}")
        if e.first_seen:
            print(f"    first seen: {e.first_seen[:10]}  last seen: {e.last_seen[:10]}")
        if e.knowledge_entry_ids:
            print(f"    knowledge entries: {len(e.knowledge_entry_ids)}")


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------


async def cmd_files(args) -> None:
    """Display files for an instance's context space."""
    from kernos.kernel.files import FileService

    service = FileService(_data_dir())
    instance_id = args.instance_id
    space_id = args.space_id

    manifest = await service.load_manifest(instance_id, space_id)

    print(f"{'─' * 60}")
    print(f"  Files: {instance_id} / {space_id}")
    print(f"{'─' * 60}")

    if not manifest:
        print("  No files in this space.")
    else:
        from kernos.utils import _safe_name
        files_dir = (
            Path(_data_dir())
            / _safe_name(instance_id)
            / "spaces"
            / space_id
            / "files"
        )
        print(f"  {len(manifest)} file(s):\n")
        for name, desc in sorted(manifest.items()):
            file_path = files_dir / name
            size = file_path.stat().st_size if file_path.exists() else 0
            print(f"  {name}  ({size} bytes)")
            print(f"    {desc}")

    # .deleted directory status
    from kernos.utils import _safe_name as _sn
    deleted_dir = (
        Path(_data_dir())
        / _sn(instance_id)
        / "spaces"
        / space_id
        / "files"
        / ".deleted"
    )
    if deleted_dir.exists():
        deleted_files = list(deleted_dir.iterdir())
        print(f"\n  .deleted/: {len(deleted_files)} file(s) preserved for recovery")
        for f in sorted(deleted_files):
            print(f"    {f.name}")
    else:
        print("\n  .deleted/: empty")


# ---------------------------------------------------------------------------
# compaction
# ---------------------------------------------------------------------------


async def cmd_compaction(args) -> None:
    """Display compaction state for an instance's context spaces."""
    from kernos.kernel.compaction import CompactionService
    from kernos.kernel.tokens import EstimateTokenAdapter

    state = _state_store()
    adapter = EstimateTokenAdapter()

    # Create a minimal compaction service just for loading state
    service = CompactionService(
        state=state, reasoning=None, token_adapter=adapter, data_dir=_data_dir()  # type: ignore[arg-type]
    )

    if args.space_id:
        # Show specific space
        comp_state = await service.load_state(args.instance_id, args.space_id)
        if comp_state is None:
            print(f"No compaction state found for space '{args.space_id}'.")
            return
        _print_compaction_state(comp_state)

        # Show first/last 10 lines of active document
        doc = await service.load_document(args.instance_id, args.space_id)
        if doc:
            lines = doc.splitlines()
            print(f"\n  Active document ({len(lines)} lines):")
            for line in lines[:10]:
                print(f"    {line}")
            if len(lines) > 20:
                print(f"    ... ({len(lines) - 20} lines omitted) ...")
                for line in lines[-10:]:
                    print(f"    {line}")
            elif len(lines) > 10:
                for line in lines[10:]:
                    print(f"    {line}")
    else:
        # Show all spaces
        spaces = await state.list_context_spaces(args.instance_id)
        if not spaces:
            print(f"No context spaces found for '{args.instance_id}'.")
            return

        print(f"{'─' * 60}")
        print(f"  Compaction State: {args.instance_id}")
        print(f"{'─' * 60}")

        for space in spaces:
            comp_state = await service.load_state(args.instance_id, space.id)
            status_label = "[no compaction state]" if comp_state is None else ""
            default_label = " [default]" if space.is_default else ""
            print(f"\n  {space.name}{default_label} ({space.id}) {status_label}")
            if comp_state:
                print(f"    compactions: {comp_state.global_compaction_number} (current rotation: {comp_state.compaction_number})")
                print(f"    archives: {comp_state.archive_count}")
                print(f"    history_tokens: {comp_state.history_tokens}")
                print(f"    document_budget: {comp_state.document_budget}")
                print(f"    headroom: {comp_state.conversation_headroom}")
                print(f"    cumulative_new_tokens: {comp_state.cumulative_new_tokens} / ceiling: {comp_state.message_ceiling}")
                if comp_state.last_compaction_at:
                    print(f"    last compaction: {comp_state.last_compaction_at[:19]}")


def _print_compaction_state(cs) -> None:
    """Print a single CompactionState in detail."""
    print(f"{'─' * 60}")
    print(f"  Compaction State: {cs.space_id}")
    print(f"{'─' * 60}")
    print(f"  compaction_number:        {cs.compaction_number}")
    print(f"  global_compaction_number: {cs.global_compaction_number}")
    print(f"  archive_count:            {cs.archive_count}")
    print(f"  history_tokens:           {cs.history_tokens}")
    print(f"  document_budget:          {cs.document_budget}")
    print(f"  conversation_headroom:    {cs.conversation_headroom}")
    print(f"  cumulative_new_tokens:    {cs.cumulative_new_tokens}")
    print(f"  message_ceiling:          {cs.message_ceiling}")
    print(f"  index_tokens:             {cs.index_tokens}")
    print(f"  _context_def_tokens:      {cs._context_def_tokens}")
    print(f"  _system_overhead:         {cs._system_overhead}")
    if cs.last_compaction_at:
        print(f"  last_compaction_at:       {cs.last_compaction_at}")


# ---------------------------------------------------------------------------
# create-space
# ---------------------------------------------------------------------------


async def cmd_create_space(args) -> None:
    """Create a new context space for an instance."""
    import uuid
    from kernos.kernel.spaces import ContextSpace
    from datetime import datetime, timezone

    state = _state_store()
    now = datetime.now(timezone.utc).isoformat()
    space_id = f"space_{uuid.uuid4().hex[:8]}"

    space = ContextSpace(
        id=space_id,
        instance_id=args.instance_id,
        name=args.name,
        description=args.description or "",
        space_type=args.type or "project",
        status="active",
        posture=args.posture or "",
        created_at=now,
        last_active_at=now,
        is_default=False,
    )
    await state.save_context_space(space)
    print(f"Created context space: {space_id}")
    print(f"  Name: {args.name}")
    if args.posture:
        print(f"  Posture: {args.posture}")


# ---------------------------------------------------------------------------
# costs
# ---------------------------------------------------------------------------


async def cmd_costs(args) -> None:
    from kernos.kernel.events import JsonEventStream

    stream = JsonEventStream(_data_dir())
    events = await stream.query(
        instance_id=args.instance_id,
        event_types=["reasoning.response"],
        after=args.after,
        before=args.before,
        limit=100_000,
    )
    if not events:
        print(f"No reasoning events found for '{args.instance_id}'.")
        return

    total_input = sum(e.payload.get("input_tokens", 0) for e in events)
    total_output = sum(e.payload.get("output_tokens", 0) for e in events)
    total_cost = sum(e.payload.get("estimated_cost_usd", 0.0) for e in events)
    total_tokens = total_input + total_output

    print(f"{'─' * 60}")
    print(f"  Cost Summary: {args.instance_id}")
    if args.after:
        print(f"  After: {args.after}")
    if args.before:
        print(f"  Before: {args.before}")
    print(f"{'─' * 60}")
    print(f"  Total API calls:    {len(events)}")
    print(f"  Total tokens:       {total_tokens:,}  (in: {total_input:,}  out: {total_output:,})")
    print(f"  Estimated cost:     ${total_cost:.6f}")
    print()
    print("  Recent calls (last 10):")
    for e in events[-10:]:
        p = e.payload
        ts = e.timestamp[:19].replace("T", " ")
        model = p.get("model", "?")
        inp = p.get("input_tokens", 0)
        out = p.get("output_tokens", 0)
        cost = p.get("estimated_cost_usd", 0.0)
        trigger = p.get("trigger", "")
        print(
            f"    {ts}  {model}  {inp}+{out} tokens  ${cost:.6f}"
            + (f"  [{trigger}]" if trigger else "")
        )


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------


async def cmd_tasks(args) -> None:
    from kernos.kernel.events import JsonEventStream

    stream = JsonEventStream(_data_dir())
    completed = await stream.query(
        instance_id=args.instance_id,
        event_types=["task.completed"],
        limit=args.limit,
    )
    failed = await stream.query(
        instance_id=args.instance_id,
        event_types=["task.failed"],
        limit=args.limit,
    )

    # Merge and sort by timestamp descending
    all_events = sorted(completed + failed, key=lambda e: e.timestamp, reverse=True)
    all_events = all_events[: args.limit]

    if not all_events:
        print(f"No task events found for instance '{args.instance_id}'.")
        return

    print(f"{'─' * 60}")
    print(f"  Tasks for {args.instance_id}  ({len(all_events)} shown)")
    print(f"{'─' * 60}")
    for e in all_events:
        p = e.payload
        ts = e.timestamp[:19].replace("T", " ")
        task_id = p.get("task_id", "?")[:20]
        task_type = p.get("task_type", "?")
        if e.type == "task.completed":
            dur = p.get("duration_ms", 0)
            cost = p.get("estimated_cost_usd", 0.0)
            print(f"[{ts}] {task_id}... {task_type} COMPLETED ({dur}ms, ${cost:.3f})")
        else:
            err = p.get("error_type", "?")
            print(f"[{ts}] {task_id}... {task_type} FAILED ({err})")


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------


async def cmd_capabilities(args) -> None:
    """Display capability registry.

    If --tenant is provided, reads from the instance's persisted profile for
    accurate runtime status. Otherwise shows the static catalog with honest
    status values — no env-var inference, no invented labels.
    """
    from kernos.capability.known import KNOWN_CAPABILITIES
    from kernos.capability.registry import CapabilityStatus

    # Optionally load tenant-specific status from persisted state
    tenant_cap_map: dict[str, str] = {}
    if args.tenant:
        state = _state_store()
        profile = await state.get_instance_profile(args.tenant)
        if profile and profile.capabilities:
            tenant_cap_map = profile.capabilities

    print(f"{'─' * 60}")
    print("  Capability Registry")
    if args.tenant:
        print(f"  Tenant: {args.tenant}")
    print(f"{'─' * 60}")

    for cap in KNOWN_CAPABILITIES:
        # Use persisted tenant state if available; otherwise use catalog status directly.
        # Never infer status from env vars — use CapabilityStatus vocabulary only.
        if tenant_cap_map and cap.name in tenant_cap_map:
            status_label = tenant_cap_map[cap.name].upper()
        else:
            status_label = cap.status.value.upper()

        print(f"\n  [{status_label}] {cap.display_name}")
        print(f"      {cap.description}")
        if cap.setup_hint:
            print(f'      Setup: "{cap.setup_hint}"')
        if cap.server_name:
            print(f"      Server: {cap.server_name}")


# ---------------------------------------------------------------------------
# tenants
# ---------------------------------------------------------------------------


async def cmd_tenants(args) -> None:
    data_path = Path(_data_dir())
    if not data_path.exists():
        print("Data directory not found. No tenants yet.")
        return

    dirs = sorted(d for d in data_path.iterdir() if d.is_dir())
    if not dirs:
        print("No tenant directories found.")
        return

    print(f"{'─' * 60}")
    print(f"  Tenants in {data_path}")
    print(f"{'─' * 60}")
    for d in dirs:
        profile_path = d / "state" / "profile.json"
        tenant_path = d / "tenant.json"
        if profile_path.exists():
            import json as _json
            p = _json.loads(profile_path.read_text())
            tid = p.get("instance_id", d.name)
            status = p.get("status", "?")
            platforms = list(p.get("platforms", {}).keys())
            plat_str = f"  [{', '.join(platforms)}]" if platforms else ""
            print(f"  {d.name}  →  {tid}  ({status}){plat_str}")
        elif tenant_path.exists():
            import json as _json
            t = _json.loads(tenant_path.read_text())
            print(f"  {d.name}  →  {t.get('instance_id', '?')}  ({t.get('status', '?')})  [legacy]")
        else:
            print(f"  {d.name}  [no profile]")


# ---------------------------------------------------------------------------
# Backfill Embeddings
# ---------------------------------------------------------------------------


async def cmd_backfill_embeddings(args) -> None:
    """Generate embeddings for knowledge entries that lack them."""
    import os
    from dotenv import load_dotenv
    load_dotenv()

    from kernos.kernel.embedding_store import JsonEmbeddingStore
    from kernos.kernel.embeddings import EmbeddingService

    api_key = os.getenv("VOYAGE_API_KEY", "")
    if not api_key:
        print("ERROR: VOYAGE_API_KEY not set in environment. Cannot generate embeddings.")
        return

    data_dir = _data_dir()
    state = _state_store()
    embed_store = JsonEmbeddingStore(data_dir)
    embed_service = EmbeddingService(api_key)

    instance_id = args.instance_id
    all_entries = await state.query_knowledge(instance_id, active_only=True, limit=500)

    missing = []
    for entry in all_entries:
        existing = await embed_store.get(instance_id, entry.id)
        if existing is None:
            missing.append(entry)

    print(f"Total active entries: {len(all_entries)}")
    print(f"Entries missing embeddings: {len(missing)}")

    if not missing:
        print("Nothing to backfill.")
        return

    success = 0
    failed = 0
    for entry in missing:
        try:
            # Content only, as the live write paths (harvest, note_this) embed.
            embedding = await embed_service.embed(entry.content)
            await embed_store.save(instance_id, entry.id, embedding)
            success += 1
            print(f"  ✓ {entry.id}: {entry.content[:60]}")
        except Exception as exc:
            failed += 1
            print(f"  ✗ {entry.id}: {exc}")

    print(f"\nBackfill complete: {success} embedded, {failed} failed")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


async def _dispatch(args) -> None:
    if args.command == "events":
        await cmd_events(args)
    elif args.command == "profile":
        await cmd_profile(args)
    elif args.command == "knowledge":
        await cmd_knowledge(args)
    elif args.command == "contract":
        await cmd_contract(args)
    elif args.command == "contracts":
        await cmd_contracts(args)
    elif args.command == "soul":
        await cmd_soul(args)
    elif args.command == "spaces":
        await cmd_spaces(args)
    elif args.command == "entities":
        await cmd_entities(args)
    elif args.command == "costs":
        await cmd_costs(args)
    elif args.command == "tenants":
        await cmd_tenants(args)
    elif args.command == "tasks":
        await cmd_tasks(args)
    elif args.command == "capabilities":
        await cmd_capabilities(args)
    elif args.command == "create-space":
        await cmd_create_space(args)
    elif args.command == "files":
        await cmd_files(args)
    elif args.command == "compaction":
        await cmd_compaction(args)
    elif args.command == "backfill-embeddings":
        await cmd_backfill_embeddings(args)
    elif args.command == "setup":
        cmd_setup(args)
    else:
        print("Unknown command. Run with --help for usage.")


def cmd_setup(args) -> None:
    """`kernos setup` dispatcher.

    `kernos setup llm` routes to the interactive LLM console (sync,
    zero LLM calls). `kernos setup` with no subtarget runs the
    INSTALL-FOR-STOCK-CONNECTORS first-run flow (service-state
    bootstrap; TTY interactive or non-interactive with --enable-
    services / --all-enabled / --non-interactive).
    """
    target = getattr(args, "setup_target", None) or ""

    if target == "llm":
        from kernos.setup.console import run_setup
        rest = list(getattr(args, "setup_args", []) or [])
        sys_exit = run_setup(rest)
        if sys_exit:
            sys.exit(sys_exit)
        return

    if target in ("", "services"):
        # First-run service-state setup. The setup-args list carries
        # the post-target flags (--data-dir, --non-interactive, etc).
        from kernos.setup.first_run import (
            cmd_setup_first_run,
        )
        ns = _parse_first_run_args(list(getattr(args, "setup_args", []) or []))
        rc = cmd_setup_first_run(ns)
        if rc:
            sys.exit(rc)
        return

    print(
        "Usage:\n"
        "  kernos setup                       interactive service-state setup\n"
        "  kernos setup --non-interactive --enable-services notion,github\n"
        "  kernos setup --all-enabled\n"
        "  kernos setup llm [status]          interactive LLM configuration"
    )


def _parse_first_run_args(rest: list[str]) -> argparse.Namespace:
    """Parse the trailing flags after `kernos setup` for the first-run flow."""
    parser = argparse.ArgumentParser(prog="kernos setup")
    from kernos.setup.first_run import add_first_run_args
    add_first_run_args(parser)
    return parser.parse_args(rest)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="kernos.cli",
        description="Inspect the KERNOS event stream and state store.",
    )
    subparsers = parser.add_subparsers(dest="command")

    # events
    p = subparsers.add_parser("events", help="View recent events for an instance")
    p.add_argument("instance_id")
    p.add_argument("--type", dest="event_type", help="Filter by event type (e.g. message.received)")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--after", help="ISO date/timestamp — show events after this time")

    # profile
    p = subparsers.add_parser("profile", help="View tenant profile")
    p.add_argument("instance_id")

    # knowledge
    p = subparsers.add_parser("knowledge", help="View knowledge entries")
    p.add_argument("instance_id")
    p.add_argument("--subject", help="Filter by subject (substring match)")
    p.add_argument("--category", help="Filter by category (entity/fact/preference/pattern)")
    p.add_argument("--include-archived", action="store_true")
    p.add_argument("--limit", type=int, default=50, help="Max entries to show (default 50)")

    # contract (backwards-compatible)
    p = subparsers.add_parser("contract", help="View behavioral contract rules")
    p.add_argument("instance_id")
    p.add_argument("--capability", help="Filter by capability (calendar/email/general)")
    p.add_argument("--include-inactive", action="store_true")

    # contracts (new — grouped by type)
    p = subparsers.add_parser("contracts", help="View behavioral contracts grouped by type")
    p.add_argument("instance_id")

    # soul
    p = subparsers.add_parser("soul", help="View hatched soul for an instance")
    p.add_argument("instance_id")

    # spaces
    p = subparsers.add_parser("spaces", help="View context spaces for an instance")
    p.add_argument("instance_id")

    # entities
    p = subparsers.add_parser("entities", help="View entity nodes for an instance")
    p.add_argument("instance_id")
    p.add_argument("--include-inactive", action="store_true")

    # costs
    p = subparsers.add_parser("costs", help="View cost summary from reasoning events")
    p.add_argument("instance_id")
    p.add_argument("--after", help="ISO date — costs after this date")
    p.add_argument("--before", help="ISO date — costs before this date")

    # tenants
    subparsers.add_parser("tenants", help="List all instances")

    # tasks
    p = subparsers.add_parser("tasks", help="View recent task lifecycle events for an instance")
    p.add_argument("instance_id")
    p.add_argument("--limit", type=int, default=20)

    # capabilities
    p = subparsers.add_parser("capabilities", help="Show capability registry")
    p.add_argument("--tenant", dest="instance", help="Tenant ID for persisted runtime status")

    # files
    p = subparsers.add_parser("files", help="View files for a context space")
    p.add_argument("instance_id")
    p.add_argument("space_id")

    # compaction
    p = subparsers.add_parser("compaction", help="View compaction state for an instance")
    p.add_argument("instance_id")
    p.add_argument("space_id", nargs="?", default=None, help="Optional space ID for detailed view")

    # backfill-embeddings
    p = subparsers.add_parser("backfill-embeddings", help="Generate embeddings for entries that lack them")
    p.add_argument("instance_id")

    # create-space
    p = subparsers.add_parser("create-space", help="Create a new context space")
    p.add_argument("instance_id")
    p.add_argument("--name", required=True, help="Space name")
    p.add_argument("--type", default="project", help="Space type (project/domain/managed_resource)")
    p.add_argument("--posture", help="Working style posture text")
    p.add_argument("--description", help="One-line description")

    # setup (interactive LLM configuration)
    p = subparsers.add_parser(
        "setup",
        help="Interactive setup (e.g. `kernos setup llm`)",
    )
    p.add_argument(
        "setup_target", nargs="?", default="",
        help="What to set up (currently only 'llm').",
    )
    p.add_argument(
        "setup_args", nargs=argparse.REMAINDER,
        help="Subcommand arguments (e.g. 'status').",
    )

    # services (INSTALL-FOR-STOCK-CONNECTORS, top-level enable/disable/list/info)
    from kernos.setup.services_cli import add_services_subcommand
    add_services_subcommand(subparsers)

    # credentials (top-level wrapper around the existing module-level CLI;
    # forwards remainder args to kernos.kernel.credentials_cli.main).
    p = subparsers.add_parser(
        "credentials",
        help=(
            "Manage member credentials for external services "
            "(onboard, revoke, refresh, list, info)."
        ),
    )
    p.add_argument(
        "credentials_args", nargs=argparse.REMAINDER,
        help="Forwarded to `python -m kernos.kernel.credentials_cli`.",
    )

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    # Sync commands run directly, async commands go through asyncio.run().
    if args.command == "setup":
        cmd_setup(args)
        return
    if args.command == "services":
        from kernos.setup.services_cli import dispatch_services
        rc = dispatch_services(args)
        if rc:
            sys.exit(rc)
        return
    if args.command == "credentials":
        from kernos.kernel.credentials_cli import main as credentials_main
        rc = credentials_main(list(getattr(args, "credentials_args", []) or []))
        if rc:
            sys.exit(rc)
        return

    asyncio.run(_dispatch_and_close(args))


if __name__ == "__main__":
    main()

"""CANVAS-SECTION-MARKERS + GARDENER — end-to-end lifecycle integration.

Spec: SPEC-CANVAS-SECTION-MARKERS + GARDENER COHORT, integration test.

Scaled down from the spec's originally-proposed lifecycle (which
included preference capture) because Pillar 5 is deferred per the design review
review. This test exercises: canvas_create-with-intent → Gardener
picks a pattern → pages instantiate → member writes content → Gardener
runs continuous-evolution heuristics → high-confidence proposal
coalesces.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from kernos.kernel.canvas import (
    CanvasService,
    canvas_dir,
    parse_frontmatter,
    serialize_frontmatter,
)
from kernos.kernel.gardener import (
    GardenerService,
    SPLIT_SECTION_LINE_THRESHOLD,
    WORKFLOW_PATTERNS_CANVAS_NAME,
)
from kernos.kernel.instance_db import InstanceDB


INSTANCE = "inst_lifecycle"
OPERATOR = "member:inst_lifecycle:owner"


class _StubReasoning:
    """Returns a fixed JSON payload for each complete_simple call."""
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0

    async def complete_simple(self, *, system_prompt, user_content, chain,
                               output_schema=None, max_tokens=1024):
        self.calls += 1
        return json.dumps(self.payload)


@pytest.fixture
async def lifecycle_env(tmp_path):
    """Instance with Workflow Patterns canvas seeded + Gardener ready."""
    idb = InstanceDB(str(tmp_path))
    await idb.connect()
    await idb.create_member(OPERATOR, "Owner", "owner", "")
    emitted: list[tuple] = []

    async def emit(iid, et, payload, *, member_id=""):
        emitted.append((et, payload))

    svc = CanvasService(
        instance_db=idb, data_dir=str(tmp_path), event_emit=emit,
    )

    # Seed a Workflow Patterns canvas with the software-development pattern
    # (the one Pillar 3 integration tests already validate against).
    wp = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name=WORKFLOW_PATTERNS_CANVAS_NAME, scope="team",
    )
    pattern_body = Path("docs/workflow-patterns/01-software-development.md").read_text()
    _, stripped = parse_frontmatter(pattern_body)
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=wp.canvas_id,
        page_slug="01-software-development.md",
        body=stripped, writer_member_id=OPERATOR,
        title="Software Development", page_type="note", state="current",
        frontmatter_overrides={"pattern": "software-development"},
    )

    reasoning = _StubReasoning({
        "action": "pick_pattern",
        "confidence": "high",
        "pattern": "software-development",
        "rationale": "backend service intent matches software-development dials",
    })
    gardener = GardenerService(
        canvas_service=svc, instance_db=idb,
        reasoning_service=reasoning,
        event_emit=emit,
    )
    yield gardener, svc, idb, reasoning, emitted, tmp_path
    await gardener.wait_idle()
    await idb.close()


async def test_full_lifecycle_create_populate_evolve(lifecycle_env):
    """Create a canvas with intent → Gardener picks + instantiates → member
    writes a large page → continuous evolution fires split proposal."""
    gardener, svc, idb, reasoning, emitted, tmp_path = lifecycle_env

    # --- STEP 1: Create the target canvas (mimics the canvas_create tool).
    target = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="Kernos Build", scope="team",
    )

    # --- STEP 2: Gardener consults LLM, picks pattern, instantiates pages.
    result = await gardener.apply_initial_shape(
        instance_id=INSTANCE,
        canvas_id=target.canvas_id,
        canvas_name="Kernos Build",
        scope="team",
        creator_member_id=OPERATOR,
        intent="building a backend service with specs + decision ledger",
    )
    assert result["source"] == "gardener"
    assert result["pattern"] == "software-development"
    assert result["pages_created"] >= 1

    # --- STEP 3: canvas.yaml records the pattern.
    yaml_path = canvas_dir(str(tmp_path), INSTANCE, target.canvas_id) / "canvas.yaml"
    yaml_data = yaml.safe_load(yaml_path.read_text())
    assert yaml_data["pattern"] == "software-development"

    # --- STEP 4: Pattern-declared pages exist on disk.
    pages = await svc.page_list(instance_id=INSTANCE, canvas_id=target.canvas_id)
    paths = {p["path"] for p in pages}
    assert "charter.md" in paths
    assert "architecture.md" in paths

    # --- STEP 5: Member writes a large page into the canvas.
    big_section = "\n".join(["detail line"] * (SPLIT_SECTION_LINE_THRESHOLD + 5))
    member_body = (
        f"## Scope\n\n{big_section}\n\n"
        f"## Approach\n\n{big_section}\n\n"
        f"## Rollout\n\n{big_section}\n"
    )
    await svc.page_write(
        instance_id=INSTANCE, canvas_id=target.canvas_id,
        page_slug="specs/launch.md",
        body=member_body, writer_member_id=OPERATOR,
        title="Launch spec", page_type="decision", state="proposed",
    )

    # --- STEP 6: Gardener continuous-evolution fires on that write.
    await gardener._dispatch(
        INSTANCE, "canvas.page.created",
        {
            "canvas_id": target.canvas_id,
            "page_path": "specs/launch.md",
            "writer_member_id": OPERATOR,
        },
    )

    # --- STEP 7: Split proposal coalesced; high-confidence surface pending.
    assert gardener.coalescer.buffered_count(target.canvas_id) >= 1
    drained = await gardener.drain_proposals(canvas_id=target.canvas_id)
    assert len(drained) >= 1
    actions = {p.action for p in drained}
    assert "propose_split" in actions

    # --- STEP 8: canvas.pattern_applied audit event fired at step 2.
    audit_events = [
        e for e in emitted if e[0] == "canvas.pattern_applied"
    ]
    assert len(audit_events) >= 1
    assert audit_events[-1][1]["pattern"] == "software-development"


async def test_lifecycle_with_unmatched_intent_keeps_canvas_usable(lifecycle_env):
    """An unmatched intent produces a minimal canvas that still works."""
    gardener, svc, idb, _, emitted, tmp_path = lifecycle_env

    # Swap the reasoning stub to return action=none (no pattern matches).
    gardener._reasoning = _StubReasoning({"action": "none", "confidence": "low"})

    target = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="Weird Thing", scope="personal",
    )
    result = await gardener.apply_initial_shape(
        instance_id=INSTANCE,
        canvas_id=target.canvas_id,
        canvas_name="Weird Thing",
        scope="personal",
        creator_member_id=OPERATOR,
        intent="something that doesn't match any pattern",
    )
    assert result["pattern"] == "unmatched"

    # Canvas still exists + index.md is writeable.
    r = await svc.page_write(
        instance_id=INSTANCE, canvas_id=target.canvas_id,
        page_slug="notes.md",
        body="Member's notes.\n", writer_member_id=OPERATOR,
    )
    assert r.ok

    # canvas.yaml records the unmatched flag.
    yaml_path = canvas_dir(str(tmp_path), INSTANCE, target.canvas_id) / "canvas.yaml"
    yaml_data = yaml.safe_load(yaml_path.read_text())
    assert yaml_data["pattern"] == "unmatched"


async def test_pattern_cache_reloads_after_workflow_canvas_edit(lifecycle_env):
    """Editing the Workflow Patterns canvas invalidates Gardener's cache."""
    gardener, svc, idb, _, _, _ = lifecycle_env

    # Trigger initial load via consult_initial_shape on a throwaway canvas.
    target = await svc.create(
        instance_id=INSTANCE, creator_member_id=OPERATOR,
        name="Preload", scope="personal",
    )
    await gardener.apply_initial_shape(
        instance_id=INSTANCE, canvas_id=target.canvas_id,
        canvas_name="Preload", scope="personal", creator_member_id=OPERATOR,
        explicit_pattern="software-development",
    )
    assert gardener.patterns.loaded

    # Fire a page-change event on the Workflow Patterns canvas.
    wp_row = await idb.find_canvas_by_name(
        name=WORKFLOW_PATTERNS_CANVAS_NAME, scope="team",
    )
    await gardener.on_canvas_event(
        INSTANCE, "canvas.page.changed",
        {"canvas_id": wp_row["canvas_id"], "page_path": "01-software-development.md"},
    )
    await gardener.wait_idle()
    # Cache invalidated.
    assert gardener.patterns.loaded is False

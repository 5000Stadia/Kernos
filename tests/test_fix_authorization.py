"""USER-INITIATED-IMPROVEMENT-TRIGGER-V1 Phase A — fix_authorization
schema + classifier + validator tests.

Coverage:
* AC2 — fix_authorization table + record tool, idempotent,
  unique index on request_id.
* AC6 — classify_fix_scope routes every documented
  touches_paths shape correctly + keyword fallback.
* AC16 — Fail-closed empty-everything → substrate_tier.
* AC17 — Classifier walks diff regardless of self-reported
  paths.
* AC18 — Diff/paths disagreement picks conservative side.
* AC19 — Unknown in-repo path → substrate_tier.
* AC20 — Malformed touches_paths → InvestigationResponseMalformed.
* AC21 — Sensitive-path lattice routes to architect gate.
* AC22 — Substrate-path lattice catches Codex round-1 gaps.
* AC25 — validate_investigation_response shape rejection.
"""
from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from kernos.kernel.fix_authorization import (
    CONFIG_DATA_PATH_PATTERNS,
    FixAuthorizationStore,
    InvestigationResponseMalformed,
    SCOPE_CONFIG_DATA,
    SCOPE_EXTERNAL_ONLY,
    SCOPE_SENSITIVE,
    SCOPE_SUBSTRATE_TIER,
    SENSITIVE_PATH_PATTERNS,
    SUBSTRATE_PATH_PATTERNS,
    classify_fix_scope,
    extract_paths_from_unified_diff,
    record_fix_authorization,
    validate_investigation_response,
)


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------


@pytest.fixture
async def fa_store(tmp_path: Path) -> FixAuthorizationStore:
    store = FixAuthorizationStore()
    await store.start(str(tmp_path))
    yield store
    await store.close()


# ---------------------------------------------------------------------
# AC2 — fix_authorization table CRUD + idempotency
# ---------------------------------------------------------------------


async def test_ac2_insert_succeeds(fa_store):
    await fa_store.insert(
        instance_id="i1",
        authorization_id="auth_1",
        request_id="req_1",
        requester_member_id="member_1",
        source_space_id="space_1",
        target_hint="the scraper",
        request_text="/fix the scraper",
    )
    row = await fa_store.get_by_request_id(
        instance_id="i1", request_id="req_1",
    )
    assert row["authorization_id"] == "auth_1"
    assert row["target_hint"] == "the scraper"
    assert row["trigger_surface"] == "slash:/fix"  # default


async def test_ac2_request_id_unique(fa_store):
    await fa_store.insert(
        instance_id="i1",
        authorization_id="auth_1",
        request_id="dup_req",
        requester_member_id="m1",
        source_space_id="s1",
        target_hint="",
        request_text="/fix",
    )
    with pytest.raises(aiosqlite.IntegrityError):
        await fa_store.insert(
            instance_id="i1",
            authorization_id="auth_2",  # different
            request_id="dup_req",       # same → unique violation
            requester_member_id="m1",
            source_space_id="s1",
            target_hint="",
            request_text="/fix",
        )


async def test_ac2_record_tool_idempotent(fa_store):
    first = await record_fix_authorization(
        store=fa_store,
        instance_id="i1",
        request_id="req_idem",
        requester_member_id="m1",
        source_space_id="s1",
        target_hint="",
        request_text="/fix",
    )
    assert first["newly_created"] is True
    second = await record_fix_authorization(
        store=fa_store,
        instance_id="i1",
        request_id="req_idem",
        requester_member_id="m1",
        source_space_id="s1",
        target_hint="",
        request_text="/fix",
    )
    assert second["newly_created"] is False
    assert second["authorization_id"] == first["authorization_id"]


async def test_ac2_per_instance_isolation(fa_store):
    """Same request_id under different instance_ids both succeed."""
    await record_fix_authorization(
        store=fa_store, instance_id="i1",
        request_id="req_x", requester_member_id="m1",
        source_space_id="s1", target_hint="",
        request_text="/fix",
    )
    await record_fix_authorization(
        store=fa_store, instance_id="i2",
        request_id="req_x", requester_member_id="m1",
        source_space_id="s1", target_hint="",
        request_text="/fix",
    )
    r1 = await fa_store.get_by_request_id(
        instance_id="i1", request_id="req_x",
    )
    r2 = await fa_store.get_by_request_id(
        instance_id="i2", request_id="req_x",
    )
    assert r1 is not None and r2 is not None
    assert r1["authorization_id"] != r2["authorization_id"]


async def test_ac2_trigger_surface_persisted(fa_store):
    """v1.1 fold: trigger_surface field carries through to DB."""
    await record_fix_authorization(
        store=fa_store, instance_id="i1",
        request_id="r1", requester_member_id="m1",
        source_space_id="s1", target_hint="thing",
        request_text="/fix",
        trigger_surface="slash:/fix:from_proposal",
    )
    row = await fa_store.get_by_request_id(
        instance_id="i1", request_id="r1",
    )
    assert row["trigger_surface"] == "slash:/fix:from_proposal"


# ---------------------------------------------------------------------
# Diff-path extraction
# ---------------------------------------------------------------------


def test_extract_paths_handles_git_headers():
    diff = """diff --git a/kernos/kernel/foo.py b/kernos/kernel/foo.py
index abc..def 100644
--- a/kernos/kernel/foo.py
+++ b/kernos/kernel/foo.py
@@ -1,3 +1,4 @@
 line one
+added
"""
    paths = extract_paths_from_unified_diff(diff)
    assert "kernos/kernel/foo.py" in paths


def test_extract_paths_handles_multiple_files():
    diff = """diff --git a/kernos/a.py b/kernos/a.py
+++ b/kernos/a.py
diff --git a/data/b.json b/data/b.json
+++ b/data/b.json
"""
    paths = extract_paths_from_unified_diff(diff)
    assert sorted(paths) == ["data/b.json", "kernos/a.py"]


def test_extract_paths_handles_no_git_header():
    diff = """--- a/some/path.py
+++ b/some/path.py
@@ -1 +1 @@
-old
+new
"""
    paths = extract_paths_from_unified_diff(diff)
    assert "some/path.py" in paths


def test_extract_paths_skips_dev_null():
    """File creation has --- /dev/null; deletion has +++ /dev/null."""
    diff = """diff --git a/new/file.py b/new/file.py
--- /dev/null
+++ b/new/file.py
"""
    paths = extract_paths_from_unified_diff(diff)
    assert "new/file.py" in paths
    assert "/dev/null" not in paths


def test_extract_paths_empty_on_none_or_empty():
    assert extract_paths_from_unified_diff(None) == []
    assert extract_paths_from_unified_diff("") == []


# ---------------------------------------------------------------------
# AC6 + AC16 — classifier routing
# ---------------------------------------------------------------------


def test_ac6_kernel_path_routes_substrate_tier():
    r = classify_fix_scope(
        touches_paths=["kernos/kernel/foo.py"],
    )
    assert r.scope == SCOPE_SUBSTRATE_TIER
    assert r.requires_architect_gate is True


def test_ac6_data_yaml_routes_config_data():
    r = classify_fix_scope(
        touches_paths=["data/instance_1/config.yaml"],
    )
    assert r.scope == SCOPE_CONFIG_DATA
    assert r.requires_architect_gate is False


def test_ac6_external_only_when_no_paths_with_action():
    r = classify_fix_scope(
        proposed_fix_summary="update the scraper selector",
        external_action="Update the CSS selector on example.com",
    )
    assert r.scope == SCOPE_EXTERNAL_ONLY
    assert r.requires_architect_gate is False
    assert r.gate_weight == "no_gate"


def test_ac6_mixed_paths_kernel_wins():
    r = classify_fix_scope(
        touches_paths=["data/foo.yaml", "kernos/x.py"],
    )
    assert r.scope == SCOPE_SUBSTRATE_TIER
    assert r.requires_architect_gate is True


def test_ac6_workflow_yaml_is_substrate():
    """specs/workflows/*.yaml are substrate, not config_data."""
    r = classify_fix_scope(
        touches_paths=["specs/workflows/self_improvement.workflow.yaml"],
    )
    assert r.scope == SCOPE_SUBSTRATE_TIER


def test_ac16_empty_everything_fails_closed():
    r = classify_fix_scope()
    assert r.scope == SCOPE_SUBSTRATE_TIER
    assert r.requires_architect_gate is True
    assert "fail-closed" in r.reasoning


# ---------------------------------------------------------------------
# AC17 — classifier walks diff regardless of self-reported paths
# ---------------------------------------------------------------------


def test_ac17_diff_overrides_empty_self_reported():
    """CC says touches_paths=[] but diff touches kernos/ →
    classifier extracts from diff → substrate_tier."""
    diff = """diff --git a/kernos/kernel/foo.py b/kernos/kernel/foo.py
+++ b/kernos/kernel/foo.py
"""
    r = classify_fix_scope(
        touches_paths=[],
        proposed_fix_diff=diff,
    )
    assert r.scope == SCOPE_SUBSTRATE_TIER
    assert r.requires_architect_gate is True
    assert "kernos/kernel/foo.py" in r.derived_paths


# ---------------------------------------------------------------------
# AC18 — disagreement picks conservative side
# ---------------------------------------------------------------------


def test_ac18_diff_paths_disagreement_picks_conservative():
    """CC says only data/, diff actually touches kernos/ →
    union → substrate hit → substrate_tier; disagreement
    flag set."""
    diff = """diff --git a/kernos/kernel/bar.py b/kernos/kernel/bar.py
+++ b/kernos/kernel/bar.py
"""
    r = classify_fix_scope(
        touches_paths=["data/foo.json"],
        proposed_fix_diff=diff,
    )
    assert r.scope == SCOPE_SUBSTRATE_TIER
    assert r.diff_path_disagreement is True
    assert "data/foo.json" in r.derived_paths
    assert "kernos/kernel/bar.py" in r.derived_paths


# ---------------------------------------------------------------------
# AC19 — unknown in-repo path → substrate_tier
# ---------------------------------------------------------------------


def test_ac19_unknown_in_repo_path_fails_closed():
    r = classify_fix_scope(
        touches_paths=["random/unknown/path.txt"],
    )
    assert r.scope == SCOPE_SUBSTRATE_TIER
    assert r.requires_architect_gate is True
    assert "fail-closed substrate_tier" in r.reasoning


# ---------------------------------------------------------------------
# AC20 — malformed touches_paths
# ---------------------------------------------------------------------


def test_ac20_validate_rejects_non_list_touches_paths():
    with pytest.raises(InvestigationResponseMalformed,
                       match="touches_paths must be a list"):
        validate_investigation_response(
            investigation_outcome="completed",
            failure_mode="x",
            proposed_fix_summary="x",
            proposed_fix_diff="diff x",
            touches_paths=None,
        )

    with pytest.raises(InvestigationResponseMalformed,
                       match="touches_paths must be a list"):
        validate_investigation_response(
            investigation_outcome="completed",
            failure_mode="x",
            proposed_fix_summary="x",
            proposed_fix_diff="diff x",
            touches_paths="single-string",  # not a list
        )


# ---------------------------------------------------------------------
# AC21 — sensitive paths
# ---------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    ".env",
    ".credentials/openai-codex.json",
    "data/discord_123/instance.db",
    "secrets/foo",
    "data/discord_999/kernos.db-wal",
])
def test_ac21_sensitive_paths_route_to_architect_gate(path):
    r = classify_fix_scope(touches_paths=[path])
    assert r.scope == SCOPE_SENSITIVE, (
        f"{path} should be SENSITIVE; got {r.scope}: {r.reasoning}"
    )
    assert r.requires_architect_gate is True
    assert r.sensitive_path_detected is True
    assert path in r.sensitive_paths


# ---------------------------------------------------------------------
# AC22 — substrate path lattice (Codex round-1 gaps closed)
# ---------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "specs/workflows/self_improvement.workflow.yaml",
    "scripts/manage-kernos-service.sh",
    "DECISIONS.md",
    "CLAUDE.md",
    "kernos/kernel/foo.py",
    "tests/test_foo.py",
    "docs/architecture/overview.md",
    "start.sh",
])
def test_ac22_substrate_path_lattice(path):
    r = classify_fix_scope(touches_paths=[path])
    assert r.scope == SCOPE_SUBSTRATE_TIER, (
        f"{path} should be SUBSTRATE_TIER; got {r.scope}: {r.reasoning}"
    )
    assert r.requires_architect_gate is True


# ---------------------------------------------------------------------
# AC25 — validate_investigation_response schema
# ---------------------------------------------------------------------


def test_ac25_completed_requires_failure_mode():
    with pytest.raises(InvestigationResponseMalformed,
                       match="failure_mode"):
        validate_investigation_response(
            investigation_outcome="completed",
            failure_mode="",  # empty
            proposed_fix_summary="x",
            proposed_fix_diff="diff",
            touches_paths=[],
        )


def test_ac25_completed_requires_proposed_fix_summary():
    with pytest.raises(InvestigationResponseMalformed,
                       match="proposed_fix_summary"):
        validate_investigation_response(
            investigation_outcome="completed",
            failure_mode="x",
            proposed_fix_summary="",
            proposed_fix_diff="diff",
            touches_paths=[],
        )


def test_ac25_completed_requires_diff_or_external_action():
    with pytest.raises(InvestigationResponseMalformed,
                       match="proposed_fix_diff.*external_action"):
        validate_investigation_response(
            investigation_outcome="completed",
            failure_mode="x",
            proposed_fix_summary="x",
            proposed_fix_diff="",
            external_action="",
            touches_paths=[],
        )


def test_ac25_completed_with_diff_passes():
    out = validate_investigation_response(
        investigation_outcome="completed",
        failure_mode="x",
        proposed_fix_summary="x",
        proposed_fix_diff="diff body",
        touches_paths=[],
    )
    assert out == {"valid": True}


def test_ac25_completed_with_external_action_passes():
    out = validate_investigation_response(
        investigation_outcome="completed",
        failure_mode="x",
        proposed_fix_summary="x",
        external_action="external description",
        touches_paths=[],
    )
    assert out == {"valid": True}


def test_ac25_invalid_outcome_rejected():
    with pytest.raises(InvestigationResponseMalformed,
                       match="investigation_outcome"):
        validate_investigation_response(
            investigation_outcome="random-string",
            touches_paths=[],
        )


def test_ac25_unable_to_investigate_passes_validation():
    """Validation passes; workflow's own logic aborts on this
    outcome separately."""
    out = validate_investigation_response(
        investigation_outcome="unable_to_investigate",
        touches_paths=[],
    )
    assert out == {"valid": True}


# v1.1 BRIDGE-RESPONSE-SCHEMA fold — summary-alone acceptance


def test_v11_completed_with_summary_alone_passes():
    """2026-05-27 19:14 live-test regression pin: the coding-
    session bridge response carries summary + metadata only, with
    no structured top-level fields (failure_mode, proposed_fix_diff,
    etc.). The loosened validator MUST accept summary-only when
    outcome=completed."""
    out = validate_investigation_response(
        investigation_outcome="completed",
        summary="I traced the failure to kernos/server.py:2279...",
        touches_paths=[],
    )
    assert out == {"valid": True}


def test_v11_completed_empty_summary_AND_empty_structured_rejects():
    """Empty summary AND empty failure_mode → reject (no signal)."""
    with pytest.raises(InvestigationResponseMalformed,
                       match="failure_mode"):
        validate_investigation_response(
            investigation_outcome="completed",
            summary="",
            failure_mode="",
            proposed_fix_summary="",
            proposed_fix_diff="",
            external_action="",
            touches_paths=[],
        )


def test_v11_partial_with_summary_alone_passes():
    out = validate_investigation_response(
        investigation_outcome="partial",
        summary="started but ran out of context",
        touches_paths=[],
    )
    assert out == {"valid": True}


def test_v11_summary_whitespace_only_treated_as_empty():
    """Whitespace-only summary doesn't count as non-empty."""
    with pytest.raises(InvestigationResponseMalformed):
        validate_investigation_response(
            investigation_outcome="completed",
            summary="   \n  \t  ",
            failure_mode="",
            proposed_fix_summary="",
            touches_paths=[],
        )


# ---------------------------------------------------------------------
# Lattice constant sanity pins
# ---------------------------------------------------------------------


def test_lattice_constants_exposed():
    assert ".env" in SENSITIVE_PATH_PATTERNS
    assert "kernos/**" in SUBSTRATE_PATH_PATTERNS
    assert "pyproject.toml" in SUBSTRATE_PATH_PATTERNS
    assert "data/**/*.json" in CONFIG_DATA_PATH_PATTERNS

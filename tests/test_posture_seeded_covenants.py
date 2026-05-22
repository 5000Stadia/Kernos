"""POSTURE-SEEDED-COVENANTS-V1 (2026-05-22) acceptance tests.

Updated 2026-05-22 post-implementation: self-update preference
removed from minimal + standard at owner request — opt-in
territory, not behavior-neutral default. Strict still carries
it for pre-POSTURE parity.

Covers spec ACs 1-11:
- AC1  minimal (default) seeds 4 rules
- AC2  KERNOS_POSTURE_PROFILE=standard seeds 6 rules
- AC3  KERNOS_POSTURE_PROFILE=strict seeds 9 rules with EXACT
       content + order parity to the pre-change implementation
- AC4  invalid env value falls back to STRICT + logs ERROR
- AC5  env normalization (whitespace + case)
- AC6  all seeded rules are source=default + active=True
- AC7  all seeded rules construct valid CovenantRule objects
- AC8  DEFAULT_COVENANTS_SEEDED INFO log fires per seed call
- AC9  existing instances unaffected (helper-level: repeated calls
       don't mutate prior call results; full provisioning-path
       integration deferred to manual verification)
- AC10 backwards-compat alias preserved
- AC11 no regressions on covenant_* tests (run separately)
"""
from __future__ import annotations

import logging
from typing import Any

import pytest


# ============================================================
# AC1 — minimal default seeds 4 rules
# (2026-05-22: self-update preference removed from minimal per
# owner request — opt-in territory, not behavior-neutral default.)
# ============================================================


class TestMinimalDefaultSeed:
    def test_unset_env_seeds_4_rules(self, monkeypatch):
        from kernos.kernel.state import default_covenant_rules
        monkeypatch.delenv("KERNOS_POSTURE_PROFILE", raising=False)
        rules = default_covenant_rules("test_inst", "2026-05-22T00:00:00")
        assert len(rules) == 4, (
            f"minimal profile must seed 4 rules; got {len(rules)}"
        )

    def test_minimal_explicit_seeds_4_rules(self, monkeypatch):
        from kernos.kernel.state import default_covenant_rules
        monkeypatch.setenv("KERNOS_POSTURE_PROFILE", "minimal")
        rules = default_covenant_rules("test_inst", "2026-05-22T00:00:00")
        assert len(rules) == 4

    def test_minimal_contains_load_bearing_invariants(self, monkeypatch):
        """Minimal must include: spirit, sharer-info, delete-files,
        escalation. (Self-update removed 2026-05-22.)"""
        from kernos.kernel.state import default_covenant_rules
        monkeypatch.delenv("KERNOS_POSTURE_PROFILE", raising=False)
        rules = default_covenant_rules("test_inst", "2026-05-22T00:00:00")
        types = [r.rule_type for r in rules]
        descs = [r.description for r in rules]
        assert "spirit" in types
        assert "escalation" in types
        assert any("delete" in d.lower() for d in descs)
        assert any("shared with you belongs" in d for d in descs)
        # Self-update is NOT in minimal (only strict, for parity).
        assert not any("substrate event" in d for d in descs)


# ============================================================
# AC2 — standard seeds 6 rules
# (2026-05-22: was 7 pre-self-update-removal.)
# ============================================================


class TestStandardProfile:
    def test_standard_seeds_6_rules(self, monkeypatch):
        from kernos.kernel.state import default_covenant_rules
        monkeypatch.setenv("KERNOS_POSTURE_PROFILE", "standard")
        rules = default_covenant_rules("test_inst", "2026-05-22T00:00:00")
        assert len(rules) == 6

    def test_standard_adds_spending_and_drafts(self, monkeypatch):
        from kernos.kernel.state import default_covenant_rules
        monkeypatch.setenv("KERNOS_POSTURE_PROFILE", "standard")
        rules = default_covenant_rules("test_inst", "2026-05-22T00:00:00")
        descs = [r.description for r in rules]
        assert any("spending money" in d for d in descs)
        assert any("Show drafts" in d for d in descs)
        # Self-update is NOT in standard either.
        assert not any("substrate event" in d for d in descs)


# ============================================================
# AC3 — strict EXACT content + order parity
# ============================================================


class TestStrictParity:
    def test_strict_seeds_9_rules(self, monkeypatch):
        from kernos.kernel.state import default_covenant_rules
        monkeypatch.setenv("KERNOS_POSTURE_PROFILE", "strict")
        rules = default_covenant_rules("test_inst", "2026-05-22T00:00:00")
        assert len(rules) == 9

    def test_strict_order_matches_original_hardcoded_sequence(
        self, monkeypatch,
    ):
        """Spec AC3: strict must preserve the original pre-change
        9-rule order byte-for-byte. Operators relying on rule
        ordering for introspection/display should see zero drift."""
        from kernos.kernel.state import default_covenant_rules
        monkeypatch.setenv("KERNOS_POSTURE_PROFILE", "strict")
        rules = default_covenant_rules("test_inst", "2026-05-22T00:00:00")
        # Original pre-change order:
        # spirit, must_not(3rd party), must_not(delete), must_not(sharer),
        # must(spending), must(drafts), preference(depth),
        # preference(self-update), escalation
        expected_types = [
            "spirit", "must_not", "must_not", "must_not",
            "must", "must", "preference", "preference", "escalation",
        ]
        actual_types = [r.rule_type for r in rules]
        assert actual_types == expected_types, (
            f"strict profile rule-type order must match pre-change "
            f"original. Expected {expected_types}, got {actual_types}"
        )
        # Spot-check key descriptions land in their original slots
        assert "third-party CONTACTS" in rules[1].description
        assert "delete the user's files" in rules[2].description
        assert "shared with you belongs" in rules[3].description
        assert "spending money" in rules[4].description
        assert "Match the depth" in rules[6].description
        assert "ambiguous" in rules[8].description


# ============================================================
# AC4 — invalid env falls back to STRICT + ERROR log
# ============================================================


class TestInvalidEnvFailsLoud:
    def test_bogus_env_falls_back_to_strict(self, monkeypatch, caplog):
        from kernos.kernel.state import default_covenant_rules
        monkeypatch.setenv("KERNOS_POSTURE_PROFILE", "bogus")
        with caplog.at_level(logging.ERROR, logger="kernos.kernel.state"):
            rules = default_covenant_rules(
                "test_inst", "2026-05-22T00:00:00",
            )
        # Fail-loud + over-seed: 9 rules, not 5
        assert len(rules) == 9, (
            "invalid env must fall back to strict (9 rules), not "
            "minimal (5) — silent under-seed is dangerous because "
            "existing-instances-not-touched policy doesn't auto-retry"
        )
        # ERROR log was emitted. POSTURE-CONFIGURATION-V1 reworded
        # the log to "posture profile '...' (source=env) unknown".
        errors = [
            r for r in caplog.records
            if r.levelno == logging.ERROR
            and "posture profile" in r.getMessage()
        ]
        assert errors, "must log ERROR on invalid env value"
        assert "bogus" in errors[0].getMessage()


# ============================================================
# AC5 — env normalization (whitespace + case)
# ============================================================


class TestEnvNormalization:
    @pytest.mark.parametrize("env_value,expected_count", [
        ("MINIMAL", 4),
        ("Standard", 6),
        ("STRICT", 9),
        ("  minimal  ", 4),
        ("  STANDARD  ", 6),
        ("  Strict  ", 9),
    ])
    def test_case_and_whitespace_normalized(
        self, monkeypatch, env_value, expected_count,
    ):
        from kernos.kernel.state import default_covenant_rules
        monkeypatch.setenv("KERNOS_POSTURE_PROFILE", env_value)
        rules = default_covenant_rules(
            "test_inst", "2026-05-22T00:00:00",
        )
        assert len(rules) == expected_count, (
            f"env value {env_value!r} should normalize to "
            f"{expected_count} rules"
        )


# ============================================================
# AC6 — seeded rules carry source=default + active=True
# ============================================================


class TestSeededRuleMetadata:
    def test_all_seeded_rules_source_default(self, monkeypatch):
        from kernos.kernel.state import default_covenant_rules
        monkeypatch.delenv("KERNOS_POSTURE_PROFILE", raising=False)
        rules = default_covenant_rules("test_inst", "2026-05-22T00:00:00")
        for r in rules:
            assert r.source == "default", (
                f"rule {r.description[:40]!r} has source={r.source!r}"
            )
            assert r.active is True


# ============================================================
# AC7 — all seeded rules construct valid CovenantRule objects
# ============================================================


class TestRuleConstruction:
    def test_seeded_rules_have_required_fields(self, monkeypatch):
        from kernos.kernel.state import default_covenant_rules
        monkeypatch.setenv("KERNOS_POSTURE_PROFILE", "strict")
        rules = default_covenant_rules("test_inst", "2026-05-22T00:00:00")
        for r in rules:
            assert r.id
            assert r.instance_id == "test_inst"
            assert r.capability == "general"
            assert r.rule_type in (
                "spirit", "must", "must_not", "preference", "escalation",
            )
            assert r.description
            assert r.created_at == "2026-05-22T00:00:00"
            assert r.enforcement_tier in ("silent", "notify", "confirm", "block")
            assert r.tier in ("pinned", "situational")


# ============================================================
# AC8 — INFO log fires per seed call
# ============================================================


class TestSeedingLogLine:
    def test_default_covenants_seeded_log_fires(self, monkeypatch, caplog):
        from kernos.kernel.state import default_covenant_rules
        monkeypatch.setenv("KERNOS_POSTURE_PROFILE", "standard")
        with caplog.at_level(logging.INFO, logger="kernos.kernel.state"):
            default_covenant_rules("test_inst", "2026-05-22T00:00:00")
        seeded = [
            r for r in caplog.records
            if "DEFAULT_COVENANTS_SEEDED" in r.getMessage()
        ]
        assert len(seeded) == 1
        msg = seeded[0].getMessage()
        assert "profile=standard" in msg
        assert "rule_count=6" in msg
        assert "instance=test_inst" in msg


# ============================================================
# AC9 — repeated calls don't mutate prior call results
# ============================================================


class TestRepeatedCallIsolation:
    def test_repeated_calls_return_independent_lists(self, monkeypatch):
        """Pin: the helper returns fresh CovenantRule lists per call.
        Mutating one call's result doesn't affect the next.
        Full integration-level "existing instance untouched" is
        deferred to manual verification per spec AC9 — the
        provisioning code path that calls this helper has its own
        first-boot-only guard."""
        from kernos.kernel.state import default_covenant_rules
        monkeypatch.setenv("KERNOS_POSTURE_PROFILE", "minimal")
        call_a = default_covenant_rules("inst_a", "2026-05-22T00:00:00")
        call_a_ids = [r.id for r in call_a]
        call_b = default_covenant_rules("inst_b", "2026-05-22T00:01:00")
        call_b_ids = [r.id for r in call_b]
        # Fresh objects per call (different IDs)
        assert set(call_a_ids).isdisjoint(set(call_b_ids))
        # Mutating call_a doesn't affect call_b's rule list
        call_a.clear()
        assert len(call_b) == 4


# ============================================================
# AC10 — backwards-compat alias preserved
# ============================================================


class TestBackwardsCompatAlias:
    def test_default_contract_rules_alias_works(self, monkeypatch):
        from kernos.kernel.state import (
            default_contract_rules,
            default_covenant_rules,
        )
        monkeypatch.setenv("KERNOS_POSTURE_PROFILE", "standard")
        via_alias = default_contract_rules("inst_a", "2026-05-22T00:00:00")
        # IDs differ per call but profile + count + content shape match
        assert len(via_alias) == 6
        # The alias literally IS the function
        assert default_contract_rules is default_covenant_rules

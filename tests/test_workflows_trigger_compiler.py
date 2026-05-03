"""Tests for the trigger compiler.

WORKFLOW-LOOP-PRIMITIVE C6. Pins the DSL parser shapes, the
top-level compile entry point, and the English-compiler
gating discipline.
"""
from __future__ import annotations

import pytest

from kernos.kernel.workflows.trigger_compiler import (
    CompilerError,
    EnglishCompilationUnavailable,
    compile_dsl,
    compile_predicate_source,
)


class TestDslLeafs:
    def test_eq_string(self):
        assert compile_dsl('event.payload.kind == "report"') == {
            "op": "eq", "path": "payload.kind", "value": "report",
        }

    def test_eq_member_id_uses_actor_eq(self):
        assert compile_dsl('event.member_id == "owner"') == {
            "op": "actor_eq", "value": "owner",
        }

    def test_eq_correlation_id_uses_correlation_eq(self):
        assert compile_dsl('event.correlation_id == "cor-1"') == {
            "op": "correlation_eq", "value": "cor-1",
        }

    def test_eq_event_type(self):
        assert compile_dsl('event.event_type == "cc.batch.report"') == {
            "op": "eq", "path": "event_type", "value": "cc.batch.report",
        }

    def test_eq_int(self):
        assert compile_dsl("event.payload.count == 42") == {
            "op": "eq", "path": "payload.count", "value": 42,
        }

    def test_eq_float(self):
        assert compile_dsl("event.payload.score == 0.5") == {
            "op": "eq", "path": "payload.score", "value": 0.5,
        }

    def test_eq_bool(self):
        assert compile_dsl("event.payload.flag == true") == {
            "op": "eq", "path": "payload.flag", "value": True,
        }

    def test_inequality_compiles_to_NOT_eq(self):
        out = compile_dsl('event.payload.kind != "report"')
        assert out["op"] == "NOT"
        assert out["operand"] == {"op": "eq", "path": "payload.kind", "value": "report"}

    def test_contains(self):
        assert compile_dsl('event.payload.message contains "deploy"') == {
            "op": "contains", "path": "payload.message", "value": "deploy",
        }

    def test_in_set_strings(self):
        out = compile_dsl(
            'event.payload.severity in ["high", "critical"]',
        )
        assert out == {
            "op": "in_set", "path": "payload.severity",
            "values": ["high", "critical"],
        }

    def test_in_set_mixed_scalars(self):
        out = compile_dsl("event.payload.code in [1, 2, 3]")
        assert out == {
            "op": "in_set", "path": "payload.code", "values": [1, 2, 3],
        }

    def test_exists(self):
        assert compile_dsl("event.payload.task_id exists") == {
            "op": "exists", "path": "payload.task_id",
        }

    def test_not_exists(self):
        assert compile_dsl("event.payload.task_id not exists") == {
            "op": "not_exists", "path": "payload.task_id",
        }


class TestDslComposites:
    def test_and(self):
        out = compile_dsl(
            'event.event_type == "cc.batch.report" '
            'AND event.member_id == "owner"',
        )
        assert out["op"] == "AND"
        assert len(out["operands"]) == 2

    def test_or(self):
        out = compile_dsl(
            'event.event_type == "a" OR event.event_type == "b"',
        )
        assert out["op"] == "OR"
        assert len(out["operands"]) == 2

    def test_not_prefix(self):
        out = compile_dsl('NOT event.payload.kind == "skip"')
        assert out["op"] == "NOT"

    def test_parenthesised(self):
        out = compile_dsl(
            '(event.event_type == "a" AND event.member_id == "x") '
            'OR event.event_type == "b"',
        )
        assert out["op"] == "OR"
        assert out["operands"][0]["op"] == "AND"
        assert out["operands"][1]["op"] == "eq"

    def test_nested(self):
        out = compile_dsl(
            'event.event_type == "x" AND '
            '(event.member_id == "a" OR event.member_id == "b")',
        )
        assert out["op"] == "AND"
        assert any(p["op"] == "OR" for p in out["operands"])


class TestDslErrors:
    def test_empty_source_rejected(self):
        with pytest.raises(CompilerError):
            compile_dsl("")

    def test_unknown_leaf_rejected(self):
        with pytest.raises(CompilerError):
            compile_dsl("just some plain text without a path")

    def test_compiled_ast_validates(self):
        # Sanity: every compile that returns has gone through
        # validate_predicate. A buggy compile would surface here.
        out = compile_dsl(
            'event.event_type == "x" AND event.payload.f exists',
        )
        from kernos.kernel.workflows.predicates import validate
        validate(out)  # no raise


class TestCompilePredicateSource:
    async def test_dict_passthrough(self):
        ast = {"op": "exists", "path": "event_id"}
        out = await compile_predicate_source(ast)
        assert out == ast

    async def test_dsl_string_compiles(self):
        out = await compile_predicate_source(
            'event.payload.kind == "report"',
        )
        assert out == {"op": "eq", "path": "payload.kind", "value": "report"}

    async def test_english_without_compiler_rejected(self):
        with pytest.raises(EnglishCompilationUnavailable):
            await compile_predicate_source(
                "when CC posts a batch report",
            )

    async def test_english_with_sync_compiler(self):
        def compile_eng(source):
            assert "batch report" in source
            return {"op": "eq", "path": "event_type", "value": "cc.batch.report"}
        out = await compile_predicate_source(
            "when CC posts a batch report",
            english_compiler=compile_eng,
        )
        assert out == {"op": "eq", "path": "event_type", "value": "cc.batch.report"}

    async def test_english_with_async_compiler(self):
        async def compile_eng(source):
            return {"op": "exists", "path": "event_id"}
        out = await compile_predicate_source(
            "anything happens at all",
            english_compiler=compile_eng,
        )
        assert out == {"op": "exists", "path": "event_id"}

    async def test_invalid_dict_rejected(self):
        from kernos.kernel.workflows.predicates import PredicateError
        with pytest.raises(PredicateError):
            await compile_predicate_source({"op": "summon_demon"})

    async def test_english_compiler_returns_invalid_ast_rejected(self):
        def bad_eng(source):
            return {"op": "summon_demon"}
        from kernos.kernel.workflows.predicates import PredicateError
        with pytest.raises(PredicateError):
            await compile_predicate_source(
                "english", english_compiler=bad_eng,
            )

"""v1 self-test bug #10: the model emitted a modified-step `arguments` as a
non-dict (e.g. a string), and dict("some.string") raised "dictionary update
sequence element #0 has length 1; 2 is required" — which escaped the
PlanValidationError-only except and killed the whole reasoning turn. Args are
now coerced defensively; construction errors degrade to DivergenceReasonerError.
"""
import pytest
from kernos.kernel.enactment.divergence_reasoner import (
    _parse_step_from_payload, DivergenceReasonerError,
)


def test_string_arguments_do_not_crash():
    # the exact crash shape: arguments emitted as a bare string
    step = _parse_step_from_payload(
        {"tool_id": "register_tool", "arguments": "flip_coin.tool.json", "expectation": {"prose": "register the tool"}}
    )
    assert step.arguments == {}          # degraded, not crashed
    assert step.tool_id == "register_tool"


def test_json_object_string_arguments_parsed():
    step = _parse_step_from_payload(
        {"tool_id": "write_file", "arguments": '{"path": "x.md"}', "expectation": {"prose": "write a file"}}
    )
    assert step.arguments == {"path": "x.md"}


def test_list_arguments_degrade_to_empty():
    step = _parse_step_from_payload({"tool_id": "t", "arguments": ["a", "b"], "expectation": {"prose": "do"}})
    assert step.arguments == {}


def test_real_dict_arguments_preserved():
    step = _parse_step_from_payload(
        {"tool_id": "t", "arguments": {"k": "v"}, "expectation": {"prose": "do"}}
    )
    assert step.arguments == {"k": "v"}

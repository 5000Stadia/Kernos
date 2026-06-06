"""v1 self-test findings: (A) the dotted `external_agent.consult` alias, and
(B) the enactment dispatcher's 30s blanket timeout strangling long tools.
"""
from unittest.mock import MagicMock

from kernos.kernel.tool_aliases import canonicalize_tool_name
from kernos.kernel.enactment.dispatcher import (
    StepDispatcher,
    DEFAULT_TOOL_TIMEOUT_MS,
    _LONG_RUNNING_TOOL_TIMEOUT_MS,
)


# --- Issue A: alias --------------------------------------------------------

def test_external_agent_consult_resolves():
    assert canonicalize_tool_name("external_agent.consult") == ("consult", True)


def test_general_dotted_suffix_self_heals():
    # any made-up namespace on a known canonical tool resolves
    assert canonicalize_tool_name("whatever_ns.consult") == ("consult", True)
    assert canonicalize_tool_name("x.improve_kernos") == ("improve_kernos", True)


def test_real_name_and_unknown_dotted_untouched():
    assert canonicalize_tool_name("consult") == ("consult", False)
    assert canonicalize_tool_name("foo.not_a_tool") == ("foo.not_a_tool", False)


# --- Issue B: long-tool timeout floor -------------------------------------

def _dispatcher():
    return StepDispatcher(executor=MagicMock(), descriptor_lookup=MagicMock())


def test_long_tools_get_a_real_floor_not_30s():
    d = _dispatcher()
    desc = MagicMock()
    desc.operation_for.return_value = None            # no explicit per-op timeout
    res = MagicMock(operation_name="")
    # consult gets its 600s floor, not the 30s blanket
    assert d._timeout_ms_for(desc, res, "consult") == _LONG_RUNNING_TOOL_TIMEOUT_MS["consult"]
    assert d._timeout_ms_for(desc, res, "consult") > DEFAULT_TOOL_TIMEOUT_MS
    # a normal tool still gets the default
    assert d._timeout_ms_for(desc, res, "read_file") == DEFAULT_TOOL_TIMEOUT_MS


def test_explicit_per_op_timeout_still_wins():
    d = _dispatcher()
    op = MagicMock(timeout_ms=5_000)
    desc = MagicMock()
    desc.operation_for.return_value = op
    res = MagicMock(operation_name="some_op")
    # an explicit classification timeout overrides even the long-tool floor
    assert d._timeout_ms_for(desc, res, "consult") == 5_000

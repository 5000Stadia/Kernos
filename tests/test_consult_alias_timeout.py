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


def test_only_curated_dotted_forms_resolve_not_arbitrary():
    # registry-safety (Codex review): a NON-curated dotted name is left alone,
    # so a legitimate dotted/MCP tool can't be misrouted by suffix-matching.
    assert canonicalize_tool_name("whatever_ns.consult") == ("whatever_ns.consult", False)
    assert canonicalize_tool_name("foo.not_a_tool") == ("foo.not_a_tool", False)


def test_real_name_untouched():
    assert canonicalize_tool_name("consult") == ("consult", False)


def test_file_tool_dotted_hallucinations_repair():
    # v1 self-test: agent reached for dotted file-tool names + context_space_read
    assert canonicalize_tool_name("files.write_file") == ("write_file", True)
    assert canonicalize_tool_name("files.read_file") == ("read_file", True)
    assert canonicalize_tool_name("context_space_read") == ("read_file", True)


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

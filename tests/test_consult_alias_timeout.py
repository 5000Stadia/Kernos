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


# --- registry-aware dotted-suffix repair (the durable fix) ---------------

_KNOWN = frozenset({"write_file", "manage_plan", "consult", "google_cal.create_event"})


def test_suffix_repair_fires_for_unknown_dotted_with_known_suffix():
    # novel hallucination, not curated: domain.verb where verb is a real tool
    assert canonicalize_tool_name("workspace.write_file", _KNOWN) == ("write_file", True)
    assert canonicalize_tool_name("anything.manage_plan", _KNOWN) == ("manage_plan", True)


def test_suffix_repair_never_rewrites_a_real_dotted_tool():
    # a legitimate dotted/MCP tool is IN known_tools → left untouched
    assert canonicalize_tool_name("google_cal.create_event", _KNOWN) == ("google_cal.create_event", False)


def test_suffix_repair_skipped_when_suffix_not_a_known_tool():
    assert canonicalize_tool_name("foo.bar_baz", _KNOWN) == ("foo.bar_baz", False)


def test_suffix_repair_inert_without_known_tools():
    # no known set supplied → pure exact-match only, no suffix guessing
    assert canonicalize_tool_name("workspace.write_file") == ("workspace.write_file", False)


def test_double_underscore_namespace_repairs():
    # SEMANTIC-ACTION-ENVELOPE area__tool presentation form resolves to flat
    assert canonicalize_tool_name("files__write_file", _KNOWN) == ("write_file", True)
    assert canonicalize_tool_name("planning__manage_plan", _KNOWN) == ("manage_plan", True)


def test_double_underscore_real_tool_untouched():
    # a registered __-named tool (e.g. MCP) is never split apart
    known = _KNOWN | {"mcp__svc__do_thing"}
    assert canonicalize_tool_name("mcp__svc__do_thing", known) == ("mcp__svc__do_thing", False)


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


def test_bare_generic_plan_names_not_aliased():
    # Codex review: bare create_plan/start_plan could shadow a real user/MCP
    # tool, so they are NOT aliased; the specific KERNOS names still are.
    assert canonicalize_tool_name("create_plan") == ("create_plan", False)
    assert canonicalize_tool_name("start_plan") == ("start_plan", False)
    assert canonicalize_tool_name("self_directed_plan") == ("manage_plan", True)


def test_known_tool_never_aliased():
    # a registered tool whose name happens to be a curated alias key is left
    # alone when known_tools is supplied (real tools never get rewritten).
    known = frozenset({"consult", "external_agent.consult"})
    assert canonicalize_tool_name("external_agent.consult", known) == ("external_agent.consult", False)

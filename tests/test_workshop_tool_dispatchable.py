"""v1 self-test bug #7: an agent-built workshop tool registered successfully but
could not be dispatched — the gate returned "unknown" ("not classified for safe
dispatch") because workshop tools live in the tool catalog (source="workspace"),
not the capability registry the gate scans. That hard-blocked the build→use loop.
Fix: a registered workshop tool defaults to its declared class, else soft_write.
Scoped to source=="workspace" — undeclared MCP/connector tools stay "unknown".
"""
from unittest.mock import MagicMock

from tests.test_dispatch_gate import _make_service


def _gate_with_catalog_entry(svc, *, source, gate_classification=""):
    gate = svc._get_gate()
    entry = MagicMock()
    entry.source = source
    entry.gate_classification = gate_classification
    cat = MagicMock()
    cat.get.return_value = entry
    gate._catalog = cat
    return gate


def test_workshop_tool_defaults_to_soft_write_not_unknown():
    svc = _make_service()
    _gate_with_catalog_entry(svc, source="workspace")
    # build→use must work: a registered workshop tool is dispatchable
    assert svc._classify_tool_effect("flip_coin", None) == "soft_write"


def test_workshop_tool_respects_declared_classification():
    svc = _make_service()
    _gate_with_catalog_entry(svc, source="workspace", gate_classification="read")
    assert svc._classify_tool_effect("my_reader", None) == "read"


def test_non_workspace_catalog_entry_stays_unknown():
    # an MCP/connector catalog entry is NOT defaulted — could be destructive
    svc = _make_service()
    _gate_with_catalog_entry(svc, source="mcp_capability")
    assert svc._classify_tool_effect("some_mcp_tool", None) == "unknown"

"""SEMANTIC-ACTION-ENVELOPE-V1 (option A): the tool-name skin lives only at the
provider boundary. Outbound, kernel tools are presented as area__tool; inbound
function calls are unskinned back to flat before they enter substrate. MCP/
workshop names pass through unchanged.
"""
from kernos.kernel.tool_namespace import build_skin_maps
from kernos.providers.codex_provider import OpenAICodexProvider as P


_TOOLS = [
    {"name": "write_file", "description": "w", "input_schema": {"type": "object"}},
    {"name": "manage_plan", "description": "p", "input_schema": {"type": "object"}},
    {"name": "brave_web_search", "description": "s", "input_schema": {"type": "object"}},
]


def test_outbound_tools_are_namespaced_kernel_only():
    skin, _ = build_skin_maps(_TOOLS)
    out = {t["name"] for t in P._translate_tools(_TOOLS, skin)}
    assert "files__write_file" in out
    assert "planning__manage_plan" in out
    assert "brave_web_search" in out          # MCP untouched


def test_inbound_function_call_unskinned_to_flat():
    _, unskin = build_skin_maps(_TOOLS)
    data = {"status": "completed", "output": [
        {"type": "function_call", "call_id": "c1",
         "name": "files__write_file", "arguments": '{"path": "x.md"}'},
    ]}
    resp = P._parse_response(data, unskin)
    tu = [b for b in resp.content if b.type == "tool_use"]
    assert tu[0].name == "write_file"          # flat before it hits substrate
    assert tu[0].input == {"path": "x.md"}


def test_multi_tool_use_parallel_subcalls_unskinned():
    _, unskin = build_skin_maps(_TOOLS)
    data = {"status": "completed", "output": [
        {"type": "function_call", "call_id": "c2", "name": "multi_tool_use.parallel",
         "arguments": '{"tool_uses": [{"recipient_name": "planning__manage_plan", "parameters": {}}]}'},
    ]}
    resp = P._parse_response(data, unskin)
    tu = [b for b in resp.content if b.type == "tool_use"]
    assert tu[0].name == "manage_plan"


def test_tool_use_history_reskinned_outbound():
    skin, _ = build_skin_maps(_TOOLS)
    messages = [{"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": "write_file", "input": {"path": "x"}},
    ]}]
    items = P._translate_input(messages, skin)
    fc = [i for i in items if i.get("type") == "function_call"]
    assert fc[0]["name"] == "files__write_file"   # history matches offered list


def test_no_skin_map_is_identity():
    # defensive: no maps → names unchanged (back-comp)
    out = {t["name"] for t in P._translate_tools(_TOOLS, None)}
    assert out == {"write_file", "manage_plan", "brave_web_search"}


def test_wire_name_collision_keeps_kernel_flat():
    # a workshop tool literally named like a kernel wire name must not collide
    tools = [
        {"name": "write_file"},                  # kernel → would skin to files__write_file
        {"name": "files__write_file"},           # real workshop tool with that exact name
    ]
    skin, unskin = build_skin_maps(tools)
    # kernel write_file stays flat to avoid a duplicate provider function name
    assert skin["write_file"] == "write_file"
    # the real files__write_file is left as-is and unskin never rewrites it
    assert "files__write_file" not in unskin or unskin.get("files__write_file") != "write_file"

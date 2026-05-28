"""Static guard for Tier 1 async file-I/O conversion sites."""

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

TARGET_FUNCTIONS = {
    "kernos/kernel/conversation_log.py": {
        "_load_meta",
        "_load_meta_async",
        "_save_meta",
        "_save_meta_async",
        "append",
        "read_recent",
        "read_current_log_text",
        "_seed_from_previous_locked",
        "read_log_text",
    },
    "kernos/kernel/runtime_trace.py": {
        "append_turn",
        "_rotate_async",
    },
    "kernos/messages/handler.py": {
        "_load_parent_briefing",
        "_load_workspace_tool_schema",
    },
}


def _function_nodes(path: Path) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _forbidden_file_calls(node: ast.AST) -> list[tuple[int, str]]:
    violations: list[tuple[int, str]] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name) and func.id == "open":
            violations.append((child.lineno, "open(...)"))
        elif isinstance(func, ast.Attribute) and func.attr in {"read_text", "write_text"}:
            violations.append((child.lineno, f"{func.attr}(...)"))
    return violations


def test_tier1_hot_path_functions_do_not_call_sync_file_io():
    failures: list[str] = []
    for rel_path, function_names in TARGET_FUNCTIONS.items():
        path = REPO_ROOT / rel_path
        functions = _function_nodes(path)
        missing = sorted(function_names - functions.keys())
        assert not missing, f"{rel_path}: missing expected functions {missing}"

        for function_name in sorted(function_names):
            for line, call in _forbidden_file_calls(functions[function_name]):
                failures.append(f"{rel_path}:{line} {function_name} calls {call}")

    assert not failures, "Tier 1 hot path sync file I/O remains:\n" + "\n".join(failures)

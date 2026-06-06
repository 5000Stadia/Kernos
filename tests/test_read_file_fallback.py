"""read_file repo-path routing: "read the thing" should just work for repo
docs. A repo-style path (docs/… specs/… kernos/…) is never a space file — the
space reader's validator rejects any "/" outright — so it routes straight to
read_source. Flat names still use the space reader. (v1 self-test bug #2.)
"""
from unittest.mock import AsyncMock, MagicMock

from kernos.kernel.reasoning import ReasoningService


def _svc(read_file_return="space reader was called"):
    svc = MagicMock(spec=ReasoningService)
    svc._KERNEL_TOOLS = ReasoningService._KERNEL_TOOLS
    svc.execute_tool = ReasoningService.execute_tool.__get__(svc)
    files = MagicMock()
    files.read_file = AsyncMock(return_value=read_file_return)
    svc._files = files
    return svc


async def test_repo_doc_routes_to_source():
    # a docs/ path returns the real repo doc, via read_source
    svc = _svc()
    req = MagicMock(instance_id="t1", active_space_id="space1")
    out = await svc.execute_tool("read_file", {"name": "docs/V1-SELF-TEST.md"}, req)
    assert out.startswith("# KERNOS v1 self-test")     # real repo doc content
    svc._files.read_file.assert_not_awaited()           # space reader bypassed


async def test_repo_path_via_path_arg_key_also_routes():
    # bug #2 + bug #1 together: model passes the path under `path`, with a slash
    svc = _svc()
    req = MagicMock(instance_id="t1", active_space_id="space1")
    out = await svc.execute_tool("read_file", {"path": "docs/V1-SELF-TEST.md"}, req)
    assert out.startswith("# KERNOS v1 self-test")


async def test_nonexistent_repo_doc_returns_source_error():
    # a docs/ path that doesn't exist returns the source reader's error,
    # not a space-file lookup (it's unambiguously a repo path)
    svc = _svc()
    req = MagicMock(instance_id="t1", active_space_id="space1")
    out = await svc.execute_tool("read_file", {"name": "docs/does-not-exist-xyz.md"}, req)
    assert out.startswith("Error")
    svc._files.read_file.assert_not_awaited()


async def test_flat_name_uses_space_reader():
    # a bare/space filename goes to the space reader, never the repo
    svc = _svc("actual space file contents")
    req = MagicMock(instance_id="t1", active_space_id="space1")
    out = await svc.execute_tool("read_file", {"name": "notes.txt"}, req)
    assert out == "actual space file contents"
    svc._files.read_file.assert_awaited_once()

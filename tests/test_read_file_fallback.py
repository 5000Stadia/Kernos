"""read_file → read_source fallback: "read the thing" should just work for repo
docs, without the user-facing read failing over which reader the agent picked.
Safe direction only (read_file → read_source); never the reverse.
"""
from unittest.mock import AsyncMock, MagicMock

from kernos.kernel.reasoning import ReasoningService


def _svc(read_file_return):
    svc = MagicMock(spec=ReasoningService)
    svc._KERNEL_TOOLS = ReasoningService._KERNEL_TOOLS
    svc.execute_tool = ReasoningService.execute_tool.__get__(svc)
    files = MagicMock()
    files.read_file = AsyncMock(return_value=read_file_return)
    svc._files = files
    return svc


async def test_repo_doc_falls_through_to_source():
    # space read misses on a docs/ path → transparently read via read_source
    svc = _svc("Error: File 'docs/V1-SELF-TEST.md' not found. Use list_files to see available files.")
    req = MagicMock(instance_id="t1", active_space_id="space1")
    out = await svc.execute_tool("read_file", {"name": "docs/V1-SELF-TEST.md"}, req)
    assert out.startswith("# KERNOS v1 self-test")          # real repo doc content


async def test_space_filename_miss_returns_original_error():
    # a bare/space filename that misses must NOT over-reach into the repo
    svc = _svc("Error: File 'notes.txt' not found.")
    req = MagicMock(instance_id="t1", active_space_id="space1")
    out = await svc.execute_tool("read_file", {"name": "notes.txt"}, req)
    assert out == "Error: File 'notes.txt' not found."


async def test_space_hit_returned_as_is_no_fallback():
    # space read succeeds → return it untouched even for a docs/-looking name
    svc = _svc("actual space file contents")
    req = MagicMock(instance_id="t1", active_space_id="space1")
    out = await svc.execute_tool("read_file", {"name": "docs/whatever.md"}, req)
    assert out == "actual space file contents"

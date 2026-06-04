from __future__ import annotations

import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
NOTE_REL = Path("docs/notes/loop-selftest.md")
NOTE_PATH = REPO_ROOT / NOTE_REL
IMPL_NOTES_PATH = REPO_ROOT / "impl_notes.md"
SPEC_PATH = REPO_ROOT / "spec.md"
EXPECTED_HEADING = "# Loop self-test note"
REQUIRED_DATE = "June 3, 2026"
REQUIRED_RUNTIME_NEUTRAL_CLAIM = (
    "the autonomous-improvement loop carried an operator-approved "
    "documentation-only change into the Kernos worktree without altering "
    "runtime behavior"
)
ALLOWED_LOOP_SELFTEST_PATHS = {
    "docs/notes/loop-selftest.md",
    "impl_notes.md",
    "spec.md",
    "tests/test_loop_selftest_note.py",
}


def test_loop_selftest_note_behavior_and_substrate_state():
    """Assert reader-visible behavior and durable docs state together."""
    assert NOTE_PATH.is_file()

    content = NOTE_PATH.read_text(encoding="utf-8")
    body_blocks = _body_blocks(content)

    # Behavioral signal: the note has the approved reader-visible claim.
    assert content.startswith(f"{EXPECTED_HEADING}\n\n")
    assert len(body_blocks) == 1
    assert REQUIRED_DATE in body_blocks[0]
    assert REQUIRED_RUNTIME_NEUTRAL_CLAIM in body_blocks[0]
    assert "documentation-only" in body_blocks[0]

    # Substrate state: the exact artifact and constrained shape are on disk.
    assert NOTE_PATH.relative_to(REPO_ROOT).as_posix() == NOTE_REL.as_posix()
    assert NOTE_REL.as_posix() in body_blocks[0]
    assert len(body_blocks[0].split()) <= 60


def test_loop_selftest_dirty_scope_and_marker_confinement():
    content = NOTE_PATH.read_text(encoding="utf-8")
    impl_notes = IMPL_NOTES_PATH.read_text(encoding="utf-8")

    # Behavioral signal: the note remains a documentation-only, runtime-neutral
    # artifact from the reader's perspective.
    assert REQUIRED_RUNTIME_NEUTRAL_CLAIM in content

    # Substrate state: this improvement's dirty worktree surface remains
    # confined to the approved documentation/review artifacts.
    dirty_paths = _dirty_worktree_paths()
    loop_selftest_is_dirty = bool(dirty_paths & ALLOWED_LOOP_SELFTEST_PATHS)
    if loop_selftest_is_dirty:
        assert dirty_paths <= ALLOWED_LOOP_SELFTEST_PATHS

    # Orchestration markers are confined to the required impl_notes terminator.
    marker_prefix = "STATUS" + ":"
    green_marker = marker_prefix + " GREEN"
    assert marker_prefix not in content
    if SPEC_PATH.exists():
        assert marker_prefix not in SPEC_PATH.read_text(encoding="utf-8")
    assert impl_notes.count(marker_prefix) == 1
    assert impl_notes.rstrip().endswith(green_marker)


def _body_blocks(content: str) -> list[str]:
    return [
        block
        for block in content.strip().split("\n\n")
        if not block.startswith("#")
    ]


def _dirty_worktree_paths() -> set[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    paths: set[str] = set()
    for line in result.stdout.splitlines():
        if not line:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.add(path)
    return paths

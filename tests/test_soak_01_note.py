from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
NOTE_REL = Path("docs/notes/soak-01.md")
NOTE_PATH = REPO_ROOT / NOTE_REL
EXPECTED_HEADING = "# Soak 01"
REQUIRED_DATE = "June 3, 2026"
REQUIRED_RUNTIME_NEUTRAL_CLAIM = (
    "the autonomous-improvement loop carried an operator-approved "
    "documentation-only change into the Kernos worktree while leaving "
    "runtime behavior unchanged"
)


def test_soak_01_note_behavior_and_substrate_state():
    """Assert reader-visible behavior and durable docs state together."""
    assert NOTE_PATH.is_file()

    content = NOTE_PATH.read_text(encoding="utf-8")
    body_blocks = _body_blocks(content)

    # Behavioral signal: the note says this was documentation-only and
    # runtime-neutral from the reader's perspective.
    assert content.startswith(f"{EXPECTED_HEADING}\n\n")
    assert len(body_blocks) == 1
    assert REQUIRED_DATE in body_blocks[0]
    assert REQUIRED_RUNTIME_NEUTRAL_CLAIM in body_blocks[0]
    assert "commit gate" in body_blocks[0]

    # Substrate state: the durable artifact lives at the approved path with a
    # constrained note shape and no orchestration marker embedded in it.
    assert NOTE_PATH.relative_to(REPO_ROOT).as_posix() == NOTE_REL.as_posix()
    assert NOTE_REL.as_posix() in body_blocks[0]
    assert len(body_blocks[0].split()) <= 60
    assert "STATUS" + ":" not in content


def _body_blocks(content: str) -> list[str]:
    return [
        block
        for block in content.strip().split("\n\n")
        if not block.startswith("#")
    ]

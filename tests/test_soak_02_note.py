from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
NOTE_REL = Path("docs/notes/soak-02.md")
NOTE_PATH = REPO_ROOT / NOTE_REL
EXPECTED_TEXT = (
    "This note was produced by Kernos's autonomous improvement loop on 2026-06-04.\n"
    "It exists to test the proportionality fix.\n"
    "The change is intentionally minimal.\n"
)


def test_soak_02_note_behavior_and_substrate_state():
    """Assert reader-visible behavior and durable docs state together."""
    assert NOTE_PATH.is_file()

    content = NOTE_PATH.read_text(encoding="utf-8")
    sentences = [line for line in content.splitlines() if line]

    # Behavioral signal: the note says what this loop-produced artifact is for.
    assert "Kernos's autonomous improvement loop" in sentences[0]
    assert "proportionality fix" in sentences[1]
    assert "intentionally minimal" in sentences[2]

    # Substrate state: the approved file exists at the exact path with only the
    # approved sentences and no orchestration marker embedded in it.
    assert NOTE_PATH.relative_to(REPO_ROOT).as_posix() == NOTE_REL.as_posix()
    assert content == EXPECTED_TEXT
    assert len(sentences) == 3
    assert "STATUS" + ":" not in content

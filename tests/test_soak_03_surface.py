from __future__ import annotations

from pathlib import Path

from kernos.kernel.agents.providers import ProviderRegistry
from kernos.utils import ensure_terminal_newline


REPO_ROOT = Path(__file__).resolve().parent.parent
NOTE_REL = Path("docs/notes/soak-03.md")
NOTE_PATH = REPO_ROOT / NOTE_REL
UTILS_PATH = REPO_ROOT / "kernos/utils.py"
PROVIDERS_PATH = REPO_ROOT / "kernos/kernel/agents/providers.py"
EXPECTED_NOTE_TEXT = (
    "This note was produced by Kernos's autonomous improvement loop on 2026-06-04.\n"
    "It exists to test one tiny helper, one docstring, and one note in the same substrate pass.\n"
    "The change is intentionally narrow.\n"
)


def test_soak_03_behavior_and_substrate_state():
    """Assert runtime behavior and durable substrate state together."""
    registry = ProviderRegistry()

    def factory(_provider_config_ref: str):
        raise AssertionError("factory should not be called by get()")

    # Behavioral signal: the new helper normalizes text artifacts, and the
    # documented registry lookup keeps known and absent provider semantics.
    assert ensure_terminal_newline("alpha") == "alpha\n"
    assert ensure_terminal_newline("alpha\n\n") == "alpha\n"
    registry.register("memory", factory)
    assert registry.get("memory") is factory
    assert registry.get("missing") is None

    # Substrate state: the approved helper, docstring-only registry change,
    # and note artifact are present at their exact durable paths.
    note_text = NOTE_PATH.read_text(encoding="utf-8")
    utils_source = UTILS_PATH.read_text(encoding="utf-8")
    providers_source = PROVIDERS_PATH.read_text(encoding="utf-8")

    assert NOTE_PATH.relative_to(REPO_ROOT).as_posix() == NOTE_REL.as_posix()
    assert note_text == EXPECTED_NOTE_TEXT
    assert "def ensure_terminal_newline(text: str) -> str:" in utils_source
    assert "Return text with exactly one trailing newline." in utils_source
    assert "def get(self, provider_key: str) -> AgentInboxFactory | None:" in providers_source
    assert "Return the factory bound to ``provider_key``, if any." in providers_source
    assert "STATUS" + ":" not in note_text

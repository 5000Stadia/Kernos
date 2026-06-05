"""`/selfreview` voice-merge: the real run's findings presented through
KERNOS's own interpretation layer (best of both worlds), not a dry template.

Substrate-assertion pattern: assert the behavioral signal (voiced text out)
AND that the actual run's concrete details reached the voice model call.
"""
import types

from kernos.messages.handler import MessageHandler


class _StubReasoning:
    def __init__(self, reply="Healthy slice — nothing urgent here."):
        self.reply = reply
        self.calls = []

    async def complete_simple(self, system_prompt, user_content,
                              max_tokens=1024, **kw):
        self.calls.append({
            "system": system_prompt, "user": user_content,
            "max_tokens": max_tokens,
        })
        return self.reply


async def test_voice_render_hands_real_findings_to_the_voice_layer():
    stub = types.SimpleNamespace(reasoning=_StubReasoning())
    report = {
        "slice": "dispatch-gate",
        "overall_health": "minor_concerns",
        "corrective_findings": ["edge case X unguarded", "comment drift in Y"],
        "evolution_idea": "extract helper Z",
        "constitutional": False,
    }

    out = await MessageHandler._render_selfreview_voice(stub, report)

    assert out and out.strip()                       # behavioral: voiced text
    u = stub.reasoning.calls[0]["user"]
    # the ACTUAL run's concrete details were merged into the voice prompt
    assert "dispatch-gate" in u
    assert "minor_concerns" in u
    assert "edge case X unguarded" in u
    assert "comment drift in Y" in u
    assert "extract helper Z" in u
    # rendered in-voice, not as a mechanical template
    assert "own voice" in stub.reasoning.calls[0]["system"].lower()


async def test_voice_render_constitutional_and_healthy_is_honest():
    stub = types.SimpleNamespace(reasoning=_StubReasoning())
    report = {
        "slice": "self-maintenance-methodology",
        "overall_health": "healthy",
        "corrective_findings": [],
        "evolution_idea": None,
        "constitutional": True,
    }

    out = await MessageHandler._render_selfreview_voice(stub, report)

    assert out and out.strip()
    u = stub.reasoning.calls[0]["user"]
    assert "constitutional" in u and "human-gated" in u   # honesty about gating
    assert "corrective findings: none" in u               # no manufactured concern
    assert "nothing worth evolving" in u


async def test_constitutional_guard_forced_when_voice_drops_it():
    # Codex must-fix: the human-gated invariant must survive into the output
    # even if the voice layer omits it.
    stub = types.SimpleNamespace(
        reasoning=_StubReasoning(reply="Looks fine, nothing to do here."))
    report = {
        "slice": "self-healing",
        "overall_health": "healthy",
        "corrective_findings": [],
        "evolution_idea": None,
        "constitutional": True,
    }

    out = await MessageHandler._render_selfreview_voice(stub, report)

    assert "human-gated" in out.lower()        # guard appended the invariant
    assert "never self-applied" in out.lower()


async def test_constitutional_guard_not_double_appended():
    stub = types.SimpleNamespace(
        reasoning=_StubReasoning(
            reply="This is constitutional, so any change is human-gated."))
    report = {
        "slice": "governing-intention",
        "overall_health": "healthy",
        "corrective_findings": [],
        "evolution_idea": None,
        "constitutional": True,
    }

    out = await MessageHandler._render_selfreview_voice(stub, report)

    assert out.lower().count("human-gated") == 1   # model's mention kept, no dup


async def test_non_constitutional_gets_no_guard_suffix():
    stub = types.SimpleNamespace(
        reasoning=_StubReasoning(reply="Healthy, nothing urgent."))
    report = {
        "slice": "dispatch-gate",
        "overall_health": "healthy",
        "corrective_findings": [],
        "evolution_idea": None,
        "constitutional": False,
    }

    out = await MessageHandler._render_selfreview_voice(stub, report)

    assert "human-gated" not in out.lower()        # no spurious gating note

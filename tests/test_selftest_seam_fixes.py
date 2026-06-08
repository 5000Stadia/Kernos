"""v1 self-test (2026-06-08): each PARTIAL/ERROR traced to a substrate seam.
This file pins the obvious-fix seams:

- ② Memory recall: note_this writes a KnowledgeEntry with no embedding, and
  retrieval was embedding-only (`if entry_embedding is None: continue`), so a
  freshly-noted fact returned knowledge=0. Lexical-overlap fallback rescues it.
- ④ Build-a-tool: the presence renderer's tool-loop cap was 5 — too low for
  execute_code + execute_code + register_tool. Raised to 12, env-overridable.
- ⑤ Proactive awareness: pending whispers had no read-side; inspect_state's
  truth view now surfaces them.
"""
import asyncio
import types

from kernos.kernel.retrieval import (
    lexical_overlap_score,
    LEXICAL_FALLBACK_THRESHOLD,
)


# --- ② lexical fallback ----------------------------------------------------

def test_lexical_overlap_recalls_keyed_fact_from_verbose_query():
    # the actual verbose recall query from the live log
    q = "favorite test color cerulean V1 self-test tests 1 2 3 4 5 results"
    score = lexical_overlap_score(q, "Kabe's favorite test color is cerulean.",
                                  "favorite_test_color")
    assert score >= LEXICAL_FALLBACK_THRESHOLD
    assert score == 1.0  # key tokens all present in the query


def test_lexical_overlap_natural_query_hits():
    score = lexical_overlap_score("what is my favorite test color",
                                  "Kabe's favorite test color is cerulean.",
                                  "favorite_test_color")
    assert score >= LEXICAL_FALLBACK_THRESHOLD


def test_lexical_overlap_unrelated_query_misses():
    score = lexical_overlap_score("what is my dog's name",
                                  "Kabe's favorite test color is cerulean.",
                                  "favorite_test_color")
    assert score < LEXICAL_FALLBACK_THRESHOLD


def test_lexical_overlap_empty_query_is_zero():
    assert lexical_overlap_score("", "anything", "k") == 0.0


def test_lexical_overlap_no_substring_false_positives():
    # Codex review: token equality, not substring — "dog" must not match
    # "dogma", "son" must not match "reasoning", "art" not "cartography".
    assert lexical_overlap_score("dog", "the dogma of the church", "") == 0.0
    assert lexical_overlap_score("son", "a chain of reasoning", "") == 0.0
    assert lexical_overlap_score("art", "a study of cartography", "") == 0.0
    # but a real whole-token match still scores
    assert lexical_overlap_score("dog", "my dog is named Sasha", "") == 1.0


# --- ④ presence renderer cap ----------------------------------------------

def test_presence_max_tool_iterations_default_raised():
    from kernos.kernel.enactment import presence_renderer as pr
    assert pr.DEFAULT_PRESENCE_MAX_TOOL_ITERATIONS >= 12
    # constructor + builder both adopt the raised default
    inst = pr.PresenceRenderer(chain_caller=lambda *a, **k: None)
    assert inst._max_tool_iterations >= 12


# --- ⑤ inspect_state surfaces pending whispers ----------------------------

def test_truth_view_surfaces_pending_whispers():
    from kernos.kernel.introspection import build_user_truth_view

    class _W:
        delivery_class = "ambient"
        insight_text = "Capability gaps found — a couple of tools would help."

    class _FakeState:
        async def query_preferences(self, *a, **k): return []
        async def query_triggers(self, *a, **k): return []
        async def query_covenants(self, *a, **k): return []
        async def query_knowledge(self, *a, **k): return []
        async def list_context_spaces(self, *a, **k): return []
        async def get_pending_whispers(self, instance_id): return [_W()]

    out = asyncio.run(build_user_truth_view("inst", _FakeState(), None, None))
    assert "Pending Whispers" in out
    assert "Capability gaps found" in out

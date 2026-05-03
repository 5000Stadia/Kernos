"""Prompt templates for the Gardener cohort (Pillar 6).

Kept separate from ``kernos/cohorts/gardener.py`` so the prompt copy
can be iterated without churning the cohort's judgment-routing logic.
Same discipline as ``messenger_prompt.py``.

Each builder returns ``(system_prompt, user_content)``.
"""
from __future__ import annotations


_ROLE = (
    "You are the Gardener, a bounded canvas-shape judgment cohort.\n"
    "Your authority: canvas shape only. You pick initial patterns for new\n"
    "canvases and propose reshapes as content accumulates. You are NOT a\n"
    "general-purpose agent. You do not generate content for pages, you\n"
    "do not delete content, and you do not make decisions outside of\n"
    "canvas shape.\n\n"
    "Confidence discipline: emit only HIGH confidence when your action\n"
    "is clearly warranted by the pattern-declared heuristics or Pattern\n"
    "00 cross-pattern rules. Marginal cases emit LOW or MEDIUM — those\n"
    "are logged for pattern tuning but do not wake members.\n\n"
    "Output strictly the JSON schema provided. Stay within the declared\n"
    "action vocabulary; invent no new actions."
)


def build_initial_shape_prompt(ctx) -> tuple[str, str]:
    """Pillar 3 — pick a pattern for a new canvas from the member's intent."""
    from kernos.cohorts.gardener import InitialShapeContext  # avoid circular

    assert isinstance(ctx, InitialShapeContext)
    patterns_block = "\n\n".join(
        f"PATTERN: {p.get('name','(unnamed)')}\n{p.get('summary','')}"
        for p in ctx.available_patterns
    )
    user_content = (
        f"NEW CANVAS\n"
        f"  name: {ctx.canvas_name}\n"
        f"  scope: {ctx.scope}\n"
        f"  creator: {ctx.creator_member_id}\n"
        f"  intent: {ctx.intent or '(no intent given)'}\n\n"
        f"AVAILABLE PATTERNS\n{patterns_block}\n\n"
        f"Pick the single best-matching pattern and emit "
        f"action=\"pick_pattern\" with pattern=<name> and confidence reflecting "
        f"how clean the dial-triple match is. If no pattern cleanly fits, "
        f"emit action=\"none\" and the caller will fall back to a minimal "
        f"canvas flagged as unmatched."
    )
    return _ROLE, user_content


def build_evolution_prompt(ctx) -> tuple[str, str]:
    """Pillar 4 — run evolution heuristics on one canvas event."""
    from kernos.cohorts.gardener import EvolutionContext

    assert isinstance(ctx, EvolutionContext)
    pages_block = "\n".join(
        f"  - {p.get('path','')}  ({p.get('type','note')}/{p.get('state','')})"
        for p in (ctx.canvas_pages_index or [])[:40]
    )
    user_content = (
        f"EVENT: {ctx.event_type}\n"
        f"  canvas: {ctx.canvas_id}\n"
        f"  pattern: {ctx.canvas_pattern}\n"
        f"  page: {ctx.page_path}\n\n"
        f"PAGE SUMMARY\n{ctx.page_summary}\n\n"
        f"CANVAS PAGES\n{pages_block}\n\n"
        f"PATTERN 00 CROSS-PATTERN HEURISTICS\n{ctx.cross_pattern_heuristics}\n\n"
    )
    if ctx.pattern_heuristics:
        user_content += (
            f"PATTERN-SPECIFIC HEURISTICS\n{ctx.pattern_heuristics}\n\n"
        )
    user_content += (
        "Emit a single action matching the applicable heuristic. If no "
        "heuristic applies cleanly, emit action=\"none\" with confidence=\"low\"."
    )
    return _ROLE, user_content


def build_preference_extraction_prompt(ctx) -> tuple[str, str]:
    """CANVAS-GARDENER-PREFERENCE-CAPTURE — extract a canvas-behavior preference
    from a member utterance.

    The prompt is strictly scoped to canvas-behavior preferences. The
    subject-matter validation is both prompt-side (explicit instructions
    + known effect kinds) and post-parse (force matched=false on any
    unknown effect_kind — see ``_parse_preference_extraction``). the design review
    revisions #1 + #2 are guarded at both layers.
    """
    from kernos.cohorts.gardener import PreferenceExtractionContext

    assert isinstance(ctx, PreferenceExtractionContext)
    intent_hooks_block = (
        "\n".join(f"  - {name}" for name in ctx.known_intent_hook_names)
        if ctx.known_intent_hook_names else "  (no intent-hook vocabulary known)"
    )
    current_prefs_block = (
        "\n".join(f"  - {k}: {v!r}" for k, v in (ctx.current_preferences or {}).items())
        if ctx.current_preferences else "  (no confirmed preferences yet)"
    )
    declined_block = (
        ", ".join(ctx.declined_preference_names)
        if ctx.declined_preference_names else "(none)"
    )

    system_prompt = (
        "You are the Gardener's preference-extraction consultant.\n"
        "Your job: decide whether a member utterance expresses a canvas-\n"
        "behavior preference, and if so, name it using the pattern's\n"
        "declared intent-hook vocabulary.\n\n"
        "A preference is about HOW THE GARDENER BEHAVES on a specific\n"
        "canvas. Examples of in-scope preferences:\n"
        "  - Suppression: \"don't ping me on every RSVP\" (suppress a dispatch)\n"
        "  - Thresholds: \"staleness for this one is 180 days\" (override a count)\n\n"
        "OUT OF SCOPE — reject these with matched=false:\n"
        "  - Agent behavior rules across all contexts (\"always explain\")\n"
        "    — those are covenants, not preferences\n"
        "  - Project management / member-authored content decisions\n"
        "  - Cross-canvas rules\n"
        "  - Anything not tied to a Gardener dispatch action\n\n"
        "REQUIRED output fields:\n"
        "  - matched (bool)\n"
        "  - preference_name (string; lowercase-hyphenated)\n"
        "  - preference_value (any; the configured value)\n"
        "  - evidence (short excerpt from the utterance showing the signal)\n"
        "  - confidence: high | medium | low\n"
        "    - high: unambiguous match to a declared intent-hook with a\n"
        "      clear value\n"
        "    - medium: probable match but novel or value-ambiguous\n"
        "    - low: weak signal; log-only\n"
        "  - supersedes: existing preference name being replaced, or null\n"
        "  - effect_kind: suppression | threshold | other\n"
        "    - suppression: turns off a heuristic class\n"
        "    - threshold: overrides a declared count/duration threshold\n"
        "    - other: any other effect (scope-modifier, routing-override,\n"
        "      authority-delegation). If you pick 'other', the system will\n"
        "      silently no-op — use it ONLY when the preference is valid\n"
        "      but its effect isn't yet wired.\n\n"
        "If the utterance does not map cleanly to canvas behavior OR\n"
        "the effect isn't suppression/threshold, return matched=false."
    )
    user_content = (
        f"UTTERANCE\n{ctx.utterance}\n\n"
        f"CANVAS PATTERN: {ctx.canvas_pattern}\n\n"
        f"DECLARED INTENT-HOOK VOCABULARY (prefer these names when matched)\n"
        f"{intent_hooks_block}\n\n"
        f"CURRENT CONFIRMED PREFERENCES (flag supersessions)\n"
        f"{current_prefs_block}\n\n"
        f"PREVIOUSLY DECLINED: {declined_block}\n\n"
        "Produce the structured output. If the utterance is ambiguous or "
        "the subject isn't canvas behavior, return matched=false with "
        "confidence=low."
    )
    return system_prompt, user_content


def build_section_prompt(ctx) -> tuple[str, str]:
    """Pillar 4 sub-judgment — section management on a single page."""
    from kernos.cohorts.gardener import SectionContext

    assert isinstance(ctx, SectionContext)
    user_content = (
        f"SECTION MANAGEMENT\n"
        f"  canvas: {ctx.canvas_id}\n"
        f"  page: {ctx.page_path}\n"
        f"  section: {ctx.section_slug} (\"{ctx.section_heading}\")\n"
        f"  current summary: {ctx.current_marker_summary or '(none)'}\n"
        f"  current tokens: {ctx.current_marker_tokens}\n\n"
        f"SECTION BODY\n{ctx.section_body[:3000]}\n\n"
        "Decide if this section needs a regenerated summary "
        "(regenerate_summary, emit payload.new_summary), a split "
        "proposal (propose_split), or no action (action=\"none\"). "
        "Stay within the Pattern 00 threshold bounds."
    )
    return _ROLE, user_content

"""Compaction Service — two-layer (Ledger + Living State) context compaction.

Per-space compaction documents accumulate history through append-only Ledger
entries and a rewritten Living State, eliminating summary drift. Replaces
simple truncation with structured historical preservation.
"""
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kernos.kernel.event_types import EventType
from kernos.kernel.events import EventStream, emit_event
from kernos.kernel.reasoning import ReasoningService
from kernos.kernel.spaces import ContextSpace
from kernos.kernel.state import StateStore
from kernos.kernel.tokens import TokenAdapter
from kernos.utils import utc_now

logger = logging.getLogger(__name__)




# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL_MAX_TOKENS = 200_000  # Claude Sonnet context window

COMPACTION_MODEL_MAX_TOKENS = 200_000
COMPACTION_OUTPUT_RESERVE_FRACTION = 0.20
COMPACTION_MODEL_USABLE_TOKENS = int(
    COMPACTION_MODEL_MAX_TOKENS * (1 - COMPACTION_OUTPUT_RESERVE_FRACTION)
)  # 160,000

COMPACTION_INSTRUCTION_TOKENS = 2000

DEFAULT_DAILY_HEADROOM = 8000


# ---------------------------------------------------------------------------
# CompactionState
# ---------------------------------------------------------------------------


@dataclass
class CompactionState:
    """Per-space compaction state, persisted as JSON."""

    space_id: str
    history_tokens: int = 0
    compaction_number: int = 0
    global_compaction_number: int = 0
    archive_count: int = 0
    message_ceiling: int = 0
    document_budget: int = 0
    conversation_headroom: int = 0
    cumulative_new_tokens: int = 0
    last_compaction_at: str = ""
    index_tokens: int = 0
    compaction_threshold: int = int(os.getenv("KERNOS_COMPACTION_THRESHOLD", "8000"))
    _context_def_tokens: int = 0
    _system_overhead: int = 0
    consecutive_failures: int = 0
    last_compaction_failure_at: str = ""
    last_seed_depth: int = 10            # Adaptive seed depth from last compaction


# ---------------------------------------------------------------------------
# Budget computation
# ---------------------------------------------------------------------------


def compute_document_budget(
    model_max_tokens: int,
    system_overhead_tokens: int,
    index_tokens: int,
    conversation_headroom: int,
) -> int:
    """Derive document budget from what's left after non-negotiable space."""
    return model_max_tokens - system_overhead_tokens - index_tokens - conversation_headroom


def domain_assessment_evidence(doc: str, max_chars: int = 3000) -> str:
    """Most-recent slice of a compaction document, for domain assessment.

    The document is ordered ledger-oldest-first with the Living State
    sections at the tail, so a head slice reads the *stalest* content —
    exactly the wrong evidence for spotting an emerging domain (live
    finding 2026-06-10: ``doc[:3000]`` covered days-old ledger entries
    and cut off the entire Current Situation). Evidence starts at the
    most recent ledger entry; when even that exceeds the budget, the
    tail wins — the Living State outranks older ledger lines.
    """
    import re as _re
    matches = list(_re.finditer(r"(?m)^## Compaction #\d+", doc))
    sliced = doc[matches[-1].start():] if matches else doc
    return sliced[-max_chars:] if len(sliced) > max_chars else sliced


# ---------------------------------------------------------------------------
# Headroom estimation
# ---------------------------------------------------------------------------

HEADROOM_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "estimated_tokens_per_exchange": {"type": "integer"},
        "minimum_recent_exchanges": {"type": "integer"},
        "conversation_headroom": {"type": "integer"},
    },
    "required": [
        "reasoning",
        "estimated_tokens_per_exchange",
        "minimum_recent_exchanges",
        "conversation_headroom",
    ],
    "additionalProperties": False,
}


async def estimate_headroom(
    reasoning: ReasoningService, space: ContextSpace
) -> int:
    """Estimate conversation headroom for a new context space."""
    result = await reasoning.complete_simple(
        system_prompt=(
            "Estimate how much token space this context space needs for "
            "recent conversation history to maintain coherent, useful interaction. "
            "Consider: how verbose are typical exchanges in this domain? "
            "How many recent turns does meaningful conversation require? "
            "A creative writing project or legal review with long exchanges needs more "
            "headroom than a scheduling space with short structured messages. "
            "Return estimated_tokens_per_exchange (average tokens per user+assistant pair), "
            "minimum_recent_exchanges (how many recent exchanges the agent needs to see), "
            "and conversation_headroom (their product, rounded up to nearest 1000)."
        ),
        user_content=(
            f"Space name: {space.name}\n"
            f"Space type: {space.space_type}\n"
            f"Space description: {space.description}\n"
        ),
        output_schema=HEADROOM_SCHEMA,
        max_tokens=256,
        prefer_cheap=True,
    )

    parsed = json.loads(result)
    headroom = parsed.get("conversation_headroom", DEFAULT_DAILY_HEADROOM)

    # Clamp to reasonable range
    return max(4000, min(headroom, 40000))


# ---------------------------------------------------------------------------
# Compaction prompt
# ---------------------------------------------------------------------------

COMPACTION_SYSTEM_PROMPT = """You are the context historian for this context space. Your sole function is to maintain a living historical record that preserves everything a future instance of this conversation would need to continue seamlessly — as if nothing were ever lost.

---

#### Inputs

You receive the following inputs in this order:

1. These instructions.
2. Context Space Definition — the domain, purpose, and character of this context space. This is your editorial lens. It determines what matters, what to preserve in detail, what to condense, and what to discard.
3. Previous Compaction History — the existing historical record from prior compaction cycles (empty on first run). This document contains two layers: a Ledger followed by a Living State. Both are described below.
4. Source Log Identifier — the log file this compaction is derived from (e.g., "log_003"). The full conversation text is permanently archived in this file and can be retrieved later via remember_details(). You do not need to preserve the conversation verbatim — the source log does that.
5. New Message Exchanges — the raw conversation messages since the last compaction that must now be integrated into the record.

This ordering is intentional. The Ledger occupies the middle of context where archival material belongs — present but not competing for attention. The Living State sits closer to the new messages, keeping current operational reality in sharp focus as you process what is new and produce your output.

---

#### Output

Return the complete, updated compaction history document structured as follows:

```
# Ledger
[All existing entries unchanged, new entry appended at end]

# Living State
[Rewritten to reflect current reality as of the end of the new message exchanges]
```

The Ledger comes first. The Living State comes last. This ensures that when this document is fed back into the next compaction cycle, the Living State will again sit closest to the new messages — maintaining optimal positioning across every cycle.

---

#### The Two Layers

**Ledger**

The Ledger is a minimal topical index. Each compaction cycle appends one short entry listing WHAT was discussed. The full conversation is permanently archived in the source log file — the Ledger does not need to preserve detail, narrative, or reasoning.

Think of the Ledger as a table of contents for the archived logs. A future reader scanning the Ledger should be able to say: "The pricing discussion happened in Compaction #3 — I'll use remember_details(log_003) to see exactly what was said." The Ledger tells them WHERE to look. The source log has the content.

Once a Ledger entry is written, it is never edited, rewritten, or removed by future compaction cycles.

Each Ledger entry is a short bullet list — one line per topic, one sentence per bullet. No paragraphs. No narrative. No analysis.

Ledger entry format:

```
## Compaction #N (source: log_NNN) — [first message timestamp] → [last message timestamp]

- [Topic]: [one-sentence summary of what happened or was decided]
- [Topic]: [one-sentence summary]
- [Topic]: [one-sentence summary]
```

Ledger rules:
1. Append only. New entries are added at the end. Existing entries are never modified.
2. Bullet points only. No paragraphs. No narrative prose.
3. One line per topic. Maximum one sentence per bullet. If a topic needs more detail, the source log has it.
4. Preserve in every bullet: named entities mentioned, decisions made, commitments given, and key facts (numbers, dates, names). These are the searchable anchors that help remember() find the right Ledger entry.
5. Do NOT preserve: reasoning, back-and-forth discussion, suggestions, emotional context, analysis, or how conclusions were reached. Those live in the source log.
6. Do NOT preserve: retry sequences, troubleshooting steps, tool errors, testing play-by-play, or operational narrative. If the conversation was primarily testing/debugging, summarize the OUTCOME only: what worked, what was decided, what changed.
7. Do NOT restate facts or decisions already established in prior Ledger entries unless they materially changed. If a preference or fact is unchanged from earlier entries, skip it.
8. Include the source log reference in the header.
9. Sequential numbering starting from 1.

Example of a GOOD Ledger entry:

```
## Compaction #2 (source: log_002) — 2026-03-25T06:20 → 2026-03-25T06:21

- Birthday dinner for Alex (35, April 12): sushi + vegetarian girlfriend + shellfish allergy + omakase preference, $50/pp budget. City/group size still open.
- Guitar learning: user knows G/C/D/Em chords, barre chords hurt. Gradual approach discussed.
- Roth vs traditional IRA: user is 32, makes ~$85k. Basics explained, no decision yet.
```

Example of a BAD Ledger entry (too verbose — this defeats the purpose):

```
## Compaction #2

The user introduced several unrelated topics. First, they asked for help planning a birthday dinner for their friend Alex, who is turning 35 on April 12th. Alex loves sushi, but his girlfriend is vegetarian. The budget is around $50 per person. The assistant asked clarifying questions about city, group size, and vibe...
```

The bad example narrates the conversation. The source log already has the conversation. The Ledger just indexes it.

**Living State**

The Living State is the mutable, current-truth layer. It represents what is true, active, and relevant right now. It is rewritten on every compaction cycle to reflect the latest reality.

A reader who only reads the Living State should be able to step into this conversation and operate competently — understanding what is happening, who is involved, what has been decided, and what is pending.

The Living State is not a summary of the conversation. It is a maintained snapshot of current reality as understood through this context space's domain. Old information that is no longer active does not persist here — it lives in the Ledger (and in full fidelity in the source logs).

The Living State should be detailed enough that a future agent can continue operating, but not so detailed that it recreates the conversation. Focus on current truth, active threads, and pending items. Drop testing/debugging narratives, resolved troubleshooting, and operational play-by-play. Deep history is recoverable from source logs — the Living State is for orientation, not reconstruction.

The context space definition determines the character of the Living State:
- In a transactional/operational context, the Living State reads like a dashboard — specific, data-dense, covering recent activity at full fidelity.
- In a narrative/creative context, the Living State reads like the current chapter — who is where, what just happened, what tensions are active, what is about to happen.
- In a technical/engineering context, the Living State reads like a working document — current architecture, active problems, recent decisions, open threads.

Organize the Living State into whatever sections serve the domain. Suggested sections (adapt freely):
- Current Situation — What is happening right now.
- Active Entities — People, systems, characters, accounts currently in play.
- Open Items — Unresolved questions, pending tasks, things deferred.
- Recent Decisions — What has been decided recently and why, if the reasoning still matters.

---

#### How to Operate

First compaction (no previous history):

1. Write Ledger entry Compaction #1 — a short bullet list of topics discussed.
2. Construct the initial Living State from the new message exchanges.
3. Return the complete document: Ledger then Living State.

Subsequent compactions:

1. Pass through all existing Ledger entries unchanged. Do not touch them.
2. Write a new Ledger entry. Append it after the last existing entry. Bullet list of topics — brief, indexed, not narrative.
3. Rewrite the Living State. Update it to reflect current reality as of the end of the new message exchanges. Remove what is no longer active. Add what is new. Information aging out of the Living State is preserved in the Ledger index AND in full detail in the source logs — nothing is truly lost.
4. Return the complete document — full Ledger (all prior entries unchanged, new entry appended), followed by the updated Living State.

---

#### Domain-Aware Judgment

The context space definition tells you what this space is for. Use it as your editorial lens:

What belongs in the Living State — anything a participant needs to know to continue operating right now. Be thorough here. The Living State is the agent's working memory.

What resolution for the Ledger — always bullet points, always minimal. One line per topic. The source log has the full resolution.

What to discard entirely from both layers — mechanical exchanges, redundant restatements, thinking-out-loud that led nowhere, greetings, acknowledgments. Information with zero retrieval value.

What to promote from noise to signal — sometimes an exchange that would normally be discarded carries unusual weight. An offhand remark that reveals a constraint. A name mentioned in passing. A number. These become bullet points in the Ledger even if they seem minor — they're the searchable anchors that help future retrieval find the right source log.

When ambiguous about the Living State — err toward keeping it one more cycle. The next compaction can remove it.

When ambiguous about the Ledger — include a bullet. One extra line costs nothing. A missed entity or decision means remember() can't find the source log.

---

#### Rules

1. Never fabricate. Every fact in either layer must originate from the message exchanges or the prior compaction history.
2. Living State is rewritten freely. It reflects current truth. Old states are preserved in the Ledger.
3. Ledger entries are immutable. Once written, never edited, merged, reworded, or removed.
4. Preserve specificity in both layers. Names, numbers, dates, identifiers — these survive compaction at full fidelity. "Alex is turning 35 on April 12" not "a friend has a birthday coming up."
5. Ledger entries are bullet points only. No paragraphs. No narrative. Each bullet is one topic, one sentence.
6. Living State should be rich and detailed. It is the working document. Don't apply the Ledger's minimalism here.
7. The document must enable continuity. A future reader with the Living State should be able to continue the conversation. A future reader with the Ledger should be able to find any past topic and retrieve the source log for details.
8. Number Ledger entries sequentially.
9. Every Ledger entry carries the source log reference and a message date range header.
10. Convert relative time references to absolute dates. "Yesterday" becomes the actual date (e.g., "2026-03-23"). "Last week" becomes "week of March 17, 2026." "Two hours ago" becomes the actual time if timestamps are available. Relative references become meaningless after time passes.

---

#### Files

If this context space has files (created via write_file), include a FILES section in the Living State listing each file's name and description. When files are created, updated, or deleted in the new messages, update the FILES section accordingly. Do not include file contents — only names and descriptions.

---

#### Seed Depth

After producing the Ledger entry and Living State, determine how many of the most recent messages are operationally critical for the next conversation turn to continue seamlessly. You've just read the entire conversation — you know what's active, what's resolved, and what the next turn needs to pick up without losing the thread.

At the very end of your output, on its own line, write:
SEED_DEPTH: N

Where N is the number of trailing messages to carry forward (minimum 3, maximum 25). A creative scene or active negotiation might need 15-20. Quick factual questions might need 3-5. A multi-step plan or project review might need 8-10.

---

#### Fact Harvest

After SEED_DEPTH, extract any durable facts from this conversation that should be remembered long-term. For each fact, indicate:
- ADD: A new fact not already in existing knowledge
- UPDATE <id>: An update to an existing fact (use the ID from the knowledge list below)
- REINFORCE <id>: Confirmation of an existing fact

Write:
FACT_HARVEST:
ADD: <fact content with subject>
UPDATE <id>: <updated content>
REINFORCE <id>

If no new facts, write:
FACT_HARVEST: NONE

---

#### Recurring Workflows

After FACT_HARVEST, if you notice the user repeatedly following the same multi-step workflow (3+ times in this conversation), note it. These are positive patterns worth formalizing as procedures.

RECURRING_WORKFLOWS:
- description: [what the user does each time]
  count: [how many times observed]
  trigger: [what starts the workflow]

If no recurring workflows, write:
RECURRING_WORKFLOWS: NONE

---

#### Follow-Ups

After RECURRING_WORKFLOWS, extract anything from this conversation that needs follow-up. Four types:

- USER_COMMITMENT: user said they would do something ("I'll send that tomorrow")
- AGENT_COMMITMENT: you promised to do something ("I'll check on that next week")
- EXTERNAL_DEADLINE: a date-bound obligation mentioned ("the permit expires March 15")
- FOLLOW_UP: something that needs checking back on ("let's see how that goes")

Do NOT extract follow-ups more than 90 days in the future. Long-horizon items belong in the Living State or Ledger, not as triggers.

FOLLOW_UPS:
- type: [USER_COMMITMENT|AGENT_COMMITMENT|EXTERNAL_DEADLINE|FOLLOW_UP]
  description: [what was committed to]
  due: [ISO date or "soon" or "next_week" — best estimate]
  context: [brief context for the reminder]

If no follow-ups, write:
FOLLOW_UPS: NONE"""


# ---------------------------------------------------------------------------
# CompactionService
# ---------------------------------------------------------------------------


class CompactionService:
    """Manages compaction lifecycle for all context spaces."""

    def __init__(
        self,
        state: StateStore,
        reasoning: ReasoningService,
        token_adapter: TokenAdapter,
        data_dir: str,
        events: EventStream | None = None,
    ) -> None:
        self.state = state
        self.reasoning = reasoning
        self.adapter = token_adapter
        self.data_dir = Path(data_dir)
        self.events = events
        self._files = None  # Set by handler after construction

    def set_files(self, files: Any) -> None:
        """Wire up the file service for manifest injection during compaction."""
        self._files = files

    def _space_dir(self, instance_id: str, space_id: str, member_id: str = "") -> Path:
        from kernos.utils import _safe_name
        base = (
            self.data_dir
            / _safe_name(instance_id)
            / "state"
            / "compaction"
            / _safe_name(space_id)
        )
        if member_id:
            return base / _safe_name(member_id)
        return base

    # --- Persistence ---

    async def load_state(
        self, instance_id: str, space_id: str, member_id: str = "",
    ) -> CompactionState | None:
        """Load CompactionState from disk. Returns None if not found.

        DISCLOSURE-GATE: previously fell back to the legacy (unscoped) path
        when the member-scoped path was missing. That fallback leaked
        another member's compaction state into the requesting member's
        context. Removed. If a member has no compaction state of their
        own, return None — do not read a shared file.
        """
        state_path = self._space_dir(instance_id, space_id, member_id) / "state.json"
        if not state_path.exists():
            return None
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return CompactionState(
                space_id=data.get("space_id", space_id),
                history_tokens=data.get("history_tokens", 0),
                compaction_number=data.get("compaction_number", 0),
                global_compaction_number=data.get("global_compaction_number", 0),
                archive_count=data.get("archive_count", 0),
                message_ceiling=data.get("message_ceiling", 0),
                document_budget=data.get("document_budget", 0),
                conversation_headroom=data.get("conversation_headroom", 0),
                cumulative_new_tokens=data.get("cumulative_new_tokens", 0),
                last_compaction_at=data.get("last_compaction_at", ""),
                index_tokens=data.get("index_tokens", 0),
                # .env is always authoritative — persisted value ignored
                compaction_threshold=int(os.getenv("KERNOS_COMPACTION_THRESHOLD", "8000")),
                _context_def_tokens=data.get("_context_def_tokens", 0),
                _system_overhead=data.get("_system_overhead", 0),
                consecutive_failures=data.get("consecutive_failures", 0),
                last_compaction_failure_at=data.get("last_compaction_failure_at", ""),
            )
        except Exception as exc:
            logger.warning("Failed to load compaction state for %s/%s: %s", instance_id, space_id, exc)
            return None

    async def save_state(
        self, instance_id: str, space_id: str, comp_state: CompactionState,
        member_id: str = "",
    ) -> None:
        """Persist CompactionState to disk."""
        space_dir = self._space_dir(instance_id, space_id, member_id)
        space_dir.mkdir(parents=True, exist_ok=True)
        state_path = space_dir / "state.json"
        data = {
            "space_id": comp_state.space_id,
            "history_tokens": comp_state.history_tokens,
            "compaction_number": comp_state.compaction_number,
            "global_compaction_number": comp_state.global_compaction_number,
            "archive_count": comp_state.archive_count,
            "message_ceiling": comp_state.message_ceiling,
            "document_budget": comp_state.document_budget,
            "conversation_headroom": comp_state.conversation_headroom,
            "cumulative_new_tokens": comp_state.cumulative_new_tokens,
            "last_compaction_at": comp_state.last_compaction_at,
            "index_tokens": comp_state.index_tokens,
            "compaction_threshold": comp_state.compaction_threshold,
            "_context_def_tokens": comp_state._context_def_tokens,
            "_system_overhead": comp_state._system_overhead,
            "consecutive_failures": comp_state.consecutive_failures,
            "last_compaction_failure_at": comp_state.last_compaction_failure_at,
            "last_seed_depth": comp_state.last_seed_depth,
        }
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    async def load_document(
        self, instance_id: str, space_id: str, member_id: str = "",
    ) -> str | None:
        """Load the active compaction document. Returns None if not found.

        DISCLOSURE-GATE: legacy lazy-migration fallback removed — it was
        the read-side of the scenario-04 leak where one member picked up
        another member's compaction document.
        """
        doc_path = self._space_dir(instance_id, space_id, member_id) / "active_document.md"
        if not doc_path.exists():
            return None
        return doc_path.read_text(encoding="utf-8")

    # --- Bounded context loading (SPEC-LEDGER-ARCHITECTURE) ---

    HOT_TAIL_BUDGET = 2000  # tokens

    async def load_context_document(
        self, instance_id: str, space_id: str,
        hot_tail_budget: int = 0, member_id: str = "",
    ) -> str:
        """Load a context-ready version: archive story + hot tail + Living State.

        Full document remains on disk unchanged. Only the context-loaded
        version is bounded.
        """
        if hot_tail_budget <= 0:
            hot_tail_budget = self.HOT_TAIL_BUDGET

        document = await self.load_document(instance_id, space_id, member_id)
        if not document:
            return ""

        entries = self._parse_ledger_entries(document)
        living_state = self._extract_living_state(document)

        # Select hot tail (most recent entries within budget)
        hot_entries = self._select_hot_tail(entries, hot_tail_budget)
        archived_count = len(entries) - len(hot_entries)

        parts: list[str] = []

        # Archive story (if we have archived entries)
        if archived_count > 0:
            story = self._load_archive_story(instance_id, space_id)
            if story:
                parts.append(
                    f"Archive: [{story.get('date_range_start', '?')} → "
                    f"{story.get('date_range_end', '?')}]\n"
                    f"{story.get('story', '')}"
                )
            elif archived_count > 0:
                # First load — generate archive story
                archived_entries = entries[:archived_count]
                story = await self._generate_initial_archive_story(
                    instance_id, space_id, archived_entries,
                )
                if story:
                    parts.append(
                        f"Archive: [{story.get('date_range_start', '?')} → "
                        f"{story.get('date_range_end', '?')}]\n"
                        f"{story.get('story', '')}"
                    )

        if hot_entries:
            parts.append("# Recent Ledger\n" + "\n\n".join(hot_entries))
        if living_state:
            parts.append("# Living State\n" + living_state)

        return "\n\n".join(parts)

    def _select_hot_tail(self, entries: list[str], budget_tokens: int) -> list[str]:
        """Select most recent entries that fit within token budget."""
        selected: list[str] = []
        total = 0
        for entry in reversed(entries):
            entry_tokens = len(entry) // 4  # rough estimate
            if total + entry_tokens > budget_tokens:
                break
            selected.insert(0, entry)
            total += entry_tokens
        # Always include at least the most recent entry
        if not selected and entries:
            selected = [entries[-1]]
        return selected

    def _load_archive_story(self, instance_id: str, space_id: str) -> dict | None:
        """Load the archive story from disk."""
        path = self._space_dir(instance_id, space_id) / "archive_story.json"
        if not path.exists():
            return None
        try:
            import json as _json
            return _json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _save_archive_story(self, instance_id: str, space_id: str, story: dict) -> None:
        """Save archive story to disk."""
        import json as _json
        path = self._space_dir(instance_id, space_id) / "archive_story.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps(story, indent=2, ensure_ascii=False), encoding="utf-8")

    async def _generate_initial_archive_story(
        self, instance_id: str, space_id: str, archived_entries: list[str],
    ) -> dict | None:
        """One-time generation of archive story from all archived entries."""
        if not self.reasoning or not archived_entries:
            return None
        try:
            entries_text = "\n\n".join(archived_entries[:30])  # cap input
            result = await self.reasoning.complete_simple(
                system_prompt=(
                    "Summarize the following ledger entries into a short archive "
                    "synopsis (under 100 words). Capture the story of this period "
                    "— what kind of work/conversation it represents, key milestones, "
                    "and the durable through-line. Not a list of every topic — a "
                    "narrative orientation."
                ),
                user_content=entries_text,
                max_tokens=256,
                prefer_cheap=True,
            )
            # Extract date range from entries
            import re as _re
            dates = _re.findall(r'(\d{4}-\d{2}-\d{2})', entries_text)
            date_start = dates[0] if dates else "?"
            date_end = dates[-1] if dates else "?"

            story = {
                "date_range_start": date_start,
                "date_range_end": date_end,
                "story": result.strip(),
                "archived_entry_count": len(archived_entries),
                "last_updated_at": utc_now(),
                "last_archived_compaction": len(archived_entries),
            }
            self._save_archive_story(instance_id, space_id, story)
            logger.info(
                "ARCHIVE_STORY_CREATED: space=%s entries=%d",
                space_id, len(archived_entries),
            )
            return story
        except Exception as exc:
            logger.warning("ARCHIVE_STORY_FAILED: %s", exc)
            return None

    async def update_archive_story(
        self, instance_id: str, space_id: str, newly_archived_entry: str,
    ) -> None:
        """Incrementally update archive story when an entry falls off hot tail."""
        existing = self._load_archive_story(instance_id, space_id)
        if not existing or not self.reasoning:
            return
        try:
            result = await self.reasoning.complete_simple(
                system_prompt=(
                    f"You maintain an archive synopsis for a conversation space.\n\n"
                    f"Current archive synopsis ({existing['date_range_start']} → "
                    f"{existing['date_range_end']}):\n"
                    f"{existing['story']}\n\n"
                    f"A new ledger entry has been archived:\n{newly_archived_entry}\n\n"
                    f"Does this entry materially change or add to the archive synopsis?\n"
                    f"- If YES: rewrite the synopsis incorporating the new information. "
                    f"Keep it under 100 words.\n"
                    f"- If NO: respond with exactly NO_UPDATE"
                ),
                user_content="",
                max_tokens=256,
                prefer_cheap=True,
            )
            if "NO_UPDATE" not in result.upper():
                # Extract new date range end
                import re as _re
                dates = _re.findall(r'(\d{4}-\d{2}-\d{2})', newly_archived_entry)
                if dates:
                    existing["date_range_end"] = dates[-1]
                existing["story"] = result.strip()
                existing["archived_entry_count"] = existing.get("archived_entry_count", 0) + 1
                existing["last_updated_at"] = utc_now()
                self._save_archive_story(instance_id, space_id, existing)
                logger.info("ARCHIVE_STORY_UPDATED: space=%s", space_id)
        except Exception as exc:
            logger.warning("ARCHIVE_STORY_UPDATE_FAILED: %s", exc)

    async def load_index(
        self, instance_id: str, space_id: str, member_id: str = "",
    ) -> str | None:
        """Load the compaction index. Returns None if not found.

        DISCLOSURE-GATE: legacy lazy-migration fallback removed — this was
        the exact read path surfacing Emma's compaction index to Harold in
        the scenario-04 leak. If member_id has no index, return None.
        """
        index_path = self._space_dir(instance_id, space_id, member_id) / "index.md"
        if not index_path.exists():
            return None
        return index_path.read_text(encoding="utf-8")

    async def load_archive(
        self, instance_id: str, space_id: str, archive_number: str,
        member_id: str = "",
    ) -> str | None:
        """Load a specific compaction archive by number. Returns None if not found."""
        # Normalize archive number — handle "1", "001", "#1", "Archive #1", etc.
        num_str = "".join(c for c in archive_number if c.isdigit())
        if not num_str:
            return None
        archive_name = f"compaction_archive_{int(num_str):03d}.md"
        archive_path = self._space_dir(instance_id, space_id, member_id) / "archives" / archive_name
        if not archive_path.exists():
            return None
        return archive_path.read_text(encoding="utf-8")

    # --- Trigger ---

    async def should_compact(
        self, space_id: str, comp_state: CompactionState
    ) -> bool:
        """Check if accumulated new messages exceed the ceiling."""
        return comp_state.cumulative_new_tokens >= comp_state.message_ceiling

    # --- Ceiling ---

    def _compute_ceiling(self, comp_state: CompactionState) -> int:
        """Compute message ceiling — max new tokens before compaction fires."""
        return (
            COMPACTION_MODEL_USABLE_TOKENS
            - COMPACTION_INSTRUCTION_TOKENS
            - comp_state._context_def_tokens
            - comp_state.history_tokens
        )

    # --- Formatting ---

    def _format_messages(self, messages: list[dict]) -> str:
        """Format messages for the compaction prompt."""
        lines = []
        for msg in messages:
            role = "User" if msg.get("role") == "user" else "Agent"
            ts = msg.get("timestamp", "")
            content = msg.get("content", "")
            if ts:
                lines.append(f"[{role}, {ts}]: {content}")
            else:
                lines.append(f"[{role}]: {content}")
        return "\n\n".join(lines)

    # --- Document parsing ---

    def _parse_ledger_entries(self, document: str) -> list[str]:
        """Split the Ledger section into individual entries."""
        ledger_match = re.search(
            r'# Ledger\s*\n(.*?)(?=# Living State)', document, re.DOTALL
        )
        if not ledger_match:
            return []

        ledger_text = ledger_match.group(1)
        entries = re.split(r'(?=## Compaction #\d+)', ledger_text)
        return [e.strip() for e in entries if e.strip()]

    def _extract_living_state(self, document: str) -> str:
        """Extract the Living State section from a compaction document."""
        match = re.search(r'# Living State\s*\n(.*)', document, re.DOTALL)
        return match.group(1).strip() if match else ""

    def _extract_forward_relevant_entries(
        self, document: str, current_compaction_number: int,
    ) -> str:
        """Extract the last 2 Ledger entries for carry-forward on rotation."""
        entries = self._parse_ledger_entries(document)
        if not entries:
            return ""
        forward = entries[-2:]
        return "\n\n".join(forward)

    # --- ROUTER-EVIDENCE-V1: public read-only accessors ---
    #
    # ROUTER-EVIDENCE-V1 batch 2.2 surfaces the Living State and the most
    # recent Ledger entries to the router cohort as orientation evidence.
    # These thin wrappers avoid forcing call sites to reach into the
    # private extractors above. Member scope is inherited from
    # ``load_document`` / ``_space_dir`` — the same disclosure boundary
    # that prevented the scenario-04 leak path.

    async def load_living_state(
        self, instance_id: str, space_id: str, member_id: str = "",
    ) -> str:
        """Return the Living State section, or empty string if no document.

        Member-scoped via ``_space_dir``. Empty member_id falls back to
        the unowned/legacy path, mirroring ``load_document``.
        """
        document = await self.load_document(instance_id, space_id, member_id)
        if not document:
            return ""
        return self._extract_living_state(document)

    async def load_recent_ledger_entries(
        self, instance_id: str, space_id: str, member_id: str = "",
        n: int = 3,
    ) -> list[str]:
        """Return the last ``n`` Ledger entries, oldest-to-newest among the tail.

        Implementation contract per Codex review 2026-05-09 v2 finding 2:
          * ``entries[-n:]`` ordering — oldest first within the returned
            window so callers can render chronologically without
            reversing.
          * ``n <= 0`` returns ``[]`` (no work, no surprise).
          * Missing document returns ``[]``.

        Member-scoped via ``_space_dir``.
        """
        if n <= 0:
            return []
        document = await self.load_document(instance_id, space_id, member_id)
        if not document:
            return []
        entries = self._parse_ledger_entries(document)
        if not entries:
            return []
        return entries[-n:]

    # --- Compact ---

    async def compact(
        self,
        instance_id: str,
        space_id: str,
        space: ContextSpace,
        new_messages: list[dict],
        comp_state: CompactionState,
    ) -> CompactionState:
        """Run one compaction cycle."""
        space_dir = self._space_dir(instance_id, space_id)
        space_dir.mkdir(parents=True, exist_ok=True)

        # Emit compaction.triggered
        try:
            if self.events:
                await emit_event(
                    self.events,
                    EventType.COMPACTION_TRIGGERED,
                    instance_id,
                    "compaction",
                    payload={
                        "space_id": space_id,
                        "compaction_number": comp_state.global_compaction_number + 1,
                        "cumulative_new_tokens": comp_state.cumulative_new_tokens,
                        "message_ceiling": comp_state.message_ceiling,
                    },
                )
        except Exception as exc:
            logger.warning("Failed to emit compaction.triggered: %s", exc)

        # Load existing document
        active_doc_path = space_dir / "active_document.md"
        existing_doc = active_doc_path.read_text(encoding="utf-8") if active_doc_path.exists() else ""

        # Format new messages
        messages_text = self._format_messages(new_messages)

        # Context space definition
        space_definition = (
            f"Space: {space.name}\n"
            f"Type: {space.space_type}\n"
            f"Description: {space.description}\n"
            f"Posture: {space.posture}\n"
        )

        # Manifest injection — tell the compaction model what files exist
        if self._files:
            try:
                manifest = await self._files.load_manifest(instance_id, space_id)
                if manifest:
                    manifest_text = "Current files in this space:\n"
                    for fname, desc in manifest.items():
                        manifest_text += f"  - {fname}: {desc}\n"
                    space_definition += f"\n{manifest_text}"
            except Exception as exc:
                logger.warning("Failed to load manifest for compaction injection: %s", exc)

        # Build the compaction call
        user_content = ""
        if existing_doc:
            user_content += f"Previous Compaction History:\n\n{existing_doc}\n\n---\n\n"
        user_content += f"New Message Exchanges:\n\n{messages_text}"

        updated_doc = await self.reasoning.complete_simple(
            system_prompt=COMPACTION_SYSTEM_PROMPT,
            user_content=(
                f"Context Space Definition:\n\n{space_definition}\n\n---\n\n"
                f"{user_content}"
            ),
            max_tokens=16000,
            prefer_cheap=True,
        )

        # Store updated document
        active_doc_path.write_text(updated_doc, encoding="utf-8")

        # Re-count tokens via adapter
        new_history_tokens = await self.adapter.count_tokens(updated_doc)

        # Update state
        comp_state.history_tokens = new_history_tokens
        comp_state.compaction_number += 1
        comp_state.global_compaction_number += 1
        comp_state.cumulative_new_tokens = 0
        comp_state.last_compaction_at = utc_now()

        # Recompute message ceiling
        comp_state.message_ceiling = self._compute_ceiling(comp_state)

        # Check rotation
        if new_history_tokens > comp_state.document_budget:
            await self._rotate(instance_id, space_id, space, comp_state)

        # Save state
        await self.save_state(instance_id, space_id, comp_state)

        # Emit compaction.completed
        try:
            if self.events:
                await emit_event(
                    self.events,
                    EventType.COMPACTION_COMPLETED,
                    instance_id,
                    "compaction",
                    payload={
                        "space_id": space_id,
                        "compaction_number": comp_state.global_compaction_number,
                        "history_tokens": comp_state.history_tokens,
                        "message_ceiling": comp_state.message_ceiling,
                    },
                )
        except Exception as exc:
            logger.warning("Failed to emit compaction.completed: %s", exc)

        return comp_state

    @staticmethod
    def _parse_seed_depth(doc: str) -> int:
        """Extract SEED_DEPTH from compaction output. Default 10."""
        import re
        for line in reversed(doc.strip().split("\n")):
            line = line.strip()
            if line.upper().startswith("SEED_DEPTH:"):
                try:
                    n = int(re.sub(r"[^0-9]", "", line.split(":")[1]))
                    return max(3, min(25, n))
                except (ValueError, IndexError):
                    pass
        return 10

    @staticmethod
    def _strip_seed_depth(doc: str) -> str:
        """Remove the SEED_DEPTH line from the document."""
        lines = doc.rstrip().split("\n")
        cleaned = []
        for line in lines:
            if line.strip().upper().startswith("SEED_DEPTH:"):
                continue
            cleaned.append(line)
        return "\n".join(cleaned)

    @staticmethod
    def _parse_fact_harvest(doc: str) -> list[dict]:
        """Extract FACT_HARVEST section from compaction output.

        Returns list of {action: "add"|"update"|"reinforce", id: str, content: str}.
        """
        results = []
        in_harvest = False
        for line in doc.strip().split("\n"):
            stripped = line.strip()
            if stripped.upper().startswith("FACT_HARVEST:"):
                rest = stripped[len("FACT_HARVEST:"):].strip()
                if rest.upper() == "NONE":
                    return []
                in_harvest = True
                # The first line might have content after the colon
                if rest.upper().startswith("ADD:"):
                    results.append({"action": "add", "id": "", "content": rest[4:].strip()})
                elif rest.upper().startswith("UPDATE"):
                    parts = rest.split(":", 1)
                    _id = parts[0].replace("UPDATE", "").strip()
                    _content = parts[1].strip() if len(parts) > 1 else ""
                    results.append({"action": "update", "id": _id, "content": _content})
                elif rest.upper().startswith("REINFORCE"):
                    _id = rest.replace("REINFORCE", "").strip()
                    results.append({"action": "reinforce", "id": _id, "content": ""})
                continue
            if in_harvest:
                if stripped.upper().startswith("ADD:"):
                    results.append({"action": "add", "id": "", "content": stripped[4:].strip()})
                elif stripped.upper().startswith("UPDATE"):
                    parts = stripped.split(":", 1)
                    _id = parts[0].replace("UPDATE", "").strip()
                    _content = parts[1].strip() if len(parts) > 1 else ""
                    results.append({"action": "update", "id": _id, "content": _content})
                elif stripped.upper().startswith("REINFORCE"):
                    _id = stripped.replace("REINFORCE", "").strip()
                    results.append({"action": "reinforce", "id": _id, "content": ""})
                elif not stripped:
                    continue  # blank line in harvest section
                else:
                    break  # end of harvest section
        return results

    @staticmethod
    def _strip_fact_harvest(doc: str) -> str:
        """Remove the FACT_HARVEST section from the document."""
        lines = doc.rstrip().split("\n")
        cleaned = []
        in_harvest = False
        for line in lines:
            stripped = line.strip()
            if stripped.upper().startswith("FACT_HARVEST:"):
                in_harvest = True
                continue
            if in_harvest:
                if stripped.upper().startswith(("ADD:", "UPDATE", "REINFORCE")) or not stripped:
                    continue
                else:
                    in_harvest = False
            if not in_harvest:
                cleaned.append(line)
        return "\n".join(cleaned)

    @staticmethod
    def _parse_recurring_workflows(doc: str) -> list[dict]:
        """Extract RECURRING_WORKFLOWS section from compaction output.

        Returns list of {"description": str, "count": int, "trigger": str}.
        """
        results = []
        in_workflows = False
        current: dict = {}
        for line in doc.strip().split("\n"):
            stripped = line.strip()
            if stripped.upper().startswith("RECURRING_WORKFLOWS:"):
                rest = stripped[len("RECURRING_WORKFLOWS:"):].strip()
                if rest.upper() == "NONE":
                    return []
                in_workflows = True
                continue
            if in_workflows:
                if stripped.startswith("- description:"):
                    if current:
                        results.append(current)
                    current = {"description": stripped[len("- description:"):].strip(), "count": 0, "trigger": ""}
                elif stripped.startswith("description:"):
                    if current:
                        results.append(current)
                    current = {"description": stripped[len("description:"):].strip(), "count": 0, "trigger": ""}
                elif stripped.startswith("count:"):
                    try:
                        current["count"] = int(stripped[len("count:"):].strip())
                    except ValueError:
                        pass
                elif stripped.startswith("trigger:"):
                    current["trigger"] = stripped[len("trigger:"):].strip()
                elif not stripped:
                    continue
                elif not stripped.startswith("-") and ":" not in stripped:
                    # End of section
                    break
            # Also handle single-line format: "- description | count | trigger"
        if current and current.get("description"):
            results.append(current)
        return results

    @staticmethod
    def _strip_recurring_workflows(doc: str) -> str:
        """Remove the RECURRING_WORKFLOWS section from the document."""
        lines = doc.rstrip().split("\n")
        cleaned = []
        in_workflows = False
        for line in lines:
            stripped = line.strip()
            if stripped.upper().startswith("RECURRING_WORKFLOWS:"):
                in_workflows = True
                continue
            if in_workflows:
                if stripped.startswith(("-", "description:", "count:", "trigger:")) or not stripped:
                    continue
                else:
                    in_workflows = False
            if not in_workflows:
                cleaned.append(line)
        return "\n".join(cleaned)

    @staticmethod
    def _parse_follow_ups(doc: str) -> list[dict]:
        """Extract FOLLOW_UPS section from compaction output.

        Returns list of {"type": str, "description": str, "due": str, "context": str}.
        """
        results = []
        in_commitments = False
        current: dict = {}
        for line in doc.strip().split("\n"):
            stripped = line.strip()
            if stripped.upper().startswith("FOLLOW_UPS:"):
                rest = stripped[len("FOLLOW_UPS:"):].strip()
                if rest.upper() == "NONE":
                    return []
                in_commitments = True
                continue
            if in_commitments:
                if stripped.startswith("- type:"):
                    if current and current.get("description"):
                        results.append(current)
                    current = {"type": stripped[len("- type:"):].strip(), "description": "", "due": "", "context": ""}
                elif stripped.startswith("type:"):
                    if current and current.get("description"):
                        results.append(current)
                    current = {"type": stripped[len("type:"):].strip(), "description": "", "due": "", "context": ""}
                elif stripped.startswith("description:"):
                    current["description"] = stripped[len("description:"):].strip()
                elif stripped.startswith("due:"):
                    current["due"] = stripped[len("due:"):].strip()
                elif stripped.startswith("context:"):
                    current["context"] = stripped[len("context:"):].strip()
                elif not stripped:
                    continue
                elif not stripped.startswith("-") and ":" not in stripped:
                    break
        if current and current.get("description"):
            results.append(current)
        return results

    @staticmethod
    def _strip_follow_ups(doc: str) -> str:
        """Remove the FOLLOW_UPS section from the document."""
        lines = doc.rstrip().split("\n")
        cleaned = []
        in_commitments = False
        for line in lines:
            stripped = line.strip()
            if stripped.upper().startswith("FOLLOW_UPS:"):
                in_commitments = True
                continue
            if in_commitments:
                if stripped.startswith(("-", "type:", "description:", "due:", "context:")) or not stripped:
                    continue
                else:
                    in_commitments = False
            if not in_commitments:
                cleaned.append(line)
        return "\n".join(cleaned)

    async def compact_from_log(
        self,
        instance_id: str,
        space_id: str,
        space: ContextSpace,
        log_text: str,
        source_log_number: int,
        comp_state: CompactionState,
        member_id: str = "",
    ) -> CompactionState:
        """Run compaction from a space log file (P3).

        Args:
            log_text: The full text of the current log file.
            source_log_number: The log number (e.g., 3 for log_003.txt).
            comp_state: Current compaction state for this space.

        Returns: Updated CompactionState after successful compaction.
        """
        # DISCLOSURE-GATE: compaction artifacts MUST be member-scoped. When
        # multiple members share a space (e.g., two members on the same
        # default space), an unscoped compaction document leaks one member's
        # transcript summary into the other member's memory block via the
        # lazy-migration fallback in load_index/load_active_document. This
        # was the scenario-04 R1 leak path: Emma's compaction wrote to the
        # legacy (unscoped) path, and Harold's load_index picked it up.
        space_dir = self._space_dir(instance_id, space_id, member_id)
        space_dir.mkdir(parents=True, exist_ok=True)

        # EVENT-STREAM-TO-SQLITE: compaction trigger emission.
        try:
            from kernos.kernel import event_stream
            await event_stream.emit(
                instance_id, "compaction.triggered",
                {
                    "source_log": source_log_number,
                    "log_bytes": len(log_text),
                    "space_name": space.name,
                },
                member_id=member_id or None,
                space_id=space_id,
            )
        except Exception as exc:
            logger.debug("Failed to emit compaction.triggered: %s", exc)

        # Load existing compaction document
        active_doc_path = space_dir / "active_document.md"
        existing_doc = (
            active_doc_path.read_text(encoding="utf-8")
            if active_doc_path.exists() else ""
        )

        # Space definition
        space_definition = (
            f"Space: {space.name}\n"
            f"Type: {space.space_type}\n"
            f"Description: {space.description}\n"
            f"Posture: {space.posture}\n"
        )

        # Manifest injection
        if self._files:
            try:
                manifest = await self._files.load_manifest(instance_id, space_id)
                if manifest:
                    manifest_text = "Current files in this space:\n"
                    for fname, desc in manifest.items():
                        manifest_text += f"  - {fname}: {desc}\n"
                    space_definition += f"\n{manifest_text}"
            except Exception as exc:
                logger.warning("Manifest load failed for compaction: %s", exc)

        # Build existing knowledge list for fact harvest
        _knowledge_section = ""
        try:
            _ke = await self.state.query_knowledge(instance_id, active_only=True, limit=100)
            if _ke:
                _ke_lines = [f"  [{e.id}] {e.subject}: {e.content[:100]}" for e in _ke[:50]]
                _knowledge_section = f"\n\nExisting knowledge (for FACT_HARVEST UPDATE/REINFORCE):\n" + "\n".join(_ke_lines)
        except Exception:
            pass

        # Build compaction prompt with source log reference
        user_content = ""
        if existing_doc:
            user_content += f"Previous Compaction History:\n\n{existing_doc}\n\n---\n\n"
        user_content += (
            f"Source: log_{source_log_number:03d}\n"
            f"You are compacting source log log_{source_log_number:03d}. "
            f"Include this reference in your Ledger entry header.\n\n"
            f"New Message Exchanges:\n\n{log_text}"
            f"{_knowledge_section}"
        )

        updated_doc = await self.reasoning.complete_simple(
            system_prompt=COMPACTION_SYSTEM_PROMPT + f"\n\n{space_definition}",
            user_content=user_content,
            max_tokens=16000,
            prefer_cheap=True,
        )

        # Parse and strip adaptive seed depth
        seed_depth = self._parse_seed_depth(updated_doc)
        updated_doc = self._strip_seed_depth(updated_doc)
        comp_state.last_seed_depth = seed_depth

        # Parse and strip fact harvest
        fact_harvest = self._parse_fact_harvest(updated_doc)
        updated_doc = self._strip_fact_harvest(updated_doc)
        if fact_harvest:
            logger.info("COMPACTION_HARVEST: facts=%d adds=%d updates=%d reinforces=%d",
                len(fact_harvest),
                sum(1 for f in fact_harvest if f["action"] == "add"),
                sum(1 for f in fact_harvest if f["action"] == "update"),
                sum(1 for f in fact_harvest if f["action"] == "reinforce"))
        # Store harvest results on comp_state for the handler to process
        comp_state._fact_harvest = fact_harvest  # type: ignore[attr-defined]

        # Parse and strip recurring workflows
        recurring_workflows = self._parse_recurring_workflows(updated_doc)
        updated_doc = self._strip_recurring_workflows(updated_doc)
        if recurring_workflows:
            logger.info("COMPACTION_WORKFLOWS: count=%d", len(recurring_workflows))
        comp_state._recurring_workflows = recurring_workflows  # type: ignore[attr-defined]

        # Parse and strip commitments
        commitments = self._parse_follow_ups(updated_doc)
        updated_doc = self._strip_follow_ups(updated_doc)
        if commitments:
            logger.info("COMPACTION_FOLLOW_UPS: count=%d", len(commitments))
        comp_state._follow_ups = commitments  # type: ignore[attr-defined]

        # Write updated document
        active_doc_path.write_text(updated_doc, encoding="utf-8")

        # Update state
        new_history_tokens = await self.adapter.count_tokens(updated_doc)
        comp_state.history_tokens = new_history_tokens
        comp_state.compaction_number += 1
        comp_state.global_compaction_number += 1
        comp_state.cumulative_new_tokens = 0
        comp_state.last_compaction_at = utc_now()
        comp_state.message_ceiling = self._compute_ceiling(comp_state)

        # Check rotation
        if new_history_tokens > comp_state.document_budget:
            await self._rotate(instance_id, space_id, space, comp_state, member_id=member_id)

        await self.save_state(instance_id, space_id, comp_state, member_id=member_id)

        logger.info(
            "COMPACTION: space=%s member=%s source=log_%03d compaction_number=%d",
            space_id, member_id or "(unscoped)", source_log_number,
            comp_state.global_compaction_number,
        )

        # EVENT-STREAM-TO-SQLITE: compaction completion emission.
        try:
            from kernos.kernel import event_stream
            await event_stream.emit(
                instance_id, "compaction.completed",
                {
                    "source_log": source_log_number,
                    "compaction_number": comp_state.global_compaction_number,
                },
                member_id=member_id or None,
                space_id=space_id,
            )
        except Exception as exc:
            logger.debug("Failed to emit compaction.completed: %s", exc)

        return comp_state

    # --- Rotation ---

    async def _rotate(
        self,
        instance_id: str,
        space_id: str,
        space: ContextSpace,
        comp_state: CompactionState,
        member_id: str = "",
    ) -> None:
        """Seal active document as archive, create fresh document.

        Member-scoped: archives and index.md live under the member subdir
        so one member's compaction history cannot leak into another's.
        """
        space_dir = self._space_dir(instance_id, space_id, member_id)
        archive_dir = space_dir / "archives"
        archive_dir.mkdir(parents=True, exist_ok=True)

        active_doc_path = space_dir / "active_document.md"
        active_doc = active_doc_path.read_text(encoding="utf-8")

        # 1. Seal as archive
        comp_state.archive_count += 1
        archive_name = f"compaction_archive_{comp_state.archive_count:03d}.md"
        (archive_dir / archive_name).write_text(active_doc, encoding="utf-8")

        # 2. Generate index summary
        summary_input = active_doc[-8000:] if len(active_doc) > 8000 else active_doc
        summary = await self.reasoning.complete_simple(
            system_prompt=(
                "Write a 1-3 sentence summary of this compaction document. "
                "Include the domain, key events, and date range. "
                "This summary will be used as an index entry for future retrieval."
            ),
            user_content=summary_input,
            max_tokens=200,
            prefer_cheap=True,
        )

        # 3. Append to index
        index_path = space_dir / "index.md"
        first_compaction = comp_state.global_compaction_number - comp_state.compaction_number + 1
        last_compaction = comp_state.global_compaction_number
        index_entry = (
            f"\n## Archive #{comp_state.archive_count}\n"
            f"Compactions {first_compaction}\u2013{last_compaction} | "
            f"{comp_state.last_compaction_at[:10]}\n\n"
            f"{summary.strip()}\n"
        )

        if index_path.exists():
            existing_index = index_path.read_text(encoding="utf-8")
            index_path.write_text(existing_index + index_entry, encoding="utf-8")
        else:
            index_path.write_text("# Compaction Index\n" + index_entry, encoding="utf-8")

        # 4. Re-count index tokens
        comp_state.index_tokens = await self.adapter.count_tokens(
            index_path.read_text(encoding="utf-8")
        )

        # 4b. Personality evolution — rewrite soul.personality_notes
        try:
            await self._evolve_personality(instance_id)
        except Exception as exc:
            logger.warning("Personality evolution failed for %s: %s", instance_id, exc)

        # 5. Create new active document
        living_state = self._extract_living_state(active_doc)
        forward_entries = self._extract_forward_relevant_entries(
            active_doc, comp_state.compaction_number
        )

        new_doc = ""
        if forward_entries:
            new_doc += f"# Ledger\n\n{forward_entries}\n\n"
        else:
            new_doc += "# Ledger\n\n"
        new_doc += f"# Living State\n\n{living_state}"

        active_doc_path.write_text(new_doc, encoding="utf-8")

        # 6. Re-count and recompute
        comp_state.history_tokens = await self.adapter.count_tokens(new_doc)
        comp_state.compaction_number = 0

        # Adaptive headroom
        rotations_per_100 = (
            comp_state.archive_count
            / max(comp_state.global_compaction_number, 1)
        ) * 100

        if rotations_per_100 > 20:
            comp_state.conversation_headroom = int(
                comp_state.conversation_headroom * 0.95
            )

        comp_state.conversation_headroom = max(
            4000, min(comp_state.conversation_headroom, 40000)
        )

        comp_state.document_budget = compute_document_budget(
            MODEL_MAX_TOKENS,
            comp_state._system_overhead,
            comp_state.index_tokens,
            comp_state.conversation_headroom,
        )
        comp_state.message_ceiling = self._compute_ceiling(comp_state)

        # Emit compaction.rotation
        try:
            if self.events:
                await emit_event(
                    self.events,
                    EventType.COMPACTION_ROTATION,
                    instance_id,
                    "compaction",
                    payload={
                        "space_id": space_id,
                        "archive_count": comp_state.archive_count,
                        "new_document_budget": comp_state.document_budget,
                        "conversation_headroom": comp_state.conversation_headroom,
                    },
                )
        except Exception as exc:
            logger.warning("Failed to emit compaction.rotation: %s", exc)

        logger.info(
            "Rotated compaction document for %s/%s — archive #%d",
            instance_id, space_id, comp_state.archive_count,
        )

    async def _evolve_personality(self, instance_id: str, member_id: str = "") -> None:
        """Rewrite personality_notes based on accumulated knowledge.

        Fires only on compaction rotation — infrequent, cheap, deliberate.
        Failure never blocks rotation. Writes to member profile if available.
        """
        # Load current personality from member profile or soul
        current_personality = "(no personality notes yet)"
        if member_id and hasattr(self, '_instance_db') and self._instance_db:
            profile = await self._instance_db.get_member_profile(member_id)
            if profile:
                current_personality = profile.get("personality_notes", "") or current_personality
        else:
            soul = await self.state.get_soul(instance_id)
            if soul and soul.personality_notes:
                current_personality = soul.personality_notes

        # Load recent user knowledge entries
        user_ke = await self.state.query_knowledge(
            instance_id, subject="user", active_only=True, limit=30,
            member_id=member_id,
        )
        user_facts = [e.content for e in user_ke
                      if e.lifecycle_archetype in ("structural", "identity", "habitual")]
        if not user_facts:
            return

        facts_text = "\n".join(f"- {f}" for f in user_facts)

        result = await self.reasoning.complete_simple(
            system_prompt=(
                "You are deepening an AI agent's personality profile based on "
                "continued interaction. DEEPEN, do not replace. The existing profile "
                "is a crystallized identity — refine it with more specificity as new "
                "evidence accumulates. One unusual exchange should add nuance, not "
                "rewrite the portrait. The agent should be recognizably the same, "
                "with more texture."
            ),
            user_content=(
                f"Current personality profile:\n{current_personality}\n\n"
                f"Recent observations:\n{facts_text}\n\n"
                "Revise the personality profile. Preserve the stable core — vibe, "
                "pace, posture, boundaries. Add specificity where new evidence "
                "supports it. Describe PATTERNS, not events. "
                "'Approaches problems by mapping them to familiar frameworks' is "
                "a pattern. 'Mentioned late-night coding on March 6' is an event "
                "— do not include events. Keep it 4-8 sentences. Write a presence."
            ),
            max_tokens=300,
            prefer_cheap=True,
        )

        if member_id and hasattr(self, '_instance_db') and self._instance_db:
            await self._instance_db.upsert_member_profile(member_id, {
                "personality_notes": result.strip(),
            })
        else:
            soul = await self.state.get_soul(instance_id)
            if soul:
                soul.personality_notes = result.strip()
                await self.state.save_soul(soul, source="compaction_rotation", trigger="personality_evolution")
        logger.info("Personality evolved for %s/%s on rotation", instance_id, member_id or "legacy")

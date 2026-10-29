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


# Output budget for one compaction call: the new Ledger entry, the Living
# State and the trailer sections. Earlier Ledger entries are never output.
COMPACTION_MAX_OUTPUT_TOKENS = 12000


class CompactionOutputError(RuntimeError):
    """The compaction call's output can't be committed; the document is untouched."""


_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})[^\]]*\]\s+\[[^\]]+\]\s+\[[^\]]+\]", re.MULTILINE)


def _span_timestamps(log_text: str) -> tuple[str, str]:
    """First and last entry timestamps (to the minute) in a log span."""
    stamps = _TS_RE.findall(log_text)
    return (stamps[0], stamps[-1]) if stamps else ("", "")


_SOURCE_RE = re.compile(r"\(source: log_(\d+)")


def _entry_source_log(entry: str) -> int | None:
    """The log number an entry's header cites, if any."""
    m = _SOURCE_RE.search(entry.split("\n", 1)[0])
    return int(m.group(1)) if m else None


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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
3. Ledger So Far — the existing Ledger entries from prior compaction cycles, most recent last (absent on the first run; the oldest may be omitted when the Ledger is long). Read-only: the system keeps every entry, and you never rewrite or repeat them.
4. Current Living State — the Living State as of the previous compaction (absent on the first run).
5. Source Log Identifier — the log file this compaction is derived from (e.g., "log_003"). The full conversation text is permanently archived in this file and can be retrieved later via remember_details(). You do not need to preserve the conversation verbatim — the source log does that.
6. New Message Exchanges — the raw conversation messages since the last compaction that must now be integrated into the record.

This ordering is intentional. The Ledger occupies the middle of context where archival material belongs — present but not competing for attention. The Living State sits closer to the new messages, keeping current operational reality in sharp focus as you process what is new and produce your output.

---

#### Output

Return exactly two sections, in this order, followed by the trailer sections described below:

```
# New Ledger Entry
[The bullets of ONE new Ledger entry, covering only the new message exchanges]

# Living State
[Rewritten to reflect current reality as of the end of the new message exchanges]
```

Do not reproduce earlier Ledger entries and do not write the entry's header: the system writes the header (number, source log, date range) and appends your entry to the Ledger, so earlier entries are preserved exactly.

---

#### The Two Layers

**Ledger**

The Ledger is a minimal topical index. Each compaction cycle appends one short entry listing WHAT was discussed. The full conversation is permanently archived in the source log file — the Ledger does not need to preserve detail, narrative, or reasoning.

Think of the Ledger as a table of contents for the archived logs. A future reader scanning the Ledger should be able to say: "The pricing discussion happened in Compaction #3 — I'll use remember_details(log_003) to see exactly what was said." The Ledger tells them WHERE to look. The source log has the content.

Once a Ledger entry is written, it is never edited, rewritten, or removed by future compaction cycles.

Each Ledger entry is a short bullet list — one line per topic, one sentence per bullet. No paragraphs. No narrative. No analysis.

Ledger entry format (the system adds the `## Compaction #N (source: log_NNN) — [first message timestamp] → [last message timestamp]` header):

```
- [Topic]: [one-sentence summary of what happened or was decided]
- [Topic]: [one-sentence summary]
- [Topic]: [one-sentence summary]
```

Ledger rules:
1. Write only the new entry. Existing entries are kept by the system and never modified.
2. Bullet points only. No paragraphs. No narrative prose.
3. One line per topic. Maximum one sentence per bullet. If a topic needs more detail, the source log has it.
4. Preserve in every bullet: named entities mentioned, decisions made, commitments given, and key facts (numbers, dates, names). These are the searchable anchors that help remember() find the right Ledger entry.
5. Do NOT preserve: reasoning, back-and-forth discussion, suggestions, emotional context, analysis, or how conclusions were reached. Those live in the source log.
6. Do NOT preserve: retry sequences, troubleshooting steps, tool errors, testing play-by-play, or operational narrative. If the conversation was primarily testing/debugging, summarize the OUTCOME only: what worked, what was decided, what changed.
7. Do NOT restate facts or decisions already established in prior Ledger entries unless they materially changed. If a preference or fact is unchanged from earlier entries, skip it.

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

1. Write the new Ledger entry — a short bullet list of topics discussed.
2. Construct the initial Living State from the new message exchanges.

Subsequent compactions:

1. Read the Ledger so far, so the new entry doesn't restate what is already indexed.
2. Write the new Ledger entry for the new message exchanges only. Bullet list of topics — brief, indexed, not narrative.
3. Rewrite the Living State. Update it to reflect current reality as of the end of the new message exchanges. Remove what is no longer active. Add what is new. Information aging out of the Living State is preserved in the Ledger index AND in full detail in the source logs — nothing is truly lost.

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
3. Ledger entries are immutable. Once written, never edited, merged, reworded, or removed — so write each new entry with care.
4. Preserve specificity in both layers. Names, numbers, dates, identifiers — these survive compaction at full fidelity. "Alex is turning 35 on April 12" not "a friend has a birthday coming up."
5. Ledger entries are bullet points only. No paragraphs. No narrative. Each bullet is one topic, one sentence.
6. Living State should be rich and detailed. It is the working document. Don't apply the Ledger's minimalism here.
7. The document must enable continuity. A future reader with the Living State should be able to continue the conversation. A future reader with the Ledger should be able to find any past topic and retrieve the source log for details.
8. Convert relative time references to absolute dates. "Yesterday" becomes the actual date (e.g., "2026-03-23"). "Last week" becomes "week of March 17, 2026." "Two hours ago" becomes the actual time if timestamps are available. Relative references become meaningless after time passes.

---

#### Files

If this context space has files (created via write_file), include a FILES section in the Living State listing each file's name and description. When files are created, updated, or deleted in the new messages, update the FILES section accordingly. Do not include file contents — only names and descriptions.

---

#### Seed Depth

After producing the new Ledger entry and the Living State, determine how many of the most recent messages are operationally critical for the next conversation turn to continue seamlessly. You've just read the entire conversation — you know what's active, what's resolved, and what the next turn needs to pick up without losing the thread.

At the very end of your output, on its own line, write:
SEED_DEPTH: N

Where N is the number of trailing messages to carry forward (minimum 3, maximum 25). A creative scene or active negotiation might need 15-20. Quick factual questions might need 3-5. A multi-step plan or project review might need 8-10.

---

#### Recurring Workflows

After SEED_DEPTH, if you notice the user repeatedly following the same multi-step workflow (3+ times in this conversation), note it. These are positive patterns worth formalizing as procedures.

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
    LEDGER_INPUT_BUDGET = 8000  # tokens of prior Ledger shown to each compaction call

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

        # Archive story (if we have archived entries). It is the member's
        # own, kept current at compaction (_refresh_archive_story).
        if archived_count > 0:
            story = self._load_archive_story(instance_id, space_id, member_id)
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

    def _load_archive_story(
        self, instance_id: str, space_id: str, member_id: str = "",
    ) -> dict | None:
        """Load the member's archive story from disk."""
        path = self._space_dir(instance_id, space_id, member_id) / "archive_story.json"
        if not path.exists():
            return None
        try:
            import json as _json
            return _json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _save_archive_story(
        self, instance_id: str, space_id: str, story: dict, member_id: str = "",
    ) -> None:
        """Save the member's archive story to disk."""
        import json as _json
        path = self._space_dir(instance_id, space_id, member_id) / "archive_story.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps(story, indent=2, ensure_ascii=False), encoding="utf-8")

    async def _refresh_archive_story(
        self, instance_id: str, space_id: str, entries: list[str],
        member_id: str = "",
    ) -> None:
        """Keep the member's archive story current with the entries that have
        fallen off the hot tail. Runs at compaction, never on the turn path."""
        hot = self._select_hot_tail(entries, self.HOT_TAIL_BUDGET)
        archived = entries[: len(entries) - len(hot)]
        if not archived:
            return
        story = self._load_archive_story(instance_id, space_id, member_id)
        if story is None:
            await self._generate_initial_archive_story(
                instance_id, space_id, archived, member_id=member_id,
            )
            return
        for entry in archived[int(story.get("archived_entry_count", 0)):]:
            await self.update_archive_story(
                instance_id, space_id, entry, member_id=member_id,
            )

    async def _generate_initial_archive_story(
        self, instance_id: str, space_id: str, archived_entries: list[str],
        member_id: str = "",
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
            self._save_archive_story(instance_id, space_id, story, member_id)
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
        member_id: str = "",
    ) -> None:
        """Incrementally update archive story when an entry falls off hot tail."""
        existing = self._load_archive_story(instance_id, space_id, member_id)
        if not existing or not self.reasoning:
            return
        try:
            result = await self.reasoning.complete_simple(
                system_prompt=(
                    f"You maintain an archive synopsis for a conversation space.\n\n"
                    f"Current archive synopsis ({existing['date_range_start']} → "
                    f"{existing['date_range_end']}):\n"
                    f"{existing['story']}\n\n"
                    f"Does the ledger entry below materially change or add to the archive synopsis?\n"
                    f"- If YES: rewrite the synopsis incorporating the new information. "
                    f"Keep it under 100 words.\n"
                    f"- If NO: respond with exactly NO_UPDATE"
                ),
                user_content=f"Newly archived ledger entry:\n{newly_archived_entry}",
                max_tokens=256,
                prefer_cheap=True,
            )
            import re as _re
            dates = _re.findall(r'(\d{4}-\d{2}-\d{2})', newly_archived_entry)
            if dates:
                existing["date_range_end"] = dates[-1]
            if "NO_UPDATE" not in result.upper():
                existing["story"] = result.strip()
                logger.info("ARCHIVE_STORY_UPDATED: space=%s", space_id)
            # Count the entry either way, so it is never re-processed.
            existing["archived_entry_count"] = existing.get("archived_entry_count", 0) + 1
            existing["last_updated_at"] = utc_now()
            self._save_archive_story(instance_id, space_id, existing, member_id)
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

    async def load_all_ledger_entries(
        self, instance_id: str, space_id: str, member_id: str = "",
    ) -> list[str]:
        """Every Ledger entry the member has in this space: sealed archives
        first, then the active document, each entry once (rotation carries
        the newest entries forward into the next document)."""
        space_dir = self._space_dir(instance_id, space_id, member_id)
        texts: list[str] = []
        archive_dir = space_dir / "archives"
        if archive_dir.is_dir():
            for path in sorted(archive_dir.glob("compaction_archive_*.md")):
                texts.append(path.read_text(encoding="utf-8"))
        document = await self.load_document(instance_id, space_id, member_id)
        if document:
            texts.append(document)
        entries: list[str] = []
        seen: set[str] = set()
        for text in texts:
            for entry in self._parse_ledger_entries(text):
                if entry not in seen:
                    seen.add(entry)
                    entries.append(entry)
        return entries

    async def load_archived_living_states(
        self, instance_id: str, space_id: str, member_id: str = "",
    ) -> list[tuple[str, str]]:
        """Compatibility evidence unique to old sealed Living States."""
        folder = self._space_dir(instance_id, space_id, member_id) / "archives"
        return [(path.stem, self._extract_living_state(path.read_text(encoding="utf-8")))
                for path in sorted(folder.glob("compaction_archive_*.md"))]

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
        """Compact one span of a member's space log into the Ledger.

        The model writes only the new Ledger entry's bullets and the new
        Living State. Code writes the entry header from the span's own
        timestamps and appends the entry to the existing Ledger, so earlier
        entries never pass through the model and stay exactly as written:
        the Ledger is append-only by construction, not by request.

        Args:
            log_text: The span to compact: the current log without the
                entries seeded from the previous log (already compacted).
            source_log_number: The log number (e.g., 3 for log_003.txt).
            comp_state: Current compaction state for this space and member.

        Raises CompactionOutputError (and leaves the document untouched)
        when the model's output is truncated or lacks either section.
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
        prior_entries = self._parse_ledger_entries(existing_doc)
        prior_state = self._extract_living_state(existing_doc)

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

        user_content = ""
        if prior_entries:
            shown = self._select_hot_tail(prior_entries, self.LEDGER_INPUT_BUDGET)
            omitted = len(prior_entries) - len(shown)
            note = f" (the {omitted} oldest entries are omitted here)" if omitted else ""
            user_content += f"Ledger So Far{note}:\n\n" + "\n\n".join(shown) + "\n\n---\n\n"
        if prior_state:
            user_content += f"Current Living State:\n\n{prior_state}\n\n---\n\n"
        user_content += (
            f"Source: log_{source_log_number:03d}\n\n"
            f"New Message Exchanges:\n\n{log_text}"
        )

        from kernos.kernel.reasoning import TruncatedResponseError
        try:
            output = await self.reasoning.complete_simple(
                system_prompt=COMPACTION_SYSTEM_PROMPT + f"\n\n{space_definition}",
                user_content=user_content,
                max_tokens=COMPACTION_MAX_OUTPUT_TOKENS,
                prefer_cheap=True,
                allow_truncated=False,
            )
        except TruncatedResponseError as exc:
            raise CompactionOutputError(f"compaction output truncated: {exc}") from exc

        # Parse and strip the trailer sections
        seed_depth = self._parse_seed_depth(output)
        output = self._strip_seed_depth(output)
        comp_state.last_seed_depth = seed_depth
        output = self._strip_fact_harvest(output)  # no longer requested; tolerate it

        recurring_workflows = self._parse_recurring_workflows(output)
        output = self._strip_recurring_workflows(output)
        if recurring_workflows:
            logger.info("COMPACTION_WORKFLOWS: count=%d", len(recurring_workflows))
        comp_state._recurring_workflows = recurring_workflows  # type: ignore[attr-defined]

        commitments = self._parse_follow_ups(output)
        output = self._strip_follow_ups(output)
        if commitments:
            logger.info("COMPACTION_FOLLOW_UPS: count=%d", len(commitments))
        comp_state._follow_ups = commitments  # type: ignore[attr-defined]

        entry_number = comp_state.global_compaction_number + 1
        bullets, living_state = self._parse_compaction_output(output, entry_number)

        first_ts, last_ts = _span_timestamps(log_text)
        header = f"## Compaction #{entry_number} (source: log_{source_log_number:03d})"
        if first_ts:
            header += f" — {first_ts} → {last_ts}"
        entries = prior_entries + [f"{header}\n\n{bullets}"]
        updated_doc = self._render_document(entries, living_state)
        _write_atomic(active_doc_path, updated_doc)

        # Update state
        new_history_tokens = await self.adapter.count_tokens(updated_doc)
        comp_state.history_tokens = new_history_tokens
        comp_state.compaction_number += 1
        comp_state.global_compaction_number += 1
        comp_state.cumulative_new_tokens = 0
        comp_state.last_compaction_at = utc_now()
        comp_state.message_ceiling = self._compute_ceiling(comp_state)

        await self._refresh_archive_story(
            instance_id, space_id, entries, member_id=member_id,
        )

        # Keep a continuous Ledger. Raw logs and all existing Ledger files
        # are searchable directly; no new seals or model-written index.

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

    async def rebuild_missing_entries(
        self,
        instance_id: str,
        space_id: str,
        space: ContextSpace,
        conv_logger: Any,
        member_id: str = "",
    ) -> list[int]:
        """Restore Ledger entries for closed logs that no entry cites.

        The old compaction rewrote the whole document each cycle, and the
        model sometimes dropped earlier entries; their source logs are still
        on disk. Each such log is recompacted with the same prompt as live
        compaction (its seeded head removed, the entries before it as the
        Ledger so far), and only the new entry is kept: it is spliced back in
        log order with "(rebuilt)" in its header. The Living State is left
        as it is. Returns the log numbers rebuilt.
        """
        doc_path = self._space_dir(instance_id, space_id, member_id) / "active_document.md"
        document = await self.load_document(instance_id, space_id, member_id)
        if not document:
            return []
        entries = self._parse_ledger_entries(document)
        living_state = self._extract_living_state(document)
        archived = await self.load_all_ledger_entries(instance_id, space_id, member_id)
        cited = {_entry_source_log(e) for e in archived} - {None}
        info = await conv_logger.get_current_log_info(instance_id, space_id, member_id=member_id)
        from kernos.kernel.conversation_log import strip_seeded_head

        rebuilt: list[int] = []
        for n in range(1, info["log_number"]):
            if n in cited:
                continue
            text = await conv_logger.read_log_text(instance_id, space_id, n, member_id=member_id)
            if not text or not text.strip():
                continue
            previous = (
                await conv_logger.read_log_text(instance_id, space_id, n - 1, member_id=member_id)
                if n > 1 else None
            )
            span = strip_seeded_head(text, previous)
            if not span.strip():
                continue
            before = [e for e in entries if (_entry_source_log(e) or 0) < n]
            shown = self._select_hot_tail(before, self.LEDGER_INPUT_BUDGET)
            user_content = (
                ("Ledger So Far:\n\n" + "\n\n".join(shown) + "\n\n---\n\n" if shown else "")
                + f"Source: log_{n:03d}\n\nNew Message Exchanges:\n\n{span}"
            )
            space_definition = (
                f"Space: {space.name}\nType: {space.space_type}\n"
                f"Description: {space.description}\nPosture: {space.posture}\n"
            )
            from kernos.kernel.reasoning import TruncatedResponseError
            try:
                output = await self.reasoning.complete_simple(
                    system_prompt=COMPACTION_SYSTEM_PROMPT + f"\n\n{space_definition}",
                    user_content=user_content,
                    max_tokens=COMPACTION_MAX_OUTPUT_TOKENS,
                    prefer_cheap=True,
                    allow_truncated=False,
                )
            except TruncatedResponseError as exc:
                raise CompactionOutputError(f"rebuild of log_{n:03d} truncated: {exc}") from exc
            output = self._strip_follow_ups(self._strip_recurring_workflows(
                self._strip_fact_harvest(self._strip_seed_depth(output))))
            bullets, _discarded_state = self._parse_compaction_output(output, n)
            first_ts, last_ts = _span_timestamps(span)
            header = f"## Compaction #{n} (source: log_{n:03d})"
            if first_ts:
                header += f" — {first_ts} → {last_ts}"
            entries.append(f"{header} (rebuilt)\n\n{bullets}")
            entries.sort(key=lambda e: _entry_source_log(e) or 0)
            rebuilt.append(n)
            logger.info(
                "LEDGER_REBUILT: space=%s member=%s log=log_%03d", space_id, member_id, n,
            )
        if rebuilt:
            _write_atomic(doc_path, self._render_document(entries, living_state))
        return rebuilt

    def _parse_compaction_output(self, output: str, entry_number: int) -> tuple[str, str]:
        """Return (new entry bullets, Living State) from the compaction output.

        Tolerates a model that returned the old whole-document shape, but only
        by taking the entry it numbered as this one, never an older entry.
        """
        state_match = re.search(r"^#\s*Living State\s*$\n?(.*)", output, re.DOTALL | re.MULTILINE)
        living_state = state_match.group(1).strip() if state_match else ""
        before_state = output[:state_match.start()] if state_match else output

        bullets = ""
        entry_match = re.search(r"^#\s*New Ledger Entry\s*$\n?(.*)", before_state, re.DOTALL | re.MULTILINE)
        if entry_match:
            bullets = entry_match.group(1).strip()
        else:
            own = re.search(
                rf"^## Compaction #{entry_number}\b[^\n]*\n(.*)", before_state,
                re.DOTALL | re.MULTILINE,
            )
            if own:
                bullets = re.split(r"^## Compaction #\d+", own.group(1), flags=re.MULTILINE)[0].strip()
        if not bullets or not living_state:
            raise CompactionOutputError(
                "compaction output lacks "
                + ("the new Ledger entry" if not bullets else "the Living State")
            )
        return bullets, living_state

    @staticmethod
    def _render_document(entries: list[str], living_state: str) -> str:
        return "# Ledger\n\n" + "\n\n".join(entries) + "\n\n# Living State\n\n" + living_state + "\n"

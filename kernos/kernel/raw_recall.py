"""Lexical recall of complete neighbouring turns from member-owned closed logs."""
from dataclasses import dataclass
from itertools import groupby
import re

from kernos.kernel.conversation_log import ConversationLogger


@dataclass(frozen=True)
class ConversationWindow:
    score: float
    space_id: str
    log_number: int
    first_turn: int
    last_turn: int
    text: str

    @property
    def source(self) -> str:
        return (f"Historical conversation [space: {self.space_id}; "
                f"source: log_{self.log_number:03d}; turns {self.first_turn}-{self.last_turn}]")


async def search_conversations(
    logger: ConversationLogger, instance_id: str, member_id: str,
    scope_chain: list[str], query: str,
) -> list[ConversationWindow]:
    # Import here: RetrievalService owns the shared BM25 implementation.
    from kernos.kernel.retrieval import bm25_rank

    groups = []
    docs, coordinates = [], []
    for space in scope_chain:
        turns = await logger.read_historical_turns(instance_id, space, member_id)
        for log_number, group in groupby(turns, key=lambda turn: turn.log_number):
            entries = list(group)
            group_number = len(groups)
            groups.append((space, log_number, entries))
            for i in range(len(entries)):
                docs.append("\n".join(t.content for t in entries[max(0, i - 1):i + 2]))
                coordinates.append((group_number, i))
    # All logs share document frequencies and length normalization. Scores
    # from independent per-log corpora cannot be compared for a global top K.
    ranked_by_group: dict[int, list] = {}
    for doc_number, score in bm25_rank(query, docs):
        group_number, i = coordinates[doc_number]
        entries = groups[group_number][2]
        ranked_by_group.setdefault(group_number, []).append(
            (max(0, i - 1), min(len(entries), i + 2), score))
    windows = []
    for group_number, spans in ranked_by_group.items():
        space, log_number, entries = groups[group_number]
        merged: list[tuple[int, int, float]] = []
        for start, end, score in sorted(spans):
            if merged and start < merged[-1][1]:
                old_start, old_end, old_score = merged[-1]
                merged[-1] = (old_start, max(old_end, end), max(old_score, score))
            else:
                merged.append((start, end, score))
        for start, end, score in merged:
            selected = entries[start:end]
            text = "\n\n".join(
                f"[turn {turn.turn_number}; {turn.timestamp}; {turn.speaker}]\n{turn.content}"
                for turn in selected
            )
            windows.append(ConversationWindow(score, space, log_number,
                                               selected[0].turn_number,
                                               selected[-1].turn_number, text))
    windows.sort(key=lambda window: window.score, reverse=True)
    return windows[:3]


def format_window(window: ConversationWindow, query: str, max_chars: int) -> str:
    """Return the whole window when it fits; otherwise a marked match-centred excerpt."""
    footer = (f"\nFor the full conversation: remember_details(source_ref=\"log_{window.log_number:03d}\", "
              f"space=\"{window.space_id}\") with a query for the detail.")
    header = window.source + " (historical reference):\n"
    available = max_chars - len(header) - len(footer)
    if available < 120:
        return ""
    return header + excerpt_text(window.text, query, available) + footer


def excerpt_text(text: str, query: str, max_chars: int) -> str:
    """Explicitly excerpt long evidence around matches, including a cut turn's speaker."""
    body = text
    available = max_chars
    if len(body) > available:
        marker = "\n[... omitted ...]\n"
        available -= 2 * len(marker)
        terms = re.findall(r"[a-z0-9]+", query.lower())
        from kernos.kernel.retrieval import _BM25_STOPWORDS
        terms = [term for term in terms if term not in _BM25_STOPWORDS]
        matches = [match for term in terms for match in re.finditer(
            r"\b" + re.escape(term) + r"\b", body, re.IGNORECASE)]
        # Keep the densest matching stretch, rather than clipping the head.
        starts = [max(0, min(len(body) - available, m.start() - available // 3))
                  for m in matches] or [0]
        start = max(starts, key=lambda pos: sum(pos <= m.start() < pos + available
                                               for m in matches))
        # A cut inside a long turn must still identify who said it and when.
        preceding = list(re.finditer(r"(?m)^\[turn [^\n]+\]\n", body[:start]))
        context = preceding[-1].group() if preceding else ""
        available -= len(context)
        end = start + available
        body = (marker if start else "") + context + body[start:end] + (marker if end < len(body) else "")
    return body

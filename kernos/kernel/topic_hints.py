"""Topic-hint ledger — recurrence receipts for domain-space formation.

The router tags messages with snake_case topic hints for emerging topics
("dnd_campaign", "kitchen_reno"). Before this ledger those hints were
recorded per-message and never aggregated, so the domain assessor had to
infer "likely recurrence" from a single compaction-document snapshot.

The ledger keeps two small per-space records:

- **hints** — per-hint counters (count, first_seen, last_seen) recorded
  best-effort at routing time.
- **near_misses** — medium-confidence domain-assessment verdicts, so
  repeated almost-domains escalate to a user suggestion instead of being
  silently re-judged from zero each compaction.

Storage: ``data/{instance}/state/topic_hints.json`` — atomic writes
(tempfile + os.replace), same convention as ``triggers.json``. Entries
are pruned by recency cap, never grown unbounded. This is operational
housekeeping data, not user content — pruning is not a shadow-archive
violation.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path

from kernos.utils import utc_now

logger = logging.getLogger(__name__)

# Per-space cap on tracked hints; least-recently-seen pruned first.
_MAX_HINTS_PER_SPACE = 50
# Near-miss verdicts at/above this count trigger a user suggestion.
NEAR_MISS_SUGGESTION_THRESHOLD = 2


# A topic hint is strict snake_case prose ("dnd_campaign", "kitchen_reno").
_HINT_SHAPE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
# Internal-identifier prefixes that must never be counted as topics, even
# if a model emits one as a tag (router tags are mixed: space IDs + hints).
_RESERVED_PREFIXES = ("space_", "mem_", "wsp_", "trig_", "sp_", "canvas_", "wf_")


def extract_hint_tags(tags: list[str], known_space_ids: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """Filter router tags down to genuine emerging-topic hints.

    Subtracts known space IDs, rejects reserved internal-ID prefixes and
    anything that isn't strict snake_case. Order-preserving, deduplicated.
    """
    out: list[str] = []
    seen: set[str] = set()
    for t in tags or []:
        if not t or t in known_space_ids:
            continue
        low = t.strip().lower()
        if low.startswith("_") or any(low.startswith(p) for p in _RESERVED_PREFIXES):
            continue
        if not _HINT_SHAPE.match(low) or low in seen:
            continue
        seen.add(low)
        out.append(low)
    return out


def normalize_hint(hint: str) -> str:
    """Canonicalize a router topic hint for counting.

    Lowercase, whitespace/hyphens to underscores, strip non-word chars —
    so "DnD Campaign", "dnd-campaign" and "dnd_campaign" count together.
    Returns "" for inputs that normalize to nothing.
    """
    cleaned = re.sub(r"[\s\-]+", "_", (hint or "").strip().lower())
    cleaned = re.sub(r"[^\w]", "", cleaned)
    return cleaned.strip("_")


class TopicHintLedger:
    """Per-instance recurrence ledger for emerging-topic hints."""

    def __init__(self, data_dir: str | Path) -> None:
        from kernos.utils import _safe_name
        self._data_dir = Path(data_dir)
        self._safe_name = _safe_name

    def _path(self, instance_id: str) -> Path:
        return self._data_dir / self._safe_name(instance_id) / "state" / "topic_hints.json"

    def _read(self, instance_id: str) -> dict:
        path = self._path(instance_id)
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("TopicHintLedger: read failed for %s: %s", instance_id, exc)
            return {}

    def _write(self, instance_id: str, data: dict) -> None:
        path = self._path(instance_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, str(path))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _space_bucket(self, data: dict, space_id: str) -> dict:
        bucket = data.setdefault(space_id, {})
        bucket.setdefault("hints", {})
        bucket.setdefault("near_misses", {})
        return bucket

    # ------------------------------------------------------------------
    # Hints (recorded at routing time)
    # ------------------------------------------------------------------

    def record_hints(self, instance_id: str, space_id: str, hints: list[str]) -> None:
        """Count router topic hints against a space. Best-effort by design."""
        normalized = [h for h in (normalize_hint(x) for x in hints) if h]
        if not normalized or not space_id:
            return
        data = self._read(instance_id)
        bucket = self._space_bucket(data, space_id)
        now = utc_now()
        for hint in normalized:
            entry = bucket["hints"].setdefault(
                hint, {"count": 0, "first_seen": now, "last_seen": now}
            )
            entry["count"] += 1
            entry["last_seen"] = now
        # Recency cap: keep the most recently seen hints.
        if len(bucket["hints"]) > _MAX_HINTS_PER_SPACE:
            keep = sorted(
                bucket["hints"].items(), key=lambda kv: kv[1].get("last_seen", ""), reverse=True,
            )[:_MAX_HINTS_PER_SPACE]
            bucket["hints"] = dict(keep)
        self._write(instance_id, data)

    def top_hints(self, instance_id: str, space_id: str, limit: int = 5) -> list[dict]:
        """Most-recurrent hints for a space, count-descending.

        Each item: {"hint", "count", "first_seen", "last_seen"}.
        """
        data = self._read(instance_id)
        hints = data.get(space_id, {}).get("hints", {})
        ranked = sorted(hints.items(), key=lambda kv: kv[1].get("count", 0), reverse=True)
        return [
            {"hint": name, **fields} for name, fields in ranked[:limit]
        ]

    # ------------------------------------------------------------------
    # Near-misses (recorded at assessment time)
    # ------------------------------------------------------------------

    def record_near_miss(self, instance_id: str, space_id: str, domain_name: str) -> int:
        """Record a medium-confidence domain verdict. Returns the new count."""
        key = normalize_hint(domain_name)
        if not key or not space_id:
            return 0
        data = self._read(instance_id)
        bucket = self._space_bucket(data, space_id)
        now = utc_now()
        entry = bucket["near_misses"].setdefault(
            key, {"name": domain_name, "count": 0, "first_seen": now, "last_seen": now,
                  "suggested_at": ""},
        )
        entry["count"] += 1
        entry["last_seen"] = now
        self._write(instance_id, data)
        return int(entry["count"])

    def should_suggest(self, instance_id: str, space_id: str, domain_name: str) -> bool:
        """True when a near-miss has recurred enough and wasn't suggested yet."""
        key = normalize_hint(domain_name)
        data = self._read(instance_id)
        entry = data.get(space_id, {}).get("near_misses", {}).get(key)
        if not entry:
            return False
        return (
            int(entry.get("count", 0)) >= NEAR_MISS_SUGGESTION_THRESHOLD
            and not entry.get("suggested_at")
        )

    def mark_suggested(self, instance_id: str, space_id: str, domain_name: str) -> None:
        """Record that the user was asked about this near-miss (anti-nag)."""
        key = normalize_hint(domain_name)
        data = self._read(instance_id)
        entry = data.get(space_id, {}).get("near_misses", {}).get(key)
        if entry is not None:
            entry["suggested_at"] = utc_now()
            self._write(instance_id, data)

    def clear_near_miss(self, instance_id: str, space_id: str, domain_name: str) -> None:
        """Drop a near-miss record (called when the domain is actually created)."""
        key = normalize_hint(domain_name)
        data = self._read(instance_id)
        near = data.get(space_id, {}).get("near_misses", {})
        if key in near:
            del near[key]
            self._write(instance_id, data)

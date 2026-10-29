"""Per-space, per-member conversation log files.

Each member gets their own conversation thread per space, stored at:
  {data_dir}/tenants/{instance_id}/spaces/{space_id}/members/{member_id}/logs/

Legacy (pre-multi-member) logs live at:
  {data_dir}/tenants/{instance_id}/spaces/{space_id}/logs/

Member-scoped reads and writes use only the member's own path. They never
fall back to the shared legacy folder: its author is unknown, and reading it
is how one member's history reached another (the disclosure gate removed the
same fallback from compaction). Unscoped (member_id="") calls use the legacy
path itself.

Concurrency: per-(space, member) asyncio Lock serializes writes.
Reads are eventually consistent (no lock). Single-process only.
"""
import asyncio
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiofiles

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HistoricalTurn:
    """A turn's original coordinates, unchanged when seeded copies are skipped."""

    log_number: int
    turn_number: int
    speaker: str
    timestamp: str
    content: str
    space_id: str = ""
    member_id: str = ""

# Pattern matching the start of a log entry: [timestamp] [speaker] [channel]
_ENTRY_RE = re.compile(
    r'^\[(\d{4}-\d{2}-\d{2}T[^\]]+)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+'
)


def _parse_entries(text: str) -> list[dict]:
    """Parse log text into structured entries, handling multiline content.

    Each entry starts with [timestamp] [speaker] [channel] on a new line.
    Everything until the next entry header is the content (including real newlines).
    """
    lines = text.split("\n")
    entries: list[dict] = []
    current: dict | None = None

    for line in lines:
        match = _ENTRY_RE.match(line)
        if match:
            if current:
                entries.append(current)
            timestamp, speaker, channel = match.groups()
            content_start = match.end()
            role = "user" if speaker == "user" else "assistant"
            current = {
                "role": role,
                "speaker": speaker,
                "content": line[content_start:],
                "timestamp": timestamp,
                "channel": channel,
            }
        elif current:
            # Continuation line — append to current entry's content
            current["content"] += "\n" + line

    if current:
        entries.append(current)

    # Strip trailing whitespace from content
    for entry in entries:
        entry["content"] = entry["content"].rstrip()

    return entries


def strip_seeded_head(text: str, previous_text: str | None) -> str:
    """Drop the entries at the head of a log that were seeded from the
    previous log. Logs written before meta recorded ``seeded_chars`` carry
    no marker, so seeds are recognised as the longest run of head entries
    equal (timestamp + content) to the previous log's tail."""
    if not previous_text:
        return text
    tail = [(e["timestamp"], e["content"]) for e in _parse_entries(previous_text)[-25:]]
    head = [(e["timestamp"], e["content"]) for e in _parse_entries(text)]
    seeded = 0
    for k in range(min(len(tail), len(head)), 0, -1):
        if head[:k] == tail[-k:]:
            seeded = k
            break
    if not seeded:
        return text
    starts = [m.start() for m in re.finditer(_ENTRY_RE.pattern, text, re.MULTILINE)]
    return text[starts[seeded]:] if seeded < len(starts) else ""


class ConversationLogger:
    """Per-space, per-member conversation log files.

    P1: Writes user/assistant turns to numbered plain-text log files.
    P2: Reads recent entries with token-budgeted windowing for context assembly.
    """

    def __init__(self, data_dir: str = "./data") -> None:
        self._data_dir = Path(data_dir)
        self._meta_locks: dict[str, asyncio.Lock] = {}
        self._history_cache: dict[tuple[str, str, str, bool], tuple[tuple, tuple[HistoricalTurn, ...]]] = {}

    async def list_member_space_ids(self, instance_id: str, member_id: str) -> list[str]:
        """List spaces with this member's own logs folder, including empty ones."""
        if not member_id:
            return []
        return await asyncio.to_thread(self._list_member_space_ids_sync, instance_id, member_id)

    def _list_member_space_ids_sync(self, instance_id: str, member_id: str) -> list[str]:
        spaces = self._data_dir / "tenants" / instance_id / "spaces"
        if not spaces.is_dir():
            return []
        return sorted(space.name for space in spaces.iterdir()
                      if space.is_dir() and (space / "members" / member_id / "logs").is_dir())

    async def read_source_turns(
        self, instance_id: str, space_id: str, member_id: str, *, include_current: bool = True,
    ) -> tuple[HistoricalTurn, ...]:
        """Read this member's source history for compaction and migration.

        Unlike recall, source validation normally needs the current log too.
        Seeded copies are skipped without renumbering physical source turns.
        No member means no source history; shared legacy logs are never used.
        """
        if not member_id:
            return ()
        return await asyncio.to_thread(
            self._read_source_turns_sync, instance_id, space_id, member_id,
            include_current=include_current,
        )

    async def read_historical_turns(
        self, instance_id: str, space_id: str, member_id: str,
    ) -> tuple[HistoricalTurn, ...]:
        """Read only this member's closed logs, without shared-folder fallback.

        The current log is already in turn context and contains the recall
        question itself. Missing metadata conservatively treats the newest
        log as current. Disk remains truth for the parsed cache.
        """
        if not member_id:
            return ()
        return await asyncio.to_thread(
            self._read_historical_turns_sync, instance_id, space_id, member_id,
        )

    def _read_historical_turns_sync(
        self, instance_id: str, space_id: str, member_id: str,
    ) -> tuple[HistoricalTurn, ...]:
        return self._read_source_turns_sync(
            instance_id, space_id, member_id, include_current=False,
        )

    def _read_source_turns_sync(
        self, instance_id: str, space_id: str, member_id: str, *, include_current: bool,
    ) -> tuple[HistoricalTurn, ...]:
        folder = self._logs_dir(instance_id, space_id, member_id)
        files = sorted((int(p.stem[4:]), p) for p in folder.glob("log_*.txt")
                       if re.fullmatch(r"log_\d+", p.stem))
        if not files:
            return ()
        meta_path = folder / "meta.json"
        current = files[-1][0]
        if meta_path.exists():
            current = int(json.loads(meta_path.read_text(encoding="utf-8"))["current_log"])
        selected = files if include_current else [(number, path) for number, path in files if number != current]
        # Fingerprint all files: even an excluded predecessor can change which
        # head turns are recognised as seeds. ctime/inode also detect an edit
        # or replacement that preserves the old length and modification time.
        source_stats = [(number, path.stat()) for number, path in files]
        fingerprint = (current, tuple((n, st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino)
                                      for n, st in source_stats))
        key = (instance_id, space_id, member_id, include_current)
        cached = self._history_cache.get(key)
        if cached and cached[0] == fingerprint:
            return cached[1]
        turns = []
        paths = dict(files)
        previous_text, previous_number = None, None
        for number, path in selected:
            text = path.read_text(encoding="utf-8")
            parsed = _parse_entries(text)
            if previous_number != number - 1:
                predecessor = paths.get(number - 1)
                previous_text = predecessor.read_text(encoding="utf-8") if predecessor else None
            unseeded = _parse_entries(strip_seeded_head(
                text, previous_text,
            ))
            offset = len(parsed) - len(unseeded)
            turns.extend(HistoricalTurn(number, offset + i + 1, entry["speaker"],
                                        entry["timestamp"], entry["content"],
                                        space_id, member_id)
                         for i, entry in enumerate(unseeded))
            previous_text, previous_number = text, number
        result = tuple(turns)
        self._history_cache[key] = (fingerprint, result)
        return result

    def _get_lock(self, instance_id: str, space_id: str, member_id: str = "") -> asyncio.Lock:
        """Per-(space, member) asyncio lock for serializing meta read-modify-write."""
        key = f"{instance_id}:{space_id}:{member_id}"
        if key not in self._meta_locks:
            self._meta_locks[key] = asyncio.Lock()
        return self._meta_locks[key]

    def _logs_dir(self, instance_id: str, space_id: str, member_id: str = "") -> Path:
        base = self._data_dir / "tenants" / instance_id / "spaces" / space_id
        if member_id:
            return base / "members" / member_id / "logs"
        return base / "logs"

    def _legacy_logs_dir(self, instance_id: str, space_id: str) -> Path:
        """Legacy path (pre-multi-member) for lazy migration reads."""
        return self._data_dir / "tenants" / instance_id / "spaces" / space_id / "logs"

    def _meta_path(self, instance_id: str, space_id: str, member_id: str = "") -> Path:
        return self._logs_dir(instance_id, space_id, member_id) / "meta.json"

    def _load_meta(self, instance_id: str, space_id: str, member_id: str = "") -> dict:
        return self._load_meta_sync(instance_id, space_id, member_id)

    async def _load_meta_async(self, instance_id: str, space_id: str, member_id: str = "") -> dict:
        return await asyncio.get_running_loop().run_in_executor(
            None, self._load_meta_sync, instance_id, space_id, member_id,
        )

    def _load_meta_sync(self, instance_id: str, space_id: str, member_id: str = "") -> dict:
        path = self._meta_path(instance_id, space_id, member_id)
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {
            "current_log": 1,
            "current_log_tokens_est": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def _save_meta(self, instance_id: str, space_id: str, meta: dict, member_id: str = "") -> None:
        self._save_meta_sync(instance_id, space_id, meta, member_id)

    async def _save_meta_async(self, instance_id: str, space_id: str, meta: dict, member_id: str = "") -> None:
        await asyncio.get_running_loop().run_in_executor(
            None, self._save_meta_sync, instance_id, space_id, meta, member_id,
        )

    def _save_meta_sync(self, instance_id: str, space_id: str, meta: dict, member_id: str = "") -> None:
        """Atomic write: tempfile + os.replace (POSIX atomic). Always writes to member-scoped path."""
        path = self._meta_path(instance_id, space_id, member_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
            os.replace(tmp, str(path))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    async def _current_log_path(self, instance_id: str, space_id: str, member_id: str = "") -> Path:
        meta = await self._load_meta_async(instance_id, space_id, member_id)
        num = meta["current_log"]
        member_dir = self._logs_dir(instance_id, space_id, member_id)
        member_path = member_dir / f"log_{num:03d}.txt"
        if member_path.exists() or member_id:
            return member_path
        # Lazy migration: fall back to legacy path for reads
        legacy_path = self._legacy_logs_dir(instance_id, space_id) / f"log_{num:03d}.txt"
        if legacy_path.exists():
            return legacy_path
        return member_path  # New writes go to member-scoped path

    async def append(
        self,
        instance_id: str,
        space_id: str,
        speaker: str,       # "user" or "assistant"
        channel: str,        # "discord", "sms", "scheduled", "whisper", "system"
        content: str,
        timestamp: str = "",  # ISO 8601, defaults to now
        member_id: str = "",
    ) -> None:
        """Append an entry to the current log file for this space+member.

        Content is written with real newlines — no escaping.
        Each entry starts with a [timestamp] [speaker] [channel] header line.
        """
        if not space_id:
            return

        try:
            async with self._get_lock(instance_id, space_id, member_id):
                # Always write to member-scoped path
                logs_dir = self._logs_dir(instance_id, space_id, member_id)
                logs_dir.mkdir(parents=True, exist_ok=True)

                ts = timestamp or datetime.now(timezone.utc).isoformat()
                line = f"[{ts}] [{speaker}] [{channel}] {content}\n"

                meta = await self._load_meta_async(instance_id, space_id, member_id)
                num = meta["current_log"]
                log_path = logs_dir / f"log_{num:03d}.txt"
                async with aiofiles.open(log_path, "a", encoding="utf-8") as f:
                    await f.write(line)

                # Update estimated token count (rough: 1 token ≈ 4 chars)
                meta["current_log_tokens_est"] += len(line) // 4
                await self._save_meta_async(instance_id, space_id, meta, member_id)

                logger.info(
                    "CONV_LOG: space=%s member=%s log=%03d speaker=%s channel=%s len=%d",
                    space_id, member_id or "legacy", meta["current_log"], speaker, channel, len(content),
                )
        except Exception as exc:
            # Never break the user's message flow for logging failures
            logger.warning("CONV_LOG: failed to write: %s", exc)

    # --- P2: Read ---

    async def read_recent(
        self,
        instance_id: str,
        space_id: str,
        token_budget: int = 4000,
        max_messages: int = 50,
        member_id: str = "",
    ) -> list[dict]:
        """Read recent conversation entries from the current space+member log.

        Walks backward from the tail of parsed entries until either
        token_budget or max_messages is exhausted. Returns entries in
        chronological order (oldest first).

        Returns list of dicts: {role, content, timestamp, channel}.
        """
        if not space_id:
            return []

        log_path = await self._current_log_path(instance_id, space_id, member_id)
        if not log_path.exists():
            return []

        async with aiofiles.open(log_path, "r", encoding="utf-8") as f:
            text = await f.read()
        all_entries = _parse_entries(text)

        result: list[dict] = []
        tokens_used = 0

        for entry in reversed(all_entries):
            entry_tokens = len(entry["content"]) // 4 + 10  # content + header overhead
            if tokens_used + entry_tokens > token_budget and result:
                break
            tokens_used += entry_tokens
            result.append(entry)
            if len(result) >= max_messages:
                break

        result.reverse()
        return result

    # --- P3: Compaction support ---

    async def get_current_log_info(
        self, instance_id: str, space_id: str, member_id: str = "",
    ) -> dict:
        """Get metadata about the current log.

        Returns: {"log_number": N, "tokens_est": N, "path": Path, "exists": bool}
        """
        meta = await self._load_meta_async(instance_id, space_id, member_id)
        log_path = self._logs_dir(instance_id, space_id, member_id) / f"log_{meta['current_log']:03d}.txt"
        return {
            "log_number": meta["current_log"],
            "tokens_est": meta.get("current_log_tokens_est", 0),
            "seeded_tokens_est": meta.get("seeded_tokens_est", 0),
            # Characters at the head of the log copied from the previous
            # log (already compacted), and the prefix already fact-harvested.
            "seeded_chars": meta.get("seeded_chars", 0),
            "harvested_chars": meta.get("harvested_chars", 0),
            "path": log_path,
            "exists": log_path.exists(),
        }

    async def read_current_log_text(
        self, instance_id: str, space_id: str, member_id: str = "",
    ) -> tuple[str, int]:
        """Read the full text of the current log file.

        Returns: (log_text, log_number)
        Raises FileNotFoundError if no log exists.
        """
        meta = await self._load_meta_async(instance_id, space_id, member_id)
        log_num = meta["current_log"]
        log_path = self._logs_dir(instance_id, space_id, member_id) / f"log_{log_num:03d}.txt"
        if not log_path.exists():
            raise FileNotFoundError(f"No log file at {log_path}")
        async with aiofiles.open(log_path, "r", encoding="utf-8") as f:
            text = await f.read()
        return text, log_num

    async def roll_log(
        self, instance_id: str, space_id: str, member_id: str = "",
    ) -> tuple[int, int]:
        """Close current log for appends and start a new one.

        Returns: (old_log_number, new_log_number)
        """
        async with self._get_lock(instance_id, space_id, member_id):
            meta = await self._load_meta_async(instance_id, space_id, member_id)
            old_num = meta["current_log"]
            new_num = old_num + 1

            meta["current_log"] = new_num
            meta["current_log_tokens_est"] = 0
            meta["seeded_tokens_est"] = 0
            meta["seeded_chars"] = 0
            meta["harvested_chars"] = 0
            meta["created_at"] = datetime.now(timezone.utc).isoformat()
            await self._save_meta_async(instance_id, space_id, meta, member_id)

            logger.info(
                "LOG_ROLL: space=%s member=%s closed=log_%03d starting=log_%03d",
                space_id, member_id or "legacy", old_num, new_num,
            )
            return old_num, new_num

    async def seed_from_previous(
        self, instance_id: str, space_id: str,
        previous_log_number: int, tail_entries: int = 10,
        member_id: str = "",
    ) -> int:
        """Copy last N entries from archived log into new current log.

        Preserves recent context across compaction boundaries so the agent
        doesn't lose track of the conversation.

        Returns number of entries seeded. Updates meta.json token estimate.
        """
        async with self._get_lock(instance_id, space_id, member_id):
            return await self._seed_from_previous_locked(
                instance_id, space_id, previous_log_number, tail_entries, member_id,
            )

    async def _seed_from_previous_locked(
        self, instance_id: str, space_id: str,
        previous_log_number: int, tail_entries: int,
        member_id: str = "",
    ) -> int:
        """Internal seed implementation — must be called under lock."""
        prev_path = self._logs_dir(instance_id, space_id, member_id) / f"log_{previous_log_number:03d}.txt"
        if not prev_path.exists():
            return 0

        async with aiofiles.open(prev_path, "r", encoding="utf-8") as f:
            text = await f.read()
        all_entries = _parse_entries(text)
        if not all_entries:
            return 0

        seed_entries = all_entries[-tail_entries:]

        # Always write to member-scoped path
        current_path_dir = self._logs_dir(instance_id, space_id, member_id)
        current_path_dir.mkdir(parents=True, exist_ok=True)
        meta = await self._load_meta_async(instance_id, space_id, member_id)
        current_path = current_path_dir / f"log_{meta['current_log']:03d}.txt"

        seed_text = ""
        for entry in seed_entries:
            speaker = entry.get("speaker") or entry["role"]
            line = f"[{entry['timestamp']}] [{speaker}] [{entry['channel']}] {entry['content']}\n"
            seed_text += line

        prior_chars = 0
        if current_path.exists():
            async with aiofiles.open(current_path, "r", encoding="utf-8") as f:
                prior_chars = len(await f.read())
        async with aiofiles.open(current_path, "a", encoding="utf-8") as f:
            await f.write(seed_text)

        # Update token estimate — track seeded tokens separately
        seed_tokens = len(seed_text) // 4
        meta["current_log_tokens_est"] += seed_tokens
        meta["seeded_tokens_est"] = meta.get("seeded_tokens_est", 0) + seed_tokens
        meta["seeded_chars"] = prior_chars + len(seed_text)
        await self._save_meta_async(instance_id, space_id, meta, member_id)

        logger.info(
            "LOG_SEED: space=%s member=%s from=log_%03d entries=%d tokens_est=%d",
            space_id, member_id or "legacy", previous_log_number, len(seed_entries), len(seed_text) // 4,
        )
        return len(seed_entries)

    async def mark_harvested(
        self, instance_id: str, space_id: str, log_number: int, chars: int,
        member_id: str = "",
    ) -> None:
        """Record that the first ``chars`` characters of the current log have
        been fact-harvested. A no-op if the log has rolled since."""
        async with self._get_lock(instance_id, space_id, member_id):
            meta = await self._load_meta_async(instance_id, space_id, member_id)
            if meta.get("current_log") != log_number:
                return
            meta["harvested_chars"] = max(meta.get("harvested_chars", 0), chars)
            await self._save_meta_async(instance_id, space_id, meta, member_id)

    async def read_log_text(
        self, instance_id: str, space_id: str, log_number: int,
        member_id: str = "",
    ) -> str | None:
        """Read the full text of an archived or current log file.

        Returns the log text, or None if the file doesn't exist.
        Public API — used by remember_details handler.
        """
        log_path = self._logs_dir(instance_id, space_id, member_id) / f"log_{log_number:03d}.txt"
        if not log_path.exists():
            return None
        async with aiofiles.open(log_path, "r", encoding="utf-8") as f:
            return await f.read()

"""Separate storage for embedding vectors.

Stored at {data_dir}/{instance_id}/state/embeddings.json
Maps entry_id → list[float].

Kept separate from knowledge.json to avoid bloating it with large float arrays.
"""
import json
import logging
from pathlib import Path

from filelock import FileLock

from kernos.utils import _safe_name

logger = logging.getLogger(__name__)


class JsonEmbeddingStore:
    """JSON-on-disk store for embedding vectors, partitioned by instance."""

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)
        # path -> ((mtime_ns, size), data): a search reads one vector per
        # fact, so re-parsing the whole file each time made recall O(n²).
        self._cache: dict[Path, tuple[tuple[int, int], dict[str, list[float]]]] = {}

    def _path(self, instance_id: str) -> Path:
        return self._data_dir / _safe_name(instance_id) / "state" / "embeddings.json"

    def _read(self, instance_id: str) -> dict[str, list[float]]:
        path = self._path(instance_id)
        try:
            st = path.stat()
        except FileNotFoundError:
            return {}
        stamp = (st.st_mtime_ns, st.st_size)
        cached = self._cache.get(path)
        if cached and cached[0] == stamp:
            return dict(cached[1])
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self._cache[path] = (stamp, data)
        return dict(data)

    def _write(self, instance_id: str, data: dict[str, list[float]]) -> None:
        path = self._path(instance_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = str(path) + ".lock"
        with FileLock(lock_path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)

    async def save(self, instance_id: str, entry_id: str, embedding: list[float]) -> None:
        """Store an embedding vector for the given entry ID."""
        data = self._read(instance_id)
        data[entry_id] = embedding
        self._write(instance_id, data)

    async def get(self, instance_id: str, entry_id: str) -> list[float] | None:
        """Retrieve embedding for entry_id, or None if not found."""
        data = self._read(instance_id)
        return data.get(entry_id)

    async def get_batch(
        self, instance_id: str, entry_ids: list[str]
    ) -> dict[str, list[float]]:
        """Retrieve embeddings for multiple entry IDs in one read.

        Returns dict mapping entry_id → embedding (only present entries included).
        """
        data = self._read(instance_id)
        return {eid: data[eid] for eid in entry_ids if eid in data}

    async def delete(self, instance_id: str, entry_id: str) -> None:
        """Remove an embedding entry (non-destructive: just removes from index)."""
        data = self._read(instance_id)
        if entry_id in data:
            del data[entry_id]
            self._write(instance_id, data)

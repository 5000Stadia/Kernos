"""TEST-TIME-COUPLING-V1 — one base instant for both ISO strings and mtimes.

`verify_and_archive` compares a report's filesystem ``st_mtime`` against a
pending timestamp parsed from an ISO string. A fixture that hard-codes the ISO
side while letting the filesystem supply the mtime side has **two clocks**, and
the assertion then depends on how far the host clock has drifted from the
fixture date.

That is not hypothetical: `test_verify_archives_quiet_resolved` passed when
written and began failing once wall-clock time moved 62 days past its fixture,
because the report it calls "quiet since" acquired an mtime *after* the pending
marker and was reclassified as a recurrence.

`LogicalTimeline` derives both sides from one base, so a fixture's timeline is
internally consistent regardless of when the suite runs.

Note the mtime does NOT have to be a real wall-clock instant — the production
comparison is purely relative, so a base far in the past or future is a valid
and useful test input.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class LogicalTimeline:
    """A single base instant that yields both ISO strings and file mtimes."""

    base: datetime

    @classmethod
    def from_iso(cls, iso: str) -> "LogicalTimeline":
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return cls(base=dt)

    def at(self, *, hours: float = 0.0, days: float = 0.0) -> datetime:
        return self.base + timedelta(hours=hours, days=days)

    def iso(self, *, hours: float = 0.0, days: float = 0.0) -> str:
        """ISO string at an offset from the base — for `now_iso` arguments."""
        return self.at(hours=hours, days=days).isoformat()

    def epoch(self, *, hours: float = 0.0, days: float = 0.0) -> float:
        return self.at(hours=hours, days=days).timestamp()

    def stamp(self, path, *, hours: float = 0.0, days: float = 0.0) -> Path:
        """Bind a file's mtime to this timeline.

        Every report participating in a verification assertion must get its
        mtime from here rather than from the filesystem default, or the fixture
        silently reacquires the host clock as a second time source.
        """
        p = Path(path)
        when = self.epoch(hours=hours, days=days)
        os.utime(p, (when, when))
        return p

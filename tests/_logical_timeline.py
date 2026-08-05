"""TEST-TIME-COUPLING-V1 — one base instant for both ISO strings and mtimes.

`verify_and_archive` compares a report's filesystem ``st_mtime`` against a
pending timestamp parsed from an ISO string. A fixture that hard-codes the ISO
side while letting the filesystem supply the mtime side has **two clocks**, and
the assertion then depends on how far the host clock has drifted from the
fixture date.

Not hypothetical: `test_verify_archives_quiet_resolved` passed when written and
began failing once wall-clock time moved 62 days past its fixture, because the
report it calls "quiet since" acquired an mtime *after* the pending marker and
was reclassified as a recurrence.

`LogicalTimeline` derives both sides from one base, and — critically — **records
what it stamped** so the domain wrapper below can verify at runtime that no
report reached the production call with a host-clock mtime.

Runtime enforcement rather than static analysis is deliberate. An earlier
AST-based guard tried to prove this from syntax and was bypassed by a stamp
followed by an `os.utime` overwrite, by direct `Path.write_text` creation, and
by re-assigning a stamped variable from a factory. Inspecting the actual files
immediately before the call cannot be fooled by any of those, because it reads
the state the production code is about to read.

Note the mtime need not be a real wall-clock instant — the production comparison
is purely relative, so a base far in the past or future is a valid input.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


class LogicalTimeline:
    """A single base instant yielding both ISO strings and file mtimes."""

    def __init__(self, base: datetime) -> None:
        self.base = base
        #: resolved path -> the mtime this timeline set. Used to prove, at call
        #: time, that nothing re-acquired the host clock afterwards.
        self._stamped: dict[Path, float] = {}

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
        """Bind a file's mtime to this timeline and remember that we did."""
        p = Path(path).resolve()
        when = self.epoch(hours=hours, days=days)
        os.utime(p, (when, when))
        self._stamped[p] = when
        return p

    # -- runtime boundary ----------------------------------------------------

    def unbound_reports(self, data_dir) -> list:
        """Reports that would reach the production call with a foreign mtime.

        Catches all three bypasses the static guard missed:
          * never stamped at all (direct creation, or an unknown factory);
          * stamped and then overwritten (`os.utime`, or rewritten content);
          * a stamped *name* re-assigned to a freshly created file.

        It compares live filesystem state, so how the file came to exist is
        irrelevant — only whether its mtime belongs to this timeline.
        """
        fdir = Path(data_dir) / "diagnostics" / "friction"
        if not fdir.is_dir():
            return []
        offenders = []
        for p in sorted(fdir.glob("*.md")):
            rp = p.resolve()
            expected = self._stamped.get(rp)
            actual = rp.stat().st_mtime
            if expected is None:
                offenders.append(f"{p.name}: never stamped (host-clock mtime)")
            elif abs(actual - expected) > 1e-6:
                offenders.append(
                    f"{p.name}: mtime changed after stamping "
                    f"(expected {expected}, found {actual})")
        return offenders

    def assert_reports_bound(self, data_dir) -> None:
        offenders = self.unbound_reports(data_dir)
        assert not offenders, (
            "verification fixture has a second clock — every report reaching "
            "the production call must take its mtime from this timeline: "
            f"{offenders}")


def verify_in_domain(timeline: LogicalTimeline, data_dir, *, now_iso: str):
    """Call `verify_and_archive` through the enforced boundary.

    Domain tests must use this rather than calling production directly, so the
    binding is checked against real files at the moment it matters. The C1
    audit rejects raw `verify_and_archive` use inside the audited module.
    """
    from kernos.kernel import friction_response as fr

    timeline.assert_reports_bound(data_dir)
    return fr.verify_and_archive(data_dir, now_iso=now_iso)

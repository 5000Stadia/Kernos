"""Shared utilities for the KERNOS kernel and persistence layers."""
from datetime import datetime, timezone


def utc_now() -> str:
    """Canonical UTC timestamp. All internal timestamps use this."""
    return datetime.now(timezone.utc).isoformat()


def resolve_system_iana_tz() -> str:
    """Best-effort IANA name for the host's local timezone (e.g.
    "America/Los_Angeles"), or "" if it can't be determined.

    ``str(datetime.now().astimezone().tzinfo)`` only yields an abbreviation
    like "PDT", which ZoneInfo rejects and IANA-format checks discard — so
    timezone discovery silently failed. Resolve the real zone from the OS:
    the TZ env var, /etc/timezone (Debian/Ubuntu), then the /etc/localtime
    symlink target. Only returns IANA-shaped values (contain "/").
    """
    import os

    def _valid(name: str) -> str:
        # POSIX TZ may carry a leading ":" (e.g. ":America/New_York"); strip it,
        # then accept only IANA-shaped names that ZoneInfo can actually load.
        name = (name or "").strip().lstrip(":").strip()
        if "/" not in name:
            return ""
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(name)
            return name
        except Exception:
            return ""

    cand = _valid(os.environ.get("TZ", ""))
    if cand:
        return cand

    try:
        with open("/etc/timezone", "r", encoding="utf-8") as f:
            cand = _valid(f.read())
        if cand:
            return cand
    except OSError:
        pass

    try:
        link = os.readlink("/etc/localtime")
        if "zoneinfo/" in link:
            cand = _valid(link.split("zoneinfo/", 1)[1])
            if cand:
                return cand
    except OSError:
        pass

    return ""


def utc_now_dt() -> datetime:
    """UTC datetime object for arithmetic."""
    return datetime.now(timezone.utc)


def humanize_duration(seconds: int) -> str:
    """Format a duration in seconds for human-readable display."""
    minutes, remaining_seconds = divmod(seconds, 60)
    if minutes and remaining_seconds:
        return f"{minutes}m {remaining_seconds}s"
    if minutes:
        return f"{minutes}m"
    return f"{remaining_seconds}s"


def to_user_local(utc_dt: datetime, tz_name: str) -> datetime:
    """Convert UTC datetime to user's local timezone.

    Falls back to system local if tz_name is empty, then to UTC
    if system local resolution fails.
    """
    if not tz_name:
        try:
            return utc_dt.astimezone()
        except Exception:
            return utc_dt
    try:
        from zoneinfo import ZoneInfo
        return utc_dt.astimezone(ZoneInfo(tz_name))
    except (KeyError, ImportError):
        return utc_dt


def format_user_time(utc_dt: datetime, tz_name: str, fmt: str = "%I:%M %p") -> str:
    """Format a UTC datetime for user display in their timezone.

    Requires utc_dt to be timezone-aware. Raises ValueError if naive.
    """
    if utc_dt.tzinfo is None:
        raise ValueError(
            f"format_user_time requires timezone-aware datetime, got naive: {utc_dt}"
        )
    local = to_user_local(utc_dt, tz_name)
    return local.strftime(fmt)


def format_user_datetime(utc_dt: datetime, tz_name: str) -> str:
    """Full date+time for display: 'Wednesday, March 26, 2026 — 06:10 PM'

    Requires utc_dt to be timezone-aware. Raises ValueError if naive.
    """
    if utc_dt.tzinfo is None:
        raise ValueError(
            f"format_user_datetime requires timezone-aware datetime, got naive: {utc_dt}"
        )
    local = to_user_local(utc_dt, tz_name)
    return local.strftime("%A, %B %d, %Y — %I:%M %p")


def interpret_local_iso_as_utc(iso_str: str, tz_name: str) -> datetime:
    """Convert a normalized local ISO timestamp to UTC.

    Used after schedule extraction — the extraction model produces local
    ISO strings like '2026-03-26T15:00:00'. This interprets that as
    local time in the user's timezone and converts to UTC.
    """
    from zoneinfo import ZoneInfo
    naive = datetime.fromisoformat(iso_str)
    if naive.tzinfo is None and tz_name:
        try:
            local = naive.replace(tzinfo=ZoneInfo(tz_name))
            return local.astimezone(timezone.utc)
        except (KeyError, ImportError):
            pass
    return naive


def _safe_name(s: str) -> str:
    """Convert a string to a safe filesystem name.

    Prevents path traversal and neutralizes dangerous characters.
    instance_id and conversation_id come from user-controlled input and
    must be treated as untrusted.
    """
    # Remove path traversal
    s = s.replace("..", "")
    # Replace path separators and other dangerous chars
    s = s.replace("/", "_").replace("\\", "_").replace(":", "_")
    # Remove null bytes
    s = s.replace("\x00", "")
    # Ensure non-empty
    if not s or not s.strip():
        s = "_empty_"
    return s

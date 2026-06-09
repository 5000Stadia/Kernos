"""Tests for SPEC-TIMEZONE-ARCHITECTURE: central time utilities."""
import inspect
from datetime import datetime, timezone, timedelta

import pytest

from kernos.utils import (
    utc_now,
    utc_now_dt,
    to_user_local,
    format_user_time,
    format_user_datetime,
    humanize_duration,
    interpret_local_iso_as_utc,
)


class TestUtcNow:
    def test_returns_iso_string(self):
        result = utc_now()
        assert isinstance(result, str)
        assert "+" in result or "Z" in result  # has timezone info

    def test_parseable(self):
        result = utc_now()
        dt = datetime.fromisoformat(result)
        assert dt.tzinfo is not None

    def test_utc_now_dt_is_aware(self):
        dt = utc_now_dt()
        assert dt.tzinfo == timezone.utc


class TestHumanizeDuration:
    def test_formats_90_seconds_and_exposes_public_contract(self):
        assert humanize_duration(90) == "1m 30s"
        signature = inspect.signature(humanize_duration)
        assert list(signature.parameters) == ["seconds"]
        assert signature.parameters["seconds"].annotation is int
        assert signature.return_annotation is str
        assert humanize_duration.__doc__


class TestToUserLocal:
    def test_with_iana_timezone(self):
        utc_dt = datetime(2026, 3, 26, 18, 0, 0, tzinfo=timezone.utc)
        local = to_user_local(utc_dt, "America/Los_Angeles")
        assert local.hour == 11  # PDT = UTC-7

    def test_empty_tz_falls_back_to_system_local(self):
        utc_dt = utc_now_dt()
        local = to_user_local(utc_dt, "")
        assert local.tzinfo is not None

    def test_invalid_tz_returns_utc(self):
        utc_dt = utc_now_dt()
        result = to_user_local(utc_dt, "Not/A/Timezone")
        assert result == utc_dt


class TestFormatUserTime:
    def test_formats_correctly(self):
        utc_dt = datetime(2026, 3, 26, 18, 10, 0, tzinfo=timezone.utc)
        result = format_user_time(utc_dt, "America/Los_Angeles")
        assert "11:10" in result
        assert "AM" in result

    def test_raises_on_naive_datetime(self):
        naive = datetime(2026, 3, 26, 18, 10, 0)
        with pytest.raises(ValueError, match="naive"):
            format_user_time(naive, "America/Los_Angeles")

    def test_custom_format(self):
        utc_dt = datetime(2026, 3, 26, 18, 10, 0, tzinfo=timezone.utc)
        result = format_user_time(utc_dt, "America/Los_Angeles", fmt="%H:%M")
        assert result == "11:10"


class TestFormatUserDatetime:
    def test_full_format(self):
        utc_dt = datetime(2026, 3, 26, 18, 10, 0, tzinfo=timezone.utc)
        result = format_user_datetime(utc_dt, "America/Los_Angeles")
        assert "Thursday" in result
        assert "March" in result
        assert "11:10" in result

    def test_raises_on_naive_datetime(self):
        naive = datetime(2026, 3, 26, 18, 10, 0)
        with pytest.raises(ValueError, match="naive"):
            format_user_datetime(naive, "America/Los_Angeles")


class TestInterpretLocalIsoAsUtc:
    def test_converts_naive_local_to_utc(self):
        result = interpret_local_iso_as_utc("2026-03-26T11:00:00", "America/Los_Angeles")
        assert result.tzinfo == timezone.utc
        assert result.hour == 18  # 11 AM PDT = 18 UTC

    def test_empty_tz_returns_naive(self):
        result = interpret_local_iso_as_utc("2026-03-26T11:00:00", "")
        assert result.tzinfo is None

    def test_already_aware_passes_through(self):
        result = interpret_local_iso_as_utc("2026-03-26T11:00:00-07:00", "America/Los_Angeles")
        # fromisoformat handles the offset
        assert result.tzinfo is not None


class TestSoulTimezoneField:
    def test_default_empty(self):
        from kernos.kernel.soul import Soul
        soul = Soul(instance_id="t1")
        assert soul.timezone == ""

    def test_set_timezone(self):
        from kernos.kernel.soul import Soul
        soul = Soul(instance_id="t1", timezone="America/Los_Angeles")
        assert soul.timezone == "America/Los_Angeles"


class TestNoNowIsoRemains:
    """Verify _now_iso is completely removed from all kernel files."""

    def test_no_now_iso_definitions(self):
        import os
        for root, dirs, files in os.walk("kernos"):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for fname in files:
                if fname.endswith(".py"):
                    path = os.path.join(root, fname)
                    with open(path) as f:
                        content = f.read()
                    assert "def _now_iso(" not in content, f"_now_iso still defined in {path}"


class TestResolveSystemIanaTz:
    def test_tz_env_var_iana_wins(self, monkeypatch):
        from kernos.utils import resolve_system_iana_tz
        monkeypatch.setenv("TZ", "America/New_York")
        assert resolve_system_iana_tz() == "America/New_York"

    def test_bare_abbreviation_in_tz_is_ignored(self, monkeypatch):
        # A non-IANA TZ like "PDT" must NOT be returned (it falls through to the
        # OS files). Whatever comes back must be IANA-shaped or empty — never the
        # abbreviation that broke discovery in the first place.
        from kernos.utils import resolve_system_iana_tz
        monkeypatch.setenv("TZ", "PDT")
        result = resolve_system_iana_tz()
        assert result != "PDT"
        assert result == "" or "/" in result

    def test_returns_iana_or_empty(self, monkeypatch):
        from kernos.utils import resolve_system_iana_tz
        monkeypatch.delenv("TZ", raising=False)
        result = resolve_system_iana_tz()
        assert result == "" or "/" in result

    def test_posix_colon_prefix_is_stripped_and_validated(self, monkeypatch):
        from kernos.utils import resolve_system_iana_tz
        monkeypatch.setenv("TZ", ":America/New_York")
        assert resolve_system_iana_tz() == "America/New_York"

    def test_nonexistent_iana_name_is_rejected(self, monkeypatch):
        from kernos.utils import resolve_system_iana_tz
        monkeypatch.setenv("TZ", "Mars/Olympus_Mons")  # has "/", but not loadable
        result = resolve_system_iana_tz()
        assert result != "Mars/Olympus_Mons"
        assert result == "" or "/" in result

"""Tests for LOG-PERSIST-V1 file handler.

Why this exists: the in-memory log ring buffer was wiped by the
manual restart after the 2026-05-19 silent-gateway failure,
making RCA impossible. This file handler writes to disk so future
investigations can actually see what discord.py logged at the
moment things broke (especially `Heartbeat blocked` warnings).

These tests pin:
* File is created at data/<instance>/diagnostics/server.log when
  KERNOS_INSTANCE_ID is set
* Falls back to data/diagnostics/ when no instance id
* Discord.py's gateway / client loggers are bumped to INFO so the
  failure-mode signals get captured (default discord.py is
  WARNING which would miss `Heartbeat blocked` lines)
* Idempotent install — multiple calls don't duplicate handlers
* Returns None gracefully when data_dir is unwritable
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def reset_handler_singleton():
    """Each test starts with a clean handler singleton + clean root."""
    import kernos.kernel.log_buffer as lb
    # Tear down any handler from a prior test
    if lb._file_handler_singleton is not None:
        try:
            logging.root.removeHandler(lb._file_handler_singleton)
            lb._file_handler_singleton.close()
        except Exception:
            pass
        lb._file_handler_singleton = None
    yield
    # Same teardown after the test
    if lb._file_handler_singleton is not None:
        try:
            logging.root.removeHandler(lb._file_handler_singleton)
            lb._file_handler_singleton.close()
        except Exception:
            pass
        lb._file_handler_singleton = None


class TestInstallLogFileHandler:
    def test_creates_file_under_diagnostics(self, tmp_path, monkeypatch):
        from kernos.kernel.log_buffer import (
            install_log_file_handler, get_log_file_path,
        )
        monkeypatch.delenv("KERNOS_INSTANCE_ID", raising=False)
        handler = install_log_file_handler(data_dir=str(tmp_path))
        assert handler is not None
        expected = tmp_path / "diagnostics" / "server.log"
        assert (tmp_path / "diagnostics").is_dir()
        # Path returned matches
        assert get_log_file_path() == str(expected)

    def test_creates_file_under_instance_dir(self, tmp_path, monkeypatch):
        from kernos.kernel.log_buffer import install_log_file_handler
        monkeypatch.setenv("KERNOS_INSTANCE_ID", "discord:test_123")
        install_log_file_handler(data_dir=str(tmp_path))
        # _safe_name turns 'discord:test_123' into 'discord_test_123'
        expected_dir = tmp_path / "discord_test_123" / "diagnostics"
        assert expected_dir.is_dir()
        assert (expected_dir / "server.log").parent.exists()

    def test_log_records_actually_land_in_file(
        self, tmp_path, monkeypatch,
    ):
        from kernos.kernel.log_buffer import install_log_file_handler
        monkeypatch.delenv("KERNOS_INSTANCE_ID", raising=False)
        handler = install_log_file_handler(data_dir=str(tmp_path))
        assert handler is not None
        log = logging.getLogger("test.log_persist")
        log.setLevel(logging.INFO)
        log.info("PERSIST_TEST_MARKER abc123")
        handler.flush()
        contents = (tmp_path / "diagnostics" / "server.log").read_text()
        assert "PERSIST_TEST_MARKER abc123" in contents

    def test_discord_gateway_logger_bumped_to_info(
        self, tmp_path, monkeypatch,
    ):
        """Critical: discord.py logs `Heartbeat blocked` at WARNING,
        and lots of useful gateway diagnostics at INFO. The default
        propagation level for discord.py is WARNING — without
        bumping discord.gateway to INFO we'd miss the lead-up
        signals (reconnect attempts, session resume, etc.) and only
        see the WARNING when the bot has already started failing.
        """
        from kernos.kernel.log_buffer import install_log_file_handler
        # Reset discord logger level before test
        logging.getLogger("discord.gateway").setLevel(logging.WARNING)
        install_log_file_handler(data_dir=str(tmp_path))
        assert (
            logging.getLogger("discord.gateway").level == logging.INFO
        )
        assert (
            logging.getLogger("discord.client").level == logging.INFO
        )

    def test_idempotent_install(self, tmp_path, monkeypatch):
        from kernos.kernel.log_buffer import install_log_file_handler
        monkeypatch.delenv("KERNOS_INSTANCE_ID", raising=False)
        h1 = install_log_file_handler(data_dir=str(tmp_path))
        h2 = install_log_file_handler(data_dir=str(tmp_path))
        assert h1 is h2  # same singleton

    def test_empty_data_dir_returns_none(self, monkeypatch):
        from kernos.kernel.log_buffer import install_log_file_handler
        monkeypatch.delenv("KERNOS_INSTANCE_ID", raising=False)
        assert install_log_file_handler(data_dir="") is None

    def test_rotation_size_tunable(self, tmp_path, monkeypatch):
        from kernos.kernel.log_buffer import install_log_file_handler
        monkeypatch.delenv("KERNOS_INSTANCE_ID", raising=False)
        monkeypatch.setenv("KERNOS_LOG_FILE_MAX_BYTES", "1024")
        monkeypatch.setenv("KERNOS_LOG_FILE_BACKUP_COUNT", "2")
        handler = install_log_file_handler(data_dir=str(tmp_path))
        assert handler is not None
        # RotatingFileHandler exposes maxBytes / backupCount
        assert handler.maxBytes == 1024
        assert handler.backupCount == 2

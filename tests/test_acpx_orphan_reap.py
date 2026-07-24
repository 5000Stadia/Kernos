"""Tests for the ACPX orphan-agent reaper.

Mocks `ps` output + `os.kill` so nothing real is signalled — verifies the
matching precision (ACP agent binaries only, never `acpi` kernel threads)
and the age guard (live consults are never touched).
"""
import os
import subprocess
from types import SimpleNamespace

from kernos.kernel.external_agents import acpx_adapter


_PS_OUTPUT = """    ELAPSED   PID CMD
   1108000   6058 node /home/user/.npm/_npx/abc/node_modules/.bin/codex-acp
   1108000   6065 /home/user/.npm/_npx/abc/codex-acp-linux-x64/bin/codex-acp
   1027000  47168 /home/user/.npm/_npx/abc/codex-acp-linux-x64/bin/codex-acp
       312    130 [irq/9-acpi]
       312    154 [kworker/R-acpi_thermal_pm]
        45  99001 node /home/user/.npm/_npx/abc/codex-acp-linux-x64/bin/codex-acp
"""


def _run(reaped, monkeypatch, *, cutoff=1800):
    monkeypatch.setattr(acpx_adapter, "MAX_TIMEOUT_SECONDS", 1800)

    def _fake_ps(args, **kwargs):
        assert args[:2] == ["ps", "-eo"]
        return SimpleNamespace(returncode=0, stdout=_PS_OUTPUT, stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_ps)

    def _fake_kill(pid, sig):
        reaped.append(pid)

    monkeypatch.setattr(os, "kill", _fake_kill)
    return acpx_adapter.reap_orphaned_acp_agents(max_age_sec=cutoff)


def test_reaps_old_acp_agents_only(monkeypatch):
    reaped: list[int] = []
    n = _run(reaped, monkeypatch)
    # The three OLD codex-acp procs (>1800s) get killed.
    assert set(reaped) == {6058, 6065, 47168}
    assert n == 3


def test_never_kills_acpi_kernel_threads(monkeypatch):
    reaped: list[int] = []
    _run(reaped, monkeypatch)
    # irq/9-acpi (130) and kworker acpi (154) must NOT be matched.
    assert 130 not in reaped
    assert 154 not in reaped


def test_age_guard_spares_live_consult(monkeypatch):
    reaped: list[int] = []
    _run(reaped, monkeypatch)
    # The 45s-old codex-acp (99001) is a LIVE consult — must be spared.
    assert 99001 not in reaped


def test_ps_failure_is_safe(monkeypatch):
    monkeypatch.setattr(acpx_adapter, "MAX_TIMEOUT_SECONDS", 1800)

    def _boom(args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="nope")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert acpx_adapter.reap_orphaned_acp_agents() == 0

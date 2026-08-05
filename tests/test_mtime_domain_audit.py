"""TEST-TIME-COUPLING-V1 Part C1 — the enforcement half.

`LogicalTimeline` is opt-in: it makes the correct thing convenient, not the
wrong thing impossible. This is what makes it binding.

**Two layers, because one of them failed review.**

The first version tried to prove the invariant from syntax — collect variable
names passed to `.stamp(...)`, then trust any report assigned to one of those
names. kreview bypassed it three ways with fixtures the audit called clean:
stamp then `os.utime` overwrite; create the report with a direct
`Path.write_text`; and stamp a name, then reassign it from a factory so a fresh
host-clock file takes its place. A bare name set cannot establish this — it has
no statement order, no aliasing, and no knowledge of unregistered factories.

So the real boundary is **runtime**: `verify_in_domain` inspects the actual
files immediately before the production call and rejects any report whose mtime
is not one this timeline set. That cannot be fooled by how a file came to
exist, because it reads the same state the production code is about to read.

This module's remaining job is narrow and reliable: ensure domain tests go
*through* that boundary rather than calling production directly. That is a
name-level check with no dataflow reasoning, which is the kind of thing AST is
actually good at.

Waivers are a registered pytest marker with a required reason —
`@pytest.mark.mtime_domain_inert(reason="...")` — not a comment token, because
comments detach from the code they were meant to exempt.
"""
from __future__ import annotations

import ast
import os
import time
from pathlib import Path

import pytest

from kernos.kernel import friction_response as fr
from tests._logical_timeline import LogicalTimeline, verify_in_domain

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Production helpers comparing a filesystem report time against a logical
#: timestamp. Calling one directly from an audited module bypasses the boundary.
MTIME_DOMAIN_CALLS = frozenset({"verify_and_archive"})

#: The enforced entry point that performs the runtime check.
DOMAIN_WRAPPER = "verify_in_domain"

AUDITED_MODULES = ("tests/test_friction_response.py",)

MARKER = "mtime_domain_inert"


def _inert_reason(fn: ast.AST):
    """The waiver marker's reason, or None if unmarked. Empty reason = invalid."""
    for dec in getattr(fn, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        attr = target.attr if isinstance(target, ast.Attribute) else None
        if attr != MARKER:
            continue
        if not isinstance(dec, ast.Call):
            return ""
        for kw in dec.keywords:
            if kw.arg == "reason" and isinstance(kw.value, ast.Constant):
                return str(kw.value.value or "")
        return ""
    return None


def audit_source(source: str) -> list:
    """Findings for one module. Exposed so mutation tests exercise the audit
    itself rather than only its verdict on a clean tree."""
    tree = ast.parse(source)
    findings = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not fn.name.startswith("test_"):
            continue

        raw_calls = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and getattr(n.func, "attr", getattr(n.func, "id", None))
            in MTIME_DOMAIN_CALLS
        ]
        if not raw_calls:
            continue

        reason = _inert_reason(fn)
        if reason is not None:
            if not reason.strip():
                findings.append(f"{fn.name}: {MARKER} marker with no reason")
            continue

        findings.append(
            f"{fn.name}: calls a production mtime-domain helper directly "
            f"(line {raw_calls[0].lineno}); use {DOMAIN_WRAPPER}() so report "
            f"mtimes are verified against the logical timeline at call time")
    return findings


def test_audited_modules_go_through_the_domain_boundary():
    findings = []
    for rel in AUDITED_MODULES:
        findings += [f"{rel}::{f}" for f in
                     audit_source((REPO_ROOT / rel).read_text())]
    assert not findings, findings


# --- the AST layer must be provable able to FAIL ----------------------------

_RAW_CALL = '''
def test_thing(tmp_path):
    f = _seed_friction(tmp_path, "x.md")
    out = fr.verify_and_archive(d, now_iso="2026-06-05T12:00:00+00:00")
    assert out
'''

_VIA_WRAPPER = '''
def test_thing(tmp_path):
    tl = LogicalTimeline.from_iso(PENDING_ISO)
    f = _seed_friction(tmp_path, "x.md")
    tl.stamp(f, hours=-25)
    out = verify_in_domain(tl, d, now_iso=tl.iso(hours=36))
    assert out
'''

_WAIVED = '''
@pytest.mark.mtime_domain_inert(reason="no report participates")
def test_thing(tmp_path):
    out = fr.verify_and_archive(d, now_iso="2026-06-05T12:00:00+00:00")
    assert out
'''

_WAIVED_NO_REASON = '''
@pytest.mark.mtime_domain_inert
def test_thing(tmp_path):
    out = fr.verify_and_archive(d, now_iso="2026-06-05T12:00:00+00:00")
    assert out
'''


@pytest.mark.parametrize("src,label", [
    (_RAW_CALL, "direct production call bypassing the boundary"),
])
def test_audit_rejects_boundary_bypass(src, label):
    assert audit_source(src), f"audit MISSED: {label}"


@pytest.mark.parametrize("src,label", [
    (_VIA_WRAPPER, "goes through the enforced wrapper"),
    (_WAIVED, "validly waived with a reason"),
])
def test_audit_permits_legitimate_shapes(src, label):
    """Counterweight — an over-broad audit gets disabled by whoever it blocks."""
    assert not audit_source(src), f"audit is over-broad: {label}"


def test_waiver_requires_a_reason():
    findings = audit_source(_WAIVED_NO_REASON)
    assert findings and "no reason" in findings[0]


# --- the RUNTIME boundary, against the exact bypasses kreview constructed ---

def _report(tmp_path, name="2026-06-01T07-51-41_CONNECTION_POOL_LEAK_82f2f4aa.md"):
    fdir = tmp_path / "diagnostics" / "friction"
    fdir.mkdir(parents=True, exist_ok=True)
    p = fdir / name
    p.write_text("x")
    return p


def _tl():
    return LogicalTimeline.from_iso("2026-06-04T00:00:00+00:00")


def test_runtime_catches_never_stamped_direct_creation(tmp_path):
    """Bypass 2: report created with a direct write, no factory involved.

    The static guard could not see this at all — it only knew one factory name.
    """
    tl = _tl()
    _report(tmp_path)                      # never stamped
    with pytest.raises(AssertionError, match="never stamped"):
        verify_in_domain(tl, str(tmp_path), now_iso=tl.iso(hours=36))


def test_runtime_catches_stamp_then_overwrite(tmp_path):
    """Bypass 1: stamped, then the mtime is overwritten with the host clock.

    Syntactically the file *was* stamped, which is exactly why a name-set check
    passed it.
    """
    tl = _tl()
    p = _report(tmp_path)
    tl.stamp(p, hours=-25)
    now = time.time()
    os.utime(p, (now, now))                # re-acquires the host clock
    with pytest.raises(AssertionError, match="mtime changed after stamping"):
        verify_in_domain(tl, str(tmp_path), now_iso=tl.iso(hours=36))


def test_runtime_catches_stamped_name_reassigned_to_new_file(tmp_path):
    """Bypass 3: a stamped variable is reassigned to a freshly created report."""
    tl = _tl()
    p = _report(tmp_path, "a.md")
    tl.stamp(p, hours=-25)
    p = _report(tmp_path, "b.md")          # same name, brand-new host-clock file
    with pytest.raises(AssertionError, match="never stamped"):
        verify_in_domain(tl, str(tmp_path), now_iso=tl.iso(hours=36))


def test_runtime_catches_content_rewrite_after_stamping(tmp_path):
    """Rewriting content silently refreshes mtime — no utime call in sight."""
    tl = _tl()
    p = _report(tmp_path)
    tl.stamp(p, hours=-25)
    p.write_text("modified")
    with pytest.raises(AssertionError, match="mtime changed after stamping"):
        verify_in_domain(tl, str(tmp_path), now_iso=tl.iso(hours=36))


def test_runtime_permits_a_correctly_bound_fixture(tmp_path):
    """Counterweight: a properly stamped fixture passes through and the
    production call actually runs."""
    tl = _tl()
    d = str(tmp_path)
    sig = fr.friction_signature(friction_type="CONNECTION_POOL_LEAK")
    quiet = _report(tmp_path)
    tl.stamp(quiet, hours=-25)
    fr.record_attempt(d, friction_signature=sig,
                      friction_type="CONNECTION_POOL_LEAK",
                      resolution_fingerprint="fix_1",
                      state=fr.PENDING_VERIFICATION, now_iso=tl.iso())
    opp = _report(tmp_path, "2026-06-05T11-00-00_INTEGRATION_NO_TOOL_USE_abcdef12.md")
    tl.stamp(opp, hours=25)

    out = verify_in_domain(tl, d, now_iso=tl.iso(hours=36))
    assert sig in out["resolved"]

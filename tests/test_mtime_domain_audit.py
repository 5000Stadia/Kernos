"""TEST-TIME-COUPLING-V1 Part C1 — the enforcement half.

The `LogicalTimeline` helper is opt-in: it makes the correct thing convenient,
it does not make the wrong thing unrepresentable. This audit is what makes it
binding.

**Scoped by production semantics, not by grepping for `time.time()`.** The
defect that started this had no `time.time()` call at all — `write_text`
assigned a real mtime and the assertion compared it against a hard-coded ISO
instant, so a syntactic scan could never find it. The rule is therefore: within
tests that exercise a production helper comparing a filesystem report time
against a logical timestamp, **every created report participating in the
assertion must take its mtime from the logical timeline.**

Deliberately narrow. A suite-wide ban on `time.time()` would be over-broad and
would simply be disabled by whoever it blocked.

Waivers are a registered pytest marker with a required reason —
`@pytest.mark.mtime_domain_inert(reason="...")` — not a comment token, because
comments detach from the code they were meant to exempt.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Production helpers whose semantics compare a filesystem report time against a
#: logical timestamp. A test calling one of these is in the audited domain.
MTIME_DOMAIN_CALLS = frozenset({"verify_and_archive"})

#: Report-creating helpers in the test suite. A report created by one of these
#: acquires the host clock unless it is explicitly stamped.
REPORT_FACTORIES = frozenset({"_seed_friction"})

AUDITED_MODULES = ("tests/test_friction_response.py",)

MARKER = "mtime_domain_inert"


def _calls_in(node: ast.AST) -> set:
    names = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                names.add(f.id)
            elif isinstance(f, ast.Attribute):
                names.add(f.attr)
    return names


def _inert_reason(fn: ast.AST) -> str | None:
    """The waiver marker's reason, or None if unmarked. Empty reason = invalid."""
    for dec in getattr(fn, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        attr = target.attr if isinstance(target, ast.Attribute) else None
        if attr != MARKER:
            continue
        if not isinstance(dec, ast.Call):
            return ""          # marker with no reason at all
        for kw in dec.keywords:
            if kw.arg == "reason" and isinstance(kw.value, ast.Constant):
                return str(kw.value.value or "")
        return ""
    return None


def _unstamped_reports(fn: ast.AST) -> list:
    """Reports created in this test whose mtime is never bound to a timeline."""
    stamped: set = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "stamp":
            for arg in n.args:
                if isinstance(arg, ast.Name):
                    stamped.add(arg.id)

    offenders = []
    for n in ast.walk(fn):
        if not isinstance(n, ast.Assign) or not isinstance(n.value, ast.Call):
            continue
        f = n.value.func
        name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
        if name not in REPORT_FACTORIES:
            continue
        for tgt in n.targets:
            if isinstance(tgt, ast.Name) and tgt.id not in stamped:
                offenders.append(f"{tgt.id} (line {n.lineno})")

    # A report created and immediately discarded can never be stamped.
    for n in ast.walk(fn):
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call):
            f = n.value.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
            if name in REPORT_FACTORIES:
                offenders.append(f"<unassigned> (line {n.lineno})")
    return offenders


def audit_source(source: str) -> list:
    """Findings for one module's source. Exposed so the mutation test can
    exercise the audit itself rather than only its result on a clean tree."""
    tree = ast.parse(source)
    findings = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not fn.name.startswith("test_"):
            continue
        if not (_calls_in(fn) & MTIME_DOMAIN_CALLS):
            continue           # not in the audited domain

        reason = _inert_reason(fn)
        if reason is not None:
            if not reason.strip():
                findings.append(f"{fn.name}: {MARKER} marker with no reason")
            continue           # validly waived

        for offender in _unstamped_reports(fn):
            findings.append(
                f"{fn.name}: report {offender} participates in an "
                f"mtime-domain assertion with an unbound host-clock mtime")
    return findings


def test_no_unbound_report_mtimes_in_the_verification_domain():
    findings = []
    for rel in AUDITED_MODULES:
        findings += [f"{rel}::{f}" for f in
                     audit_source((REPO_ROOT / rel).read_text())]
    assert not findings, (
        "verification fixtures must bind report mtimes to a LogicalTimeline "
        f"(or carry @pytest.mark.{MARKER}(reason=...)): {findings}")


# --- the audit must be proved able to FAIL ----------------------------------

_IMPLICIT_MTIME = '''
def test_thing(tmp_path):
    f = _seed_friction(tmp_path, "x.md")
    out = fr.verify_and_archive(d, now_iso="2026-06-05T12:00:00+00:00")
    assert out
'''

_DISCARDED_REPORT = '''
def test_thing(tmp_path):
    _seed_friction(tmp_path, "x.md")
    out = fr.verify_and_archive(d, now_iso="2026-06-05T12:00:00+00:00")
    assert out
'''

_EXPLICIT_TIME_TIME = '''
def test_thing(tmp_path):
    f = _seed_friction(tmp_path, "x.md")
    os.utime(f, (time.time(), time.time()))
    out = fr.verify_and_archive(d, now_iso="2026-06-05T12:00:00+00:00")
    assert out
'''

_STAMPED_OK = '''
def test_thing(tmp_path):
    tl = LogicalTimeline.from_iso(PENDING_ISO)
    f = _seed_friction(tmp_path, "x.md")
    tl.stamp(f, hours=-25)
    out = fr.verify_and_archive(d, now_iso=tl.iso(hours=36))
    assert out
'''

_OUT_OF_DOMAIN = '''
def test_thing(tmp_path):
    f = _seed_friction(tmp_path, "x.md")
    assert f.exists()
'''

_WAIVED = '''
@pytest.mark.mtime_domain_inert(reason="no report participates in the assertion")
def test_thing(tmp_path):
    f = _seed_friction(tmp_path, "x.md")
    out = fr.verify_and_archive(d, now_iso="2026-06-05T12:00:00+00:00")
    assert out
'''

_WAIVED_NO_REASON = '''
@pytest.mark.mtime_domain_inert
def test_thing(tmp_path):
    f = _seed_friction(tmp_path, "x.md")
    out = fr.verify_and_archive(d, now_iso="2026-06-05T12:00:00+00:00")
    assert out
'''


@pytest.mark.parametrize("src,label", [
    (_IMPLICIT_MTIME, "implicit write_text mtime"),
    (_DISCARDED_REPORT, "report created and discarded"),
    (_EXPLICIT_TIME_TIME, "explicit time.time() mtime"),
])
def test_audit_catches_unbound_mtimes(src, label):
    """Mutation: each injected coupling must be caught. Both the implicit form
    (which a syntactic grep misses entirely) and the explicit one."""
    assert audit_source(src), f"audit MISSED: {label}"


@pytest.mark.parametrize("src,label", [
    (_STAMPED_OK, "properly stamped fixture"),
    (_OUT_OF_DOMAIN, "no mtime-domain call"),
    (_WAIVED, "validly waived with a reason"),
])
def test_audit_permits_legitimate_shapes(src, label):
    """Counterweight — an over-broad audit gets disabled by whoever it blocks."""
    assert not audit_source(src), f"audit is over-broad: {label}"


def test_waiver_requires_a_reason():
    findings = audit_source(_WAIVED_NO_REASON)
    assert findings and "no reason" in findings[0]

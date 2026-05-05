"""REFERENCE-PRIMITIVE-V1 embedded live test runner.

Per spec § "Embedded live test". Runs the nine sub-tests against the
real ``docs/`` tree using stubbed LLMs (the substrate-fidelity
assertions are the load-bearing check; real-network LLM calls are a
founder-runtime concern). Output: a markdown report at
``data/diagnostics/live-tests/REFERENCE-PRIMITIVE-V1-live-test.md``.

Run from the repo root:

    .venv/bin/python -m scripts.reference_primitive_live_test
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Allow running both as `python -m scripts.reference_primitive_live_test`
# and `python scripts/reference_primitive_live_test.py`.
if __name__ == "__main__" and __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kernos.kernel import event_stream
from kernos.kernel.kernel_tool_registry import kernel_tool_schemas
from kernos.kernel.reference.catalog import (
    CatalogStore,
    SCOPE_INSTANCE,
    TRUST_AGENT_AUTHORED,
    TRUST_CANONICAL,
    TRUST_EXTERNAL_SNAPSHOT,
    TRUST_QUARANTINED,
    scope_for_domain,
)
from kernos.kernel.reference.cohort import CatalogingCohort
from kernos.kernel.reference.events import (
    REFERENCE_SOURCE_MODULE,
    ReferenceEventEmitter,
)
from kernos.kernel.reference.induction import induce
from kernos.kernel.reference.ingest import (
    IngestionScanner,
    docs_source_root,
    references_source_root,
)
from kernos.kernel.reference.injection import inject_entry
from kernos.kernel.reference.tools import (
    ReferenceService,
    ReferenceServiceContext,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_ROOT = REPO_ROOT / "docs"
REPORT_DIR = REPO_ROOT / "data" / "diagnostics" / "live-tests"
REPORT_PATH = REPORT_DIR / "REFERENCE-PRIMITIVE-V1-live-test.md"


# ---------------------------------------------------------------------------
# Stub LLMs
# ---------------------------------------------------------------------------


class _CatalogingStubLLM:
    """Returns a deterministic short string for cataloging prompts.

    Real-network cataloging is a founder-runtime concern; the
    substrate-fidelity assertions don't depend on natural-language
    quality of the one-liners."""

    @property
    def temperature(self) -> float:
        return 0.1

    async def complete(self, prompt: str) -> str:
        # Pull the section title out of the prompt for a slightly
        # more realistic one-liner.
        for line in prompt.splitlines():
            if line.startswith("Section title: "):
                return f"summary of {line.removeprefix('Section title: ').strip()}"
        return "summary"


class _NavigatorStubLLM:
    """Pre-loadable navigator. Tests set ``next_response`` before
    each call."""

    def __init__(self) -> None:
        self.next_response: str | None = None

    @property
    def temperature(self) -> float:
        return 0.1

    async def complete(self, prompt: str) -> str:
        return self.next_response or "NONE"


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------


class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.results: dict[str, str] = {}

    def header(self, text: str) -> None:
        self.lines.append(text)

    def test(self, name: str, status: str, detail: str) -> None:
        self.results[name] = status
        marker = "PASS" if status == "pass" else status.upper()
        self.lines.append(f"### {name} — {marker}")
        self.lines.append("")
        self.lines.append(detail)
        self.lines.append("")

    def render(self) -> str:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        n_pass = sum(1 for v in self.results.values() if v == "pass")
        n_total = len(self.results)
        head = [
            "# REFERENCE-PRIMITIVE-V1 — Embedded Live Test Report",
            "",
            f"**Run:** {ts}",
            f"**Pass count:** {n_pass} / {n_total}",
            "",
            "## Setup",
            "",
            "Substrate-fidelity verification against the real `docs/` tree "
            "using stubbed LLMs. Real-network LLM verification of the "
            "cataloging cohort + navigator is a founder-runtime concern; "
            "this report pins the substrate behavior — catalog rows, "
            "events, scope visibility, hash validation, retire-via-strike, "
            "auto-induction shape — that does NOT depend on LLM quality.",
            "",
            "## Per-test results",
            "",
        ]
        return "\n".join(head + self.lines)


# ---------------------------------------------------------------------------
# Runtime context
# ---------------------------------------------------------------------------


class Context:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self.catalog: CatalogStore | None = None
        self.cohort: CatalogingCohort | None = None
        self.emitter: ReferenceEventEmitter | None = None
        self.scanner: IngestionScanner | None = None
        self.service: ReferenceService | None = None
        self.navigator = _NavigatorStubLLM()
        self.refs_root: Path = Path(data_dir) / "references"

    async def startup(self) -> None:
        await event_stream._reset_for_tests()
        await event_stream.start_writer(self.data_dir, flush_interval_s=0.05)
        self.catalog = CatalogStore()
        await self.catalog.start(self.data_dir)
        registry = event_stream.emitter_registry()
        raw = registry.register(REFERENCE_SOURCE_MODULE)
        self.emitter = ReferenceEventEmitter(emitter=raw)
        self.cohort = CatalogingCohort(
            catalog=self.catalog, emitter=self.emitter,
            llm=_CatalogingStubLLM(), instance_id="default",
        )
        await self.cohort.start()
        self.scanner = IngestionScanner(
            catalog=self.catalog, cohort=self.cohort,
            emitter=self.emitter, instance_id="default",
        )
        self.scanner.add_source(docs_source_root(DOCS_ROOT))
        self.refs_root.mkdir(parents=True, exist_ok=True)
        self.scanner.add_source(
            references_source_root(
                references_path=self.refs_root, domain_id="space-A",
            )
        )
        self.scanner.add_source(
            references_source_root(
                references_path=self.refs_root, domain_id="space-B",
            )
        )
        self.service = ReferenceService(
            catalog=self.catalog, cohort=self.cohort,
            emitter=self.emitter, navigator_llm=self.navigator,
            references_root=self.refs_root,
            instance_id="default",
        )

    async def shutdown(self) -> None:
        if self.cohort:
            await self.cohort.stop()
        if self.catalog:
            await self.catalog.stop()
        await event_stream.stop_writer()
        await event_stream._reset_for_tests()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_1_cataloging_fires_on_install(ctx: Context, report: Report) -> None:
    """Pick three real docs files; verify catalog rows match the
    section count for each."""
    summary = await ctx.scanner.scan()
    await ctx.cohort.drain()
    samples = [
        DOCS_ROOT / "architecture" / "overview.md",
        DOCS_ROOT / "TECHNICAL-ARCHITECTURE.md",
        DOCS_ROOT / "kernos-introduction.md",
    ]
    detail_lines: list[str] = []
    all_ok = True
    detail_lines.append(f"Scan summary: {summary}")
    for s in samples:
        if not s.exists():
            detail_lines.append(f"- MISSING: {s.relative_to(REPO_ROOT)}")
            all_ok = False
            continue
        body = s.read_text(encoding="utf-8")
        n_h2 = sum(1 for L in body.splitlines() if L.startswith("## "))
        rows = await ctx.catalog.list_entries_for_file(
            instance_id="default", file_path=str(s),
        )
        # Files without any h2 produce a single section.
        expected = max(n_h2, 1)
        ok = len(rows) == expected
        if not ok:
            all_ok = False
        detail_lines.append(
            f"- {s.relative_to(REPO_ROOT)}: h2-count={n_h2}, "
            f"catalog-rows={len(rows)} (expected {expected}) — "
            f"{'OK' if ok else 'MISMATCH'}"
        )
        # Spot-check fields.
        if rows:
            r = rows[0]
            assert r.scope == SCOPE_INSTANCE
            assert r.source_hash and len(r.source_hash) == 64
            assert r.indexed_at
    report.test(
        "Test 1 — Cataloging fires on install",
        "pass" if all_ok else "fail",
        "\n".join(detail_lines),
    )


async def test_2_request_reference_returns_canonical(
    ctx: Context, report: Report,
) -> None:
    """Pick a real catalog entry whose section_title contains
    "gate" or "classification"; navigate to it; verify content
    arrives intact."""
    rows = await ctx.catalog.list_visible(
        instance_id="default", domain_id="space-A",
    )
    target = next(
        (
            r for r in rows
            if r.entry_type == "file"
            and ("gate" in r.section_title.lower()
                 or "classification" in r.section_title.lower()
                 or "covenant" in r.section_title.lower())
        ),
        None,
    )
    if target is None:
        # Fall back: pick any file-level row.
        target = next((r for r in rows if r.entry_type == "file"), None)
    if target is None:
        report.test(
            "Test 2 — request_reference returns canonical content",
            "fail",
            "No file-level catalog rows present; cataloging may have skipped.",
        )
        return
    ctx.navigator.next_response = target.entry_id
    result = await ctx.service.handle_request_reference(
        ctx=ReferenceServiceContext(
            instance_id="default", domain_id="space-A", member_id="founder",
        ),
        brief_request=f"about the {target.section_title}",
    )
    detail = (
        f"- chosen entry: `{target.entry_id}` "
        f"(`{Path(target.file_path).name}` — {target.section_title})\n"
        f"- result.status: `{result.get('status')}`\n"
        f"- content len: {len(result.get('content') or '')}\n"
        f"- line range: {result.get('line_start')}-{result.get('line_end')}"
    )
    ok = result.get("status") == "ok" and (result.get("content") or "")
    report.test(
        "Test 2 — request_reference returns canonical content",
        "pass" if ok else "fail",
        detail,
    )


async def test_3_hash_validation_fail_closed(
    ctx: Context, report: Report,
) -> None:
    """Simulate an out-of-band edit and verify fail-closed behavior."""
    # Use a tmp file inside the data dir so we can safely mutate it.
    target = Path(ctx.data_dir) / "drift_test.md"
    target.write_text(
        "## Drift section\n\nbody A\n", encoding="utf-8",
    )
    ctx.scanner.add_source(
        docs_source_root(target.parent)
    )
    await ctx.scanner.scan()
    await ctx.cohort.drain()
    rows_before = await ctx.catalog.list_entries_for_file(
        instance_id="default", file_path=str(target),
    )
    if not rows_before:
        report.test(
            "Test 3 — Hash validation fail-closed",
            "fail",
            "Drift-test file did not catalog.",
        )
        return
    # Edit the file out-of-band so the hash drifts.
    target.write_text(
        "## Drift section\n\nbody A — EDITED\n\n## New\n\nbody B\n",
        encoding="utf-8",
    )
    result = await inject_entry(
        entry_id=rows_before[0].entry_id,
        catalog=ctx.catalog,
        emitter=ctx.emitter,
        cohort=ctx.cohort,
        instance_id="default",
    )
    fail_closed = (
        not result.success
        and result.fail_reason == "hash_mismatch"
        and "Reference unavailable" in result.content
    )
    await ctx.cohort.drain()
    rows_after = await ctx.catalog.list_entries_for_file(
        instance_id="default", file_path=str(target),
    )
    detail = (
        f"- pre-edit catalog rows: {len(rows_before)}\n"
        f"- inject result.success: {result.success}\n"
        f"- inject result.fail_reason: `{result.fail_reason}`\n"
        f"- post-recatalog rows: {len(rows_after)}"
    )
    ok = fail_closed and len(rows_after) == 2
    report.test(
        "Test 3 — Hash validation fail-closed",
        "pass" if ok else "fail",
        detail,
    )


async def test_4_store_reference_round_trip(
    ctx: Context, report: Report,
) -> None:
    rctx = ReferenceServiceContext(
        instance_id="default", domain_id="space-A", member_id="founder",
    )
    await ctx.service.handle_create_reference_collection(
        ctx=rctx,
        name="vendor-test-api",
        purpose="Test vendor API for live test.",
        trust_tier=TRUST_EXTERNAL_SNAPSHOT,
        refresh_policy="snapshot",
        provenance={"source_url": "https://example.com/api"},
    )
    store_res = await ctx.service.handle_store_reference(
        ctx=rctx,
        content=(
            "## Authentication\n\nThe API uses Bearer tokens. "
            "Pass the token in the Authorization header.\n"
        ),
        collection="vendor-test-api",
        filename="auth.md",
        trust_tier=TRUST_EXTERNAL_SNAPSHOT,
        metadata={
            "source_url": "https://example.com/api/auth",
            "fetched_at": "2026-05-04",
        },
    )
    await ctx.cohort.drain()
    # Find the stored entry's id and request_reference for it.
    rows = await ctx.catalog.list_visible(
        instance_id="default", domain_id="space-A",
    )
    auth_rows = [
        r for r in rows
        if r.collection_name == "vendor-test-api"
        or r.collection_back_reference == "vendor-test-api"
    ]
    auth_file = next(
        (r for r in auth_rows if r.entry_type == "file"),
        None,
    )
    request_ok = False
    annotation = ""
    if auth_file:
        ctx.navigator.next_response = auth_file.entry_id
        rr = await ctx.service.handle_request_reference(
            ctx=rctx,
            brief_request="how does the test vendor authenticate?",
        )
        request_ok = (
            rr.get("status") == "ok"
            and "Bearer" in (rr.get("content") or "")
        )
        annotation = rr.get("provenance_annotation") or ""
    coll = await ctx.catalog.get_collection_entry(
        instance_id="default",
        collection_name="vendor-test-api",
        scope=scope_for_domain("space-A"),
    )
    detail = (
        f"- store status: `{store_res.get('status')}`\n"
        f"- collection-level entry exists: {coll is not None}\n"
        f"- file-level entry catalogued: {auth_file is not None}\n"
        f"- request_reference status: ok={request_ok}\n"
        f"- annotation includes snapshot framing: "
        f"{'not canonical live truth' in annotation}"
    )
    report.test(
        "Test 4 — store_reference + async cataloging + retrieval",
        "pass" if (request_ok and coll is not None
                   and "not canonical live truth" in annotation) else "fail",
        detail,
    )


async def test_5_auto_induction_conservative(
    ctx: Context, report: Report,
) -> None:
    result = await induce(
        catalog=ctx.catalog,
        instance_id="default",
        domain_id="space-A",
        signals=["how do covenants compose with gates"],
    )
    detail = (
        f"- bounded set N injected: {len(result.injected)} (≤ 2)\n"
        f"- additional surfaced pairs: {len(result.surfaced_pairs)}\n"
        + ("\n".join(
            f"  - injected: `{c.entry.section_title}` "
            f"(overlap={c.overlap}, tier={c.entry.trust_tier})"
            for c in result.injected
        ))
    )
    ok = len(result.injected) <= 2
    report.test(
        "Test 5 — Auto-induction surfaces conservatively",
        "pass" if ok else "fail",
        detail,
    )


async def test_6_quarantine_recovery(ctx: Context, report: Report) -> None:
    rctx = ReferenceServiceContext(
        instance_id="default", domain_id="space-A", member_id="founder",
    )
    rows = await ctx.catalog.list_visible(
        instance_id="default", domain_id="space-A",
    )
    file_row = next(
        (r for r in rows
         if r.entry_type == "file"
         and r.scope == scope_for_domain("space-A")),
        None,
    )
    if not file_row:
        report.test(
            "Test 6 — Recovery primitive (quarantine)", "fail",
            "No domain-scoped file entry to quarantine.",
        )
        return
    qr = await ctx.service.handle_quarantine_reference(
        ctx=rctx, entry_id=file_row.entry_id, reason="live test quarantine",
    )
    refreshed = await ctx.catalog.get_entry(entry_id=file_row.entry_id)
    detail = (
        f"- target entry: `{file_row.entry_id}`\n"
        f"- quarantine result status: `{qr.get('status')}`\n"
        f"- post trust_tier: `{refreshed.trust_tier if refreshed else 'MISSING'}`\n"
        f"- auto_inducible: {refreshed.auto_inducible if refreshed else 'MISSING'}"
    )
    ok = (
        qr.get("status") == "ok"
        and refreshed is not None
        and refreshed.trust_tier == TRUST_QUARANTINED
        and not refreshed.auto_inducible
    )
    report.test(
        "Test 6 — Recovery primitive (quarantine)",
        "pass" if ok else "fail",
        detail,
    )


async def test_7_collection_level_surfaces_map(
    ctx: Context, report: Report,
) -> None:
    """Auto-induction over a broad signal: the collection-level
    entry should surface its purpose + member-file count, not a
    bundle of file content."""
    # Signal tuned for the external_snapshot threshold (>=4 overlapping
    # tokens); the collection's purpose includes "test vendor api live"
    # so this signal surfaces it as a confident match without forcing
    # the test fixture to use a more-permissive trust tier.
    result = await induce(
        catalog=ctx.catalog,
        instance_id="default",
        domain_id="space-A",
        signals=["test vendor api authentication live docs"],
    )
    coll_match = next(
        (c for c in result.injected if c.entry.entry_type == "collection"),
        None,
    )
    detail_lines: list[str] = []
    detail_lines.append(
        f"- injected count: {len(result.injected)}\n"
        f"- surfaced pairs: {len(result.surfaced_pairs)}"
    )
    if coll_match:
        detail_lines.append(
            f"- collection match: `{coll_match.entry.collection_name}`\n"
            f"- purpose: {coll_match.entry.purpose}\n"
            f"- member_file_count: {coll_match.entry.member_file_count}"
        )
    ok = coll_match is not None or any(
        "[collection]" in s[0] for s in result.surfaced_pairs
    )
    report.test(
        "Test 7 — Collection-level auto-induction surfaces map, not bundle",
        "pass" if ok else "fail",
        "\n".join(detail_lines),
    )


def test_8_read_doc_retired_clean(report: Report) -> None:
    """Static check: read_doc is no longer in the kernel-tool catalog."""
    names = {s["name"] for s in kernel_tool_schemas()}
    detail_lines = [
        f"- 'read_doc' in kernel-tool catalog: {'read_doc' in names}",
        f"- 'request_reference' in kernel-tool catalog: "
        f"{'request_reference' in names}",
        f"- 'store_reference' in kernel-tool catalog: "
        f"{'store_reference' in names}",
    ]
    ok = (
        "read_doc" not in names
        and "request_reference" in names
        and "store_reference" in names
    )
    report.test(
        "Test 8 — read_doc retired cleanly",
        "pass" if ok else "fail",
        "\n".join(detail_lines),
    )


async def test_9_scope_visibility(ctx: Context, report: Report) -> None:
    rctx_A = ReferenceServiceContext(
        instance_id="default", domain_id="space-A", member_id="m1",
    )
    rctx_B = ReferenceServiceContext(
        instance_id="default", domain_id="space-B", member_id="m2",
    )
    # Store something in space-A.
    await ctx.service.handle_create_reference_collection(
        ctx=rctx_A, name="A-only", purpose="domain-A-only material",
    )
    await ctx.service.handle_store_reference(
        ctx=rctx_A,
        content="## DomainA Note\n\nexclusively domain-A content here.\n",
        collection="A-only",
        filename="note.md",
        trust_tier=TRUST_AGENT_AUTHORED,
    )
    await ctx.cohort.drain()

    # 9.1 Cross-domain isolation: agent in space-B cannot see space-A
    # content via list_visible.
    rows_B = await ctx.catalog.list_visible(
        instance_id="default", domain_id="space-B",
    )
    A_only_visible_in_B = any(
        r.collection_name == "A-only" or r.collection_back_reference == "A-only"
        for r in rows_B
    )

    # 9.2 Instance scope reaches all domains (docs/ is instance-scoped).
    instance_visible_in_B = any(
        r.scope == SCOPE_INSTANCE for r in rows_B
    )

    # 9.3 Same-domain retrieval works.
    rows_A = await ctx.catalog.list_visible(
        instance_id="default", domain_id="space-A",
    )
    A_only_visible_in_A = any(
        r.collection_name == "A-only" or r.collection_back_reference == "A-only"
        for r in rows_A
    )

    detail = (
        "- 9.1 cross-domain isolation: A-only visible in B = "
        f"{A_only_visible_in_B} (expect False)\n"
        "- 9.2 instance scope reaches all domains: docs visible in B = "
        f"{instance_visible_in_B} (expect True)\n"
        "- 9.3 same-domain retrieval: A-only visible in A = "
        f"{A_only_visible_in_A} (expect True)"
    )
    ok = (
        not A_only_visible_in_B
        and instance_visible_in_B
        and A_only_visible_in_A
    )
    report.test(
        "Test 9 — Scope visibility pin",
        "pass" if ok else "fail",
        detail,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> int:
    report = Report()
    with tempfile.TemporaryDirectory() as data_dir:
        ctx = Context(data_dir)
        try:
            await ctx.startup()
            await test_1_cataloging_fires_on_install(ctx, report)
            await test_2_request_reference_returns_canonical(ctx, report)
            await test_3_hash_validation_fail_closed(ctx, report)
            await test_4_store_reference_round_trip(ctx, report)
            await test_5_auto_induction_conservative(ctx, report)
            await test_6_quarantine_recovery(ctx, report)
            await test_7_collection_level_surfaces_map(ctx, report)
            test_8_read_doc_retired_clean(report)
            await test_9_scope_visibility(ctx, report)
        finally:
            await ctx.shutdown()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report.render(), encoding="utf-8")
    print(report.render())
    failures = [k for k, v in report.results.items() if v != "pass"]
    return 0 if not failures else 1


if __name__ == "__main__":
    rc = asyncio.run(main())
    sys.exit(rc)

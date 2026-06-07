"""SELF-MAINTENANCE-REVIEW-V1 — KERNOS's daily self-stewardship review.

Once a day, KERNOS holds ONE slice of its own code + systems up to the light
through two lenses:

  * **Corrective** — is this still the healthiest implementation of its
    intention, or has it drifted / decayed / grown an unguarded edge?
  * **Generative** — even when healthy, is there a more efficient or effective
    way, and does this function's validity + role still hold up against the
    overarching intention of the WHOLE KERNOS system?

It produces a short, honest report and surfaces it as a whisper to the main
agent **to consider** — never to act on autonomously. Every actual change still
flows through approval-gated ``improve_kernos``. Thoughtful evolution, not
out-of-hand mutation: at most ONE minor, reversible, well-justified evolution
idea per review.

Design mirrors recursive_self_heal: seam-injected (consult_fn / whisper_fn),
inert unless ``KERNOS_SELF_MAINTENANCE_REVIEW`` is set (default OFF for v1),
deterministic + unit-testable. The orchestration (``maybe_run_daily``) is
idle-aware and runs at most once per 24h.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Kill switch (default OFF for v1) + cadence
# ---------------------------------------------------------------------------

MIN_HOURS_BETWEEN_REVIEWS = 20.0  # "~once a day", with slack so a slightly
#                                   early daily tick still fires.
DEDUP_TTL_DAYS = 14.0  # don't re-surface the same observation for two weeks.


def is_enabled() -> bool:
    """SELF-MAINTENANCE-REVIEW-V3: ships DEFAULT-ON. The daily review is
    reflection-only (never changes code on its own), idle-aware (defers to live
    turns), and costs ~one bounded LLM call/day, so it's on out of the box.
    Disable explicitly with ``KERNOS_SELF_MAINTENANCE_REVIEW`` in
    {0, false, off, no}."""
    return os.environ.get("KERNOS_SELF_MAINTENANCE_REVIEW", "").strip().lower() not in (
        "0", "false", "off", "no",
    )


# ---------------------------------------------------------------------------
# The rotating slices — one reviewed per day, cursor advances. Over ~a week
# the whole system is covered. Each carries an intent pointer so the review
# reads intention (docs/spec) against as-built code.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewSlice:
    name: str
    intent: str           # the documented intention, in one line
    paths: tuple[str, ...]  # the as-built code to read
    constitutional: bool = False  # self-governance/maintenance machinery —
    #   reviewable + ponderable, but any evolution is HUMAN-GATED (never
    #   self-applied). Nothing is exempt from review; the methodology audits
    #   itself, but it cannot quietly rewrite its own rules.


# SELF-MAINTENANCE-REVIEW-V3: a COMPREHENSIVE functional map — every substantive
# module belongs to exactly one intention-defined element (single-owner: the
# most-specific matching path wins). `paths` are exact files or directory
# prefixes (trailing "/") only — NO globs (matches `_path_matches`). The 11
# original slice names are preserved.
REVIEW_SLICES: tuple[ReviewSlice, ...] = (
    # --- Turn pipeline & cognition ---------------------------------------
    ReviewSlice(
        "message-pipeline",
        "Six-phase turn pipeline (provision→route→assemble→reason→consequence→"
        "persist) + handler orchestration + slash commands; adapter/handler "
        "isolation.",
        ("kernos/messages/handler.py", "kernos/messages/pipeline.py",
         "kernos/messages/phases/", "kernos/messages/phase_context.py",
         "kernos/messages/models.py", "kernos/messages/reference.py"),
    ),
    ReviewSlice(
        "message-adapters",
        "Platform adapters (Discord/Telegram/SMS): translate events to/from "
        "NormalizedMessage; never import the handler.",
        ("kernos/messages/adapters/", "kernos/sms_poller.py",
         "kernos/telegram_poller.py"),
    ),
    ReviewSlice(
        "reasoning",
        "Tool loop, provider chains, kernel-tool dispatch, cost logging.",
        ("kernos/kernel/reasoning.py", "kernos/providers/chains.py",
         "kernos/kernel/exceptions.py", "kernos/kernel/turn_runner.py",
         "kernos/kernel/turn_runner_provider.py"),
    ),
    ReviewSlice(
        "providers",
        "Provider-agnostic model backends behind a common ABC + model routing.",
        ("kernos/providers/base.py", "kernos/providers/anthropic_provider.py",
         "kernos/providers/codex_provider.py", "kernos/providers/ollama_provider.py",
         "kernos/models/", "kernos/kernel/model_routing.py"),
    ),
    ReviewSlice(
        "cognitive-context-assembly",
        "The typed cognitive substrate + seven Cognitive-UI zones + response "
        "delivery.",
        ("kernos/kernel/cognitive_context/", "kernos/kernel/response_delivery.py"),
    ),
    ReviewSlice(
        "context-routing",
        "Message→ContextSpace routing, candidate selection, per-space evidence.",
        ("kernos/kernel/router.py", "kernos/kernel/space_candidates.py",
         "kernos/kernel/space_evidence.py", "kernos/kernel/spaces.py"),
    ),
    ReviewSlice(
        "dispatch-gate",
        "Action-based tool classification + scoped amortization at the dispatch "
        "boundary; proportional caution on user data; binding diagnostics.",
        ("kernos/kernel/gate.py", "kernos/kernel/dispatch_diagnostics.py",
         "kernos/kernel/tools/operation_resolver.py"),
    ),
    # --- Memory, knowledge & stewardship ---------------------------------
    ReviewSlice(
        "stewardship",
        "Compaction harvest: value extraction, tension detection, sensitivity "
        "classification, operational insights as whispers; token accounting.",
        ("kernos/kernel/compaction.py", "kernos/kernel/fact_harvest.py",
         "kernos/kernel/tokens.py", "kernos/kernel/token_estimator.py"),
    ),
    ReviewSlice(
        "knowledge-retrieval",
        "The memory moat: retrieval over knowledge/entities/archives, entity "
        "resolution, dedup, embeddings.",
        ("kernos/kernel/retrieval.py", "kernos/kernel/resolution.py",
         "kernos/kernel/entities.py", "kernos/kernel/dedup.py",
         "kernos/kernel/embeddings.py", "kernos/kernel/embedding_store.py",
         "kernos/kernel/note_this.py"),
    ),
    ReviewSlice(
        "projectors",
        "Post-response tier1/tier2 extraction coordinator, per-member writes.",
        ("kernos/kernel/projectors/",),
    ),
    ReviewSlice(
        "awareness",
        "Whispers + suppression: surface insight only when there's a concrete "
        "actionable idea; ambient, not demanding.",
        ("kernos/kernel/awareness.py",),
    ),
    ReviewSlice(
        "reference-primitive",
        "Cataloging cohort, hash-validated injection, auto-induction, baked "
        "catalog.",
        ("kernos/kernel/reference/",),
    ),
    ReviewSlice(
        "canvas",
        "Scoped markdown pages, wiki-link reference index, Gardener shape "
        "authority.",
        ("kernos/kernel/canvas.py", "kernos/kernel/canvas_reference_index.py",
         "kernos/kernel/gardener.py", "kernos/cohorts/gardener.py",
         "kernos/cohorts/gardener_prompts.py",
         "kernos/kernel/cohorts/gardener_cohort.py",
         "kernos/setup/seed_canvases.py"),
    ),
    # --- Members & coordination ------------------------------------------
    ReviewSlice(
        "multi-member-identity",
        "Per-member profiles, hatching, member management, display names; the "
        "Soul shim (deprecated for identity).",
        ("kernos/kernel/members.py", "kernos/kernel/soul.py",
         "kernos/kernel/display_names.py", "kernos/kernel/conversation_log.py"),
    ),
    ReviewSlice(
        "relationships-covenants-disclosure",
        "Pairwise relationships, permission profiles, covenants, cross-member "
        "disclosure gate, preference reconcile, messenger stewardship.",
        ("kernos/kernel/covenant_manager.py", "kernos/kernel/contract_parser.py",
         "kernos/kernel/disclosure_gate.py", "kernos/kernel/preference_parser.py",
         "kernos/kernel/preference_reconcile.py",
         "kernos/kernel/cohorts/covenant_cohort.py", "kernos/cohorts/messenger.py",
         "kernos/cohorts/messenger_prompt.py", "kernos/cohorts/admin.py"),
    ),
    ReviewSlice(
        "member-coordination",
        "Member-to-member relational messaging + parcels + cross-space request "
        "dispatch.",
        ("kernos/kernel/relational_messaging.py",
         "kernos/kernel/relational_dispatch.py", "kernos/kernel/relational_tools.py",
         "kernos/kernel/parcel.py", "kernos/kernel/cross_space/"),
    ),
    # --- Kernel substrate ------------------------------------------------
    ReviewSlice(
        "event-stream",
        "The append-only nervous system: best-effort emission, durable timeline, "
        "runtime trace, log buffer.",
        ("kernos/kernel/events.py", "kernos/kernel/event_stream.py",
         "kernos/kernel/event_types.py", "kernos/kernel/runtime_trace.py",
         "kernos/kernel/log_buffer.py"),
    ),
    ReviewSlice(
        "state-store",
        "The runtime query surface: State Store (JSON/SQLite), instance.db "
        "member/relationship/abuse tables, shadow-archive semantics.",
        ("kernos/kernel/state.py", "kernos/kernel/state_json.py",
         "kernos/kernel/state_sqlite.py", "kernos/kernel/instance_db.py",
         "kernos/persistence/"),
    ),
    ReviewSlice(
        "task-engine",
        "The kernel execution layer: Task model, engine, self-directed plan "
        "step execution + budget, protocols.",
        ("kernos/kernel/engine.py", "kernos/kernel/task.py",
         "kernos/kernel/execution.py", "kernos/kernel/protocols.py"),
    ),
    ReviewSlice(
        "introspection-dump",
        "'What Kernos believes is true' views + state introspection for /dump.",
        ("kernos/kernel/introspection.py",),
    ),
    # --- Tools & capabilities --------------------------------------------
    ReviewSlice(
        "tool-catalog-registry",
        "Universal tool catalog + canonical kernel-tool registry, schemas, "
        "aliases, audit, introspection.",
        ("kernos/kernel/tool_catalog.py", "kernos/kernel/kernel_tool_registry.py",
         "kernos/kernel/tools/", "kernos/kernel/tool_aliases.py",
         "kernos/kernel/tool_namespace.py",
         "kernos/kernel/tool_audit.py", "kernos/kernel/tool_introspection.py",
         "kernos/kernel/tool_gate_routing.py"),
    ),
    ReviewSlice(
        "workshop-tool-primitive",
        "Tool-making: descriptors, runtime context + enforcement, authoring-"
        "pattern validation, the external-service registry.",
        ("kernos/kernel/tool_descriptor.py", "kernos/kernel/tool_runtime.py",
         "kernos/kernel/tool_runtime_enforcement.py", "kernos/kernel/tool_validation.py",
         "kernos/kernel/services.py", "kernos/kernel/self_admin_tools.py"),
    ),
    ReviewSlice(
        "capability-registry",
        "The three-tier capability graph + MCP client; single source of truth "
        "for capability status.",
        ("kernos/kernel/capabilities.py", "kernos/kernel/channels.py",
         "kernos/capability/"),
    ),
    ReviewSlice(
        "capability-install-bus",
        "Capability/workflow install proposals: CRB approval flow + SubstrateTools "
        "register/query facade.",
        ("kernos/kernel/crb/", "kernos/kernel/substrate_tools/"),
    ),
    # --- Workflows & cohorts ---------------------------------------------
    ReviewSlice(
        "workflows",
        "Background trigger-driven workflows on the event-stream post-flush "
        "hook; compose existing surfaces, no parallel substrate.",
        ("kernos/kernel/workflows/",),
    ),
    ReviewSlice(
        "triggers-scheduler",
        "Unified time+event trigger runtime + scheduler + webhook receiver.",
        ("kernos/kernel/triggers/", "kernos/kernel/scheduler.py",
         "kernos/kernel/webhooks/"),
    ),
    ReviewSlice(
        "drafts-primitive",
        "Persistent conversational workflow drafts (WDP).",
        ("kernos/kernel/drafts/",),
    ),
    ReviewSlice(
        "cohorts-and-drafter",
        "The cohort fan-out substrate (descriptor/registry/runner/redaction/"
        "durable substrate) + the tool-starved Drafter cohort.",
        ("kernos/kernel/cohorts/",),
    ),
    ReviewSlice(
        "four-layer-cognition",
        "The PDI four-layer path: enactment (planner/dispatcher/presence/tiers/"
        "friction-observer) + integration prep + agent/inbox registries.",
        ("kernos/kernel/enactment/", "kernos/kernel/integration/",
         "kernos/kernel/agents/"),
    ),
    # --- Build & external ------------------------------------------------
    ReviewSlice(
        "external-agents-consult",
        "The consult tool + external-agent harnesses (Codex/Claude/Gemini/Aider) "
        "+ ACPX bridge + subprocess substrate.",
        ("kernos/kernel/external_agents/", "kernos/kernel/coding_session_bridge.py"),
    ),
    ReviewSlice(
        "builders-codeexec",
        "The agentic workspace + sandboxed build/execute + file service.",
        ("kernos/kernel/workspace.py", "kernos/kernel/code_exec.py",
         "kernos/kernel/builders/", "kernos/kernel/sandbox_preamble.py",
         "kernos/kernel/files.py"),
    ),
    ReviewSlice(
        "mcp-integrations",
        "Concrete MCP-backed integration tools (Notion/Drive) + the browser MCP "
        "server.",
        ("kernos/kernel/integrations/", "kernos/browser/"),
    ),
    ReviewSlice(
        "credentials",
        "Provider + per-member workshop credential resolution, OAuth device-code/"
        "PKCE, operator onboarding CLI.",
        ("kernos/kernel/credentials.py", "kernos/kernel/credentials_member.py",
         "kernos/kernel/credentials_cli.py", "kernos/kernel/oauth_device_code.py"),
    ),
    ReviewSlice(
        "projects-long-horizon",
        "Long-horizon project tools binding a ContextSpace + pinned canvas + "
        "workflow.",
        ("kernos/kernel/projects.py",),
    ),
    # --- Friction & health -----------------------------------------------
    ReviewSlice(
        "friction-and-diagnostics",
        "The friction observer (pure sink) + reactive response loop + pattern "
        "catalog + gateway/dispatch-layer health observation.",
        ("kernos/kernel/friction.py", "kernos/kernel/friction_response.py",
         "kernos/kernel/friction_patterns.py", "kernos/kernel/pattern_heuristics.py",
         "kernos/kernel/diagnostics.py", "kernos/kernel/gateway_health.py",
         "kernos/kernel/behavioral_patterns.py",
         "kernos/setup/seed_friction_patterns.py"),
    ),
    # --- Approval & self-governance (constitutional: human-gated) --------
    ReviewSlice(
        "approval-receipts",
        "Durable approval-receipt substrate + fix authorization — the human-"
        "gating record layer.",
        ("kernos/kernel/approval_receipts.py", "kernos/kernel/fix_authorization.py"),
        constitutional=True,
    ),
    ReviewSlice(
        "improvement-loop",
        "Autonomous self-improvement: spec→impl→approval→commit→deploy→verify, "
        "with request-fidelity + proportionality; ledger, workspace, git ops, "
        "self-test gate, closure substrate.",
        ("kernos/kernel/improvement_loop_workflow.py",
         "kernos/kernel/improvement_ledger.py",
         "kernos/kernel/improvement_workspace.py", "kernos/kernel/git_operations.py",
         "kernos/kernel/self_test_gate.py", "kernos/kernel/closure_store.py",
         "kernos/kernel/workflows/self_improvement_helper.py",
         "kernos/kernel/workflows/user_initiated_improvement_helper.py",
         "kernos/kernel/workflows/loop_health_helper.py",
         "kernos/kernel/workflows/closure_tools.py",
         "kernos/kernel/workflows/autonomy_tools.py",
         "kernos/kernel/workflows/autonomy_emitters.py"),
        constitutional=True,
    ),
    ReviewSlice(
        "self-healing",
        "The bounded recovery lane: classify machinery-vs-task failure, the "
        "durable runaway bound, constitutional guard, hermetic verification. "
        "Is recovery still bounded, legible, and proportionate?",
        ("kernos/kernel/recursive_self_heal.py",),
        constitutional=True,
    ),
    ReviewSlice(
        "self-maintenance-methodology",
        "HOW KERNOS reviews + evolves itself: the daily two-lens review, the "
        "request-fidelity + proportionality gates, the evolution discipline "
        "(thoughtful, one minor step at a time). Is the way I improve myself "
        "still the healthiest, most effective approach, and does it serve the "
        "whole? Nothing is exempt — the methodology audits itself.",
        ("kernos/kernel/self_maintenance_review.py",
         "kernos/kernel/improvement_review_protocol.py",
         "specs/SELF-MAINTENANCE-REVIEW-V1.md",
         "specs/SELF-MAINTENANCE-REVIEW-V2.md",
         "specs/SELF-MAINTENANCE-REVIEW-V3.md"),
        constitutional=True,
    ),
    ReviewSlice(
        "governing-intention",
        "The constitution the rest serves: operating principles, identity, "
        "hatching guidance, conservative-by-default posture. Does the lived "
        "system still embody these, and do they still serve the whole?",
        ("kernos/kernel/template.py",),
        constitutional=True,
    ),
    ReviewSlice(
        "boot-deploy-bringup",
        "Setup, boot-guard auto-rollback, self-update, substrate bring-up, "
        "entrypoints. Boot-guard + self-update are human-gated self-modification.",
        ("kernos/setup/", "kernos/server.py", "kernos/cli.py", "kernos/chat.py",
         "kernos/repl.py", "kernos/utils.py"),
        constitutional=True,
    ),
    # --- Eval ------------------------------------------------------------
    ReviewSlice(
        "evals-soak",
        "Substrate-fidelity eval scenario runner/rubrics/reports + the soak "
        "harness.",
        ("kernos/evals/", "kernos/soak.py"),
    ),
)


def slice_for_cursor(cursor: int) -> ReviewSlice:
    return REVIEW_SLICES[cursor % len(REVIEW_SLICES)]


def load_bounded_source(
    slice_: ReviewSlice,
    repo_root: str = ".",
    *,
    max_lines_per_file: int = 160,
    max_files_per_dir: int = 4,
    max_line_chars: int = 400,
    max_total_chars: int = 24000,
) -> str:
    """Read a BOUNDED excerpt of the slice's source for a single, tool-less
    completion (the live consult has no file-reading tools). HARD caps on
    bytes, not just lines (Codex wiring-review #4): per-line char cap + a total
    char budget, streamed line-by-line so a huge single line or big file can't
    blow memory or prompt size. Every target is resolved and must stay under
    ``repo_root`` — traversal / absolute / symlinked-out paths are skipped."""
    try:
        root = Path(repo_root).resolve()
    except Exception:
        return "(invalid repo root)"
    chunks: list[str] = []
    total_chars = 0

    def _contained(p: Path) -> Path | None:
        try:
            rp = p.resolve()
            return rp if rp.is_relative_to(root) else None
        except Exception:
            return None

    for rel in slice_.paths:
        if total_chars >= max_total_chars:
            break
        target = _contained(root / rel)
        if target is None:
            continue
        files: list[Path] = []
        if rel.endswith("/") or target.is_dir():
            files = [f for f in sorted(target.glob("*.py"))[:max_files_per_dir]
                     if _contained(f) is not None]
        elif target.is_file():
            files = [target]
        for f in files:
            if total_chars >= max_total_chars:
                break
            header = f"# ── {f.relative_to(root)} ──"
            picked: list[str] = [header]
            total_chars += len(header) + 1
            n = 0
            try:
                with f.open(errors="replace") as fh:
                    for line in fh:
                        if n >= max_lines_per_file or total_chars >= max_total_chars:
                            picked.append("…")
                            break
                        snippet = line.rstrip("\n")[:max_line_chars]
                        picked.append(snippet)
                        total_chars += len(snippet) + 1
                        n += 1
            except Exception:
                continue
            chunks.append("\n".join(picked))
    out = "\n\n".join(chunks)
    # Hard ceiling: the streaming early-breaks bound memory, but a header + a
    # final line can still nudge past the budget — guarantee the RETURNED size
    # never exceeds max_total_chars (Codex confirmation #4).
    return out[:max_total_chars] if out else "(no readable source found)"


# ---------------------------------------------------------------------------
# Durable cursor + dedup state (a small JSON in data_dir)
# ---------------------------------------------------------------------------


def _state_path(data_dir: str) -> Path:
    return Path(data_dir) / "self_maintenance_review.json"


def load_state(data_dir: str) -> dict:
    p = _state_path(data_dir)
    _fresh = {"cursor": 0, "last_run_iso": "", "seen": {}, "last_reviewed": {},
              "shape_fingerprint": "", "gap_surfaced_fingerprint": ""}
    if not p.exists():
        return dict(_fresh)
    try:
        data = json.loads(p.read_text())
        for k, v in _fresh.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return dict(_fresh)


def save_state(data_dir: str, state: dict) -> None:
    _state_path(data_dir).write_text(json.dumps(state, separators=(",", ":")))


def _receipts_path(data_dir: str) -> Path:
    return Path(data_dir) / "self_maintenance_receipts.jsonl"


def append_receipt(data_dir: str, record: dict) -> None:
    """Append one JSONL audit receipt per attempted review so the founder can
    see the cadence + what KERNOS has been noticing over time (spec §3.6)."""
    try:
        line = json.dumps(record, separators=(",", ":"))
        with _receipts_path(data_dir).open("a") as fh:
            fh.write(line + "\n")
    except Exception:
        pass  # receipts are best-effort; never break the review on logging


def _hours_between(a_iso: str, b_iso: str) -> float | None:
    """Hours from a_iso to b_iso, or None if either is unparseable."""
    from datetime import datetime

    try:
        a = datetime.fromisoformat(a_iso)
        b = datetime.fromisoformat(b_iso)
    except (ValueError, TypeError):
        return None
    return (b - a).total_seconds() / 3600.0


def due_for_review(state: dict, now_iso: str) -> bool:
    last = state.get("last_run_iso") or ""
    if not last:
        return True
    gap = _hours_between(last, now_iso)
    if gap is None:
        return True
    return gap >= MIN_HOURS_BETWEEN_REVIEWS


# ---------------------------------------------------------------------------
# The two-lens review prompt + parsing
# ---------------------------------------------------------------------------


def build_review_prompt(slice_: ReviewSlice) -> str:
    """The single bounded reasoning consult: read intent + as-built, assess
    through both lenses, honour the evolution discipline."""
    paths = "\n".join(f"  - {p}" for p in slice_.paths)
    constitutional_note = ""
    if slice_.constitutional:
        constitutional_note = (
            "\nNOTE — this slice IS part of your self-governance / maintenance "
            "machinery (how you review, heal, and govern yourself). Review and "
            "ponder it as freely and honestly as any other — nothing is exempt "
            "— but any evolution here is CONSTITUTIONAL: it is human-gated and "
            "must NOT be self-applied. Frame any idea as something for the "
            "founder to weigh, not something to route into an autonomous "
            "change.\n"
        )
    return (
        "You are KERNOS performing your DAILY SELF-MAINTENANCE REVIEW of one "
        f"slice of yourself: `{slice_.name}`.\n\n"
        f"Documented intention of this slice:\n  {slice_.intent}\n"
        f"{constitutional_note}\n"
        f"As-built code to read (use your source-reading tools):\n{paths}\n\n"
        "Review through TWO lenses:\n\n"
        "1. CORRECTIVE — does the implementation still serve that intention, "
        "or has it drifted / decayed? Dead code, redundancy, an unguarded "
        "failure mode, a violated principle or covenant, a simpler/healthier "
        "shape it should already have?\n\n"
        "2. GENERATIVE (do this EVEN IF the slice is healthy) — is there a more "
        "EFFICIENT or EFFECTIVE way to handle this function? And does this "
        "function's validity and role still hold up against the OVERARCHING "
        "INTENTION OF THE WHOLE KERNOS SYSTEM — is it still pulling its weight, "
        "in the right place, worth its complexity? This is creative, holistic "
        "pondering, not bug-hunting.\n\n"
        "BUDGET: this is ONE bounded, single-pass review. Read only what you "
        "need — for a directory slice, focus on the key modules + entry points, "
        "do NOT exhaustively read every file or expand into a broad sweep.\n\n"
        "DISCIPLINE (binding): thoughtful evolution, NOT out-of-hand mutation. "
        "Propose AT MOST ONE minor, reversible, well-justified evolution idea "
        "— one step, serving the whole. If nothing is genuinely worth "
        "evolving, propose nothing. Be honest when the slice is healthy and "
        "honest when there's nothing to evolve; do NOT manufacture concerns or "
        "ideas to seem useful.\n\n"
        "End your response with EXACTLY ONE fenced JSON block of this shape:\n"
        "```json\n"
        "{\n"
        '  "overall_health": "healthy" | "minor_concerns" | "needs_attention",\n'
        '  "corrective_findings": ["short finding", ...],\n'
        '  "evolution_idea": "one minor step, or null",\n'
        '  "serves_the_whole": true | false,\n'
        '  "serves_the_whole_why": "one sentence",\n'
        '  "suggested_direction": "what (if anything) you would consider next"\n'
        "}\n"
        "```"
    )


def parse_review(text: str, slice_name: str) -> dict:
    """Parse the trailing JSON block; fall back to a freeform report so a
    malformed block never loses the review."""
    report: dict[str, Any] = {
        "slice": slice_name,
        "overall_health": "unknown",
        "corrective_findings": [],
        "evolution_idea": None,
        "serves_the_whole": None,
        "serves_the_whole_why": "",
        "suggested_direction": "",
        "parsed": False,
        "raw": text.strip()[-4000:],
    }
    block = _last_json_block(text)
    if block is not None:
        report["parsed"] = True
        for k in (
            "overall_health", "corrective_findings", "evolution_idea",
            "serves_the_whole", "serves_the_whole_why", "suggested_direction",
        ):
            if k in block:
                report[k] = block[k]
    # Discipline at the parse boundary: at most ONE evolution idea.
    ev = report.get("evolution_idea")
    if isinstance(ev, list):
        report["evolution_idea"] = ev[0] if ev else None
    # Discipline: an evolution idea is only valid if it serves the whole —
    # "serves-the-whole or it isn't raised" (Codex code-review #4).
    if report.get("serves_the_whole") is not True:
        report["evolution_idea"] = None
    if not isinstance(report.get("corrective_findings"), list):
        report["corrective_findings"] = (
            [str(report["corrective_findings"])]
            if report.get("corrective_findings") else []
        )
    return report


def _last_json_block(text: str) -> dict | None:
    import re

    matches = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    for raw in reversed(matches):
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


# ---------------------------------------------------------------------------
# Dedup + whisper framing
# ---------------------------------------------------------------------------


def _fingerprint(slice_name: str, finding: str) -> str:
    norm = " ".join(str(finding).lower().split())[:200]
    return hashlib.sha256(f"{slice_name}|{norm}".encode()).hexdigest()[:16]


def prune_seen(seen: dict, now_iso: str) -> dict:
    """Return seen with TTL-expired fingerprints dropped (pure)."""
    out = {}
    for fp, iso in (seen or {}).items():
        gap = _hours_between(iso, now_iso)
        if gap is None or gap < DEDUP_TTL_DAYS * 24:
            out[fp] = iso
    return out


def filter_seen(report: dict, seen: dict, now_iso: str) -> tuple[dict, dict]:
    """Return ``(filtered_report, fresh_fingerprints)``. Does NOT mutate
    ``seen`` — the caller commits ``fresh_fingerprints`` ONLY after a finding
    is actually surfaced (Codex code-review #2: a failed/absent whisper must
    not bury the concern for the TTL). Drops findings/idea/role-concern already
    seen within the TTL so the same observation doesn't nag every rotation."""
    slice_name = report.get("slice", "")
    fresh: dict[str, str] = {}

    kept_findings = []
    for f in report.get("corrective_findings", []):
        fp = _fingerprint(slice_name, f)
        if fp in seen or fp in fresh:
            continue
        fresh[fp] = now_iso
        kept_findings.append(f)

    kept_ev = None
    ev = report.get("evolution_idea")
    if ev:  # parse_review already enforced serves_the_whole is True
        fp = _fingerprint(slice_name, f"evolve:{ev}")
        if fp not in seen:
            fresh[fp] = now_iso
            kept_ev = ev

    # A "doesn't serve the whole" verdict is itself a dedup-able concern, so a
    # repeat minor_concerns with no FRESH detail can't keep re-whispering
    # (Codex code-review #3).
    role_fresh = False
    if report.get("serves_the_whole") is False:
        fp = _fingerprint(slice_name, "role:does_not_serve_whole")
        if fp not in seen:
            fresh[fp] = now_iso
            role_fresh = True

    out = dict(report)
    out["corrective_findings"] = kept_findings
    out["evolution_idea"] = kept_ev
    out["role_concern_fresh"] = role_fresh
    return out, fresh


def has_anything_to_say(report: dict) -> bool:
    """Honest-when-healthy: surface only on FRESH substance — a fresh finding,
    a fresh (serves-the-whole) evolution idea, or a fresh role concern. The
    bare health verdict alone is NOT a trigger, so an all-duplicate
    minor_concerns report stays quiet (Codex code-review #3)."""
    return bool(
        report.get("corrective_findings")
        or report.get("evolution_idea")
        or report.get("role_concern_fresh")
        or report.get("opportunities")     # V3: open docket items folded in
    )


def to_whisper_text(report: dict) -> str:
    """Agent-facing framing — a thought to CONSIDER, not an instruction."""
    slice_name = report.get("slice", "?")
    lines = [
        f"Daily self-review of `{slice_name}` "
        f"(health: {report.get('overall_health', 'unknown')}).",
    ]
    findings = report.get("corrective_findings") or []
    if findings:
        lines.append("Corrective notes:")
        lines.extend(f"  • {f}" for f in findings[:5])
    ev = report.get("evolution_idea")
    if ev:
        lines.append(f"One thoughtful evolution to consider: {ev}")
    if report.get("serves_the_whole") is False:
        lines.append(
            "Role check: this may not be earning its place in the whole — "
            f"{report.get('serves_the_whole_why', '')}".rstrip()
        )
    opps = report.get("opportunities") or []
    if opps:
        lines.append("")
        lines.append(
            f"Open improvement opportunities from the docket ({len(opps)} lived "
            "'this could be better' moment(s) worth working during downtime):")
        lines.extend(f"  • {o.get('desc', '')}" for o in opps[:5])
        lines.append(
            "If one is a clean single improvement, consider proposing it through "
            "the normal approval gate; otherwise leave it on the docket.")
    if report.get("constitutional"):
        lines.append(
            "This slice is governance/maintenance machinery — CONSTITUTIONAL. "
            "Raise any idea to the founder to weigh; it is human-gated, not "
            "something to self-apply."
        )
    else:
        lines.append(
            "Consider whether any of this is worth raising to the founder or "
            "proposing as a single minor improvement (through the normal gate). "
            "Thoughtful evolution, one step at a time — no obligation to act."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# V2: signal-promoted selection with a rotation floor (SELF-MAINTENANCE-REVIEW-V2)
# ---------------------------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def instance_allowed(instance_id: str) -> bool:
    """When ``KERNOS_SMR_INSTANCE_ALLOWLIST`` is set (comma-separated), the daily
    loop runs only for listed instances. Unset → all instances (single-instance
    host is the common case)."""
    allow = os.getenv("KERNOS_SMR_INSTANCE_ALLOWLIST", "").strip()
    if not allow:
        return True
    return instance_id in {x.strip() for x in allow.split(",") if x.strip()}


def resolve_target(name: str) -> "ReviewSlice | None":
    """Resolve an on-demand target slice name (case/sep-insensitive)."""
    if not name:
        return None
    key = name.strip().lower()
    norm = key.replace(" ", "-").replace("_", "-")
    for s in REVIEW_SLICES:
        if s.name.lower() in (key, norm):
            return s
    return None


def _path_matches(changed_file: str, slice_path: str) -> bool:
    """Prefix-safe: a changed file matches a slice path iff the path is that
    exact file, or the file lives under that path on a directory boundary. No
    basename matching (Codex spec review)."""
    cf = changed_file.strip().lstrip("./")
    sp = slice_path.strip().lstrip("./")
    if not cf or not sp:
        return False
    if sp.endswith("/"):
        return cf.startswith(sp)
    return cf == sp or cf.startswith(sp + "/")


def list_modules(repo_root: str) -> list:
    """Sorted relative posix paths of substantive kernos/**/*.py modules
    (__init__.py + __pycache__ excluded). Best-effort: [] on error."""
    try:
        root = Path(repo_root).resolve()
    except Exception:
        return []
    base = root / "kernos"
    if not base.is_dir():
        return []
    out: list = []
    for p in base.rglob("*.py"):
        if p.name == "__init__.py" or "__pycache__" in p.parts:
            continue
        try:
            out.append(p.resolve().relative_to(root).as_posix())
        except Exception:
            continue
    return sorted(out)


def _match_specificity(module: str, slice_path: str) -> int:
    """Length of the matching path (exact file or dir prefix) if it matches the
    module, else -1. Longer = more specific — an exact file beats a dir prefix."""
    return (len(slice_path.strip().lstrip("./"))
            if _path_matches(module, slice_path) else -1)


def assign_owners(slices, modules: list) -> dict:
    """Single-owner assignment: each module → the element whose matching path is
    MOST specific (longest); ties break by REVIEW_SLICES order. Unmatched → ''."""
    owner: dict = {}
    for m in modules:
        best_name, best_spec = "", -1
        for s in slices:
            spec = max((_match_specificity(m, p) for p in s.paths), default=-1)
            if spec > best_spec:          # strict '>' → earliest slice wins ties
                best_spec, best_name = spec, s.name
        owner[m] = best_name
    return owner


def unassigned_modules(slices, repo_root: str) -> list:
    """Substantive modules owned by no element (the coverage gap)."""
    modules = list_modules(repo_root)
    owner = assign_owners(slices, modules)
    return [m for m in modules if not owner.get(m)]


def shape_fingerprint(repo_root: str) -> str:
    """Stable hash of the SET of module paths — changes only on add/remove
    (structural), not on content edits."""
    return hashlib.sha256(
        "\n".join(list_modules(repo_root)).encode()).hexdigest()[:16]


def _coverage_gap_text(unassigned: list) -> str:
    n = len(unassigned)
    shown = unassigned[:25]
    lines = [
        f"**Coverage gap:** {n} module(s) aren't in the self-review functional "
        "map yet — which element should each belong to (or is a new element "
        "warranted)? Until slotted in, they aren't getting reviewed.",
        "",
    ]
    lines += [f"- `{m}`" for m in shown]
    if n > len(shown):
        lines.append(f"- …and {n - len(shown)} more")
    return "\n".join(lines)


def _opportunity_class(body: str) -> str:
    for line in (body or "").splitlines()[:12]:
        s = line.strip().lower()
        if s.startswith("class:"):
            return "opportunity" if s.split(":", 1)[1].strip() == "opportunity" else "error"
    return "error"


def _opportunity_desc(body: str) -> str:
    lines = (body or "").splitlines()
    for i, ln in enumerate(lines):
        if ln.strip().lower().startswith("## description"):
            for nxt in lines[i + 1:i + 5]:
                if nxt.strip():
                    return nxt.strip()[:200]
    for ln in lines:
        if ln.startswith("# Friction Report:"):
            return ln.split(":", 1)[1].strip()[:200]
    return "(opportunity)"


def open_opportunities(data_dir: str, now_iso: str, *, slice_paths=(),
                       limit: int = 5, window_days: int = 30) -> list:
    """The improvement docket: OPEN opportunity-class friction notes, newest
    first within the window, biased toward those touching the reviewed element's
    paths. Returns [{desc, signature, mtime}]. Best-effort — [] on any error."""
    try:
        fdir = Path(data_dir) / "diagnostics" / "friction"
        if not fdir.is_dir():
            return []
        files = sorted(fdir.glob("*.md"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:200]
    except Exception:
        return []
    try:
        from datetime import datetime
        cutoff = datetime.fromisoformat(now_iso).timestamp() - window_days * 86400
    except Exception:
        cutoff = 0.0
    norm_paths = [p.strip().lstrip("./").lower() for p in slice_paths]
    relevant, others = [], []
    for p in files:
        try:
            if p.stat().st_mtime < cutoff:
                continue
            body = p.read_text(errors="replace")
        except Exception:
            continue
        if _opportunity_class(body) != "opportunity":
            continue
        item = {"desc": _opportunity_desc(body), "signature": p.stem,
                "mtime": p.stat().st_mtime}
        low = body.lower()
        touches = any(sp and sp in low for sp in norm_paths)
        (relevant if touches else others).append(item)
    return (relevant + others)[:limit]


def _changed_files_since(repo_root: str, window_days: int) -> set:
    try:
        import subprocess
        out = subprocess.run(
            ["git", "-C", repo_root, "log", f"--since={window_days} days ago",
             "--name-only", "--pretty=format:"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return set()
        return {ln.strip() for ln in out.stdout.splitlines() if ln.strip()}
    except Exception:
        return set()


def _churn_scores(slices, repo_root: str, window_days: int, owner: dict) -> dict:
    """Per-element count of changed modules in the window, attributed to each
    module's SINGLE owner (no double-counting). Best-effort."""
    scores = {s.name: 0 for s in slices}
    for f in _changed_files_since(repo_root, window_days):
        nm = owner.get(f)
        if nm in scores:
            scores[nm] += 1
    return scores


def _friction_scores(slices, data_dir: str, window_days: int,
                     now_iso: str, owner: dict) -> dict:
    """Per-element count of recent friction reports (bounded read, newest first,
    within the window) whose text references one of the element's OWNED modules
    (single-owner) or the element name on a word boundary. Each element credited
    at most once per report. Best-effort: missing dir / error → all zero."""
    scores = {s.name: 0 for s in slices}
    try:
        fdir = Path(data_dir) / "diagnostics" / "friction"
        if not fdir.is_dir():
            return scores
        files = sorted(fdir.glob("*.md"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:20]
    except Exception:
        return scores
    try:
        from datetime import datetime
        cutoff = datetime.fromisoformat(now_iso).timestamp() - window_days * 86400
    except Exception:
        cutoff = 0.0
    modules = list(owner.keys())
    names = [s.name for s in slices]
    for f in files:
        try:
            if f.stat().st_mtime < cutoff:
                continue
            text = f.read_text(errors="replace")[:4000].lower()
        except Exception:
            continue
        credited: set = set()
        for m in modules:
            if m.lower() in text:
                nm = owner.get(m)
                if nm and nm not in credited:
                    scores[nm] += 1
                    credited.add(nm)
        for nm in names:
            if nm not in credited and re.search(
                    r"\b" + re.escape(nm.lower()) + r"\b", text):
                scores[nm] += 1
                credited.add(nm)
    return scores


def collect_signal_scores(slices, repo_root: str, data_dir: str,
                          window_days: int, cap: int, now_iso: str) -> dict:
    """Combined, per-source-isolated, capped signal score per element. Uses the
    single-owner assignment so a changed/referenced module promotes exactly one
    element (no double-counting — Codex spec review)."""
    try:
        owner = assign_owners(slices, list_modules(repo_root))
    except Exception:
        owner = {}
    try:
        churn = _churn_scores(slices, repo_root, window_days, owner)
    except Exception:
        churn = {}
    try:
        fric = _friction_scores(slices, data_dir, window_days, now_iso, owner)
    except Exception:
        fric = {}
    return {s.name: min(cap, churn.get(s.name, 0) + fric.get(s.name, 0))
            for s in slices}


def _age_days(last_reviewed: dict, name: str, now_iso: str) -> float:
    iso = last_reviewed.get(name)
    if not iso:
        return float("inf")
    try:
        from datetime import datetime
        delta = datetime.fromisoformat(now_iso) - datetime.fromisoformat(iso)
        return max(0.0, delta.total_seconds() / 86400.0)
    except Exception:
        return float("inf")


def select_slice(slices, state: dict, signal_scores: dict,
                 now_iso: str) -> "ReviewSlice":
    """Signal-promoted pick with a hard coverage floor. Step 1: any slice aged
    past COVERAGE_MAX_DAYS is picked over everything (stalest first) — the
    rotation guarantee, bounding worst-case time-to-review. Step 2 (nothing past
    the floor): argmax of W_SIGNAL*signal + W_STALE*age_days. Tie-break by
    REVIEW_SLICES index in both steps."""
    last = state.get("last_reviewed")
    if not isinstance(last, dict):      # malformed/legacy container → treat as empty
        last = {}
    cov = float(_env_int("KERNOS_SMR_COVERAGE_MAX_DAYS", 10))
    ages = [(s, _age_days(last, s.name, now_iso)) for s in slices]
    floored = [(s, a) for s, a in ages if a >= cov]
    if floored:
        maxage = max(a for _, a in floored)
        for s, a in ages:               # REVIEW_SLICES order → lowest index wins
            if a >= cov and a == maxage:
                return s
    ws = _env_float("KERNOS_SMR_W_SIGNAL", 6.0)
    wst = _env_float("KERNOS_SMR_W_STALE", 1.0)
    best, best_score = slices[0], None
    for s, a in ages:
        score = ws * signal_scores.get(s.name, 0) + wst * min(a, cov)
        if best_score is None or score > best_score:   # strict → lowest index on ties
            best, best_score = s, score
    return best


# ---------------------------------------------------------------------------
# Orchestration — idle-aware, once/24h, behind the kill switch
# ---------------------------------------------------------------------------


async def maybe_run_daily(
    *,
    data_dir: str,
    now_iso: str,
    consult_fn: Callable[..., Any],   # async (prompt, slice) -> str
    whisper_fn: Callable[..., Any] | None = None,  # async (text, report) -> None
    busy: bool = False,
    force: bool = False,
    target: str | None = None,        # V2: on-demand specific slice
    repo_root: str = "",              # V2: for churn signal (defaults to env)
) -> dict:
    """Run today's review. Returns a result dict with ``outcome``: disabled |
    busy | not_due | reviewed_quiet | reviewed_surfaced | parse_error | error.

    ``force`` is for an OPERATOR-initiated on-demand review (e.g. a slash
    command): it bypasses the kill switch, the busy check, and the once/24h
    gate, because the operator is explicitly asking and is present to watch.
    The autonomous daily loop never sets force, so it stays fully gated."""
    # Resolve an explicit target UP FRONT — an unknown target must short-circuit
    # before any state read, gate, or consult (Codex code-review must-fix).
    target_slice = None
    if target:
        target_slice = resolve_target(target)
        if target_slice is None:
            return {"outcome": "unknown_target", "target": target,
                    "valid": [s.name for s in REVIEW_SLICES]}
    if not force:
        if not is_enabled():
            return {"outcome": "disabled"}
        if busy:
            # Idle-aware: never compete with a live turn or in-flight attempt.
            return {"outcome": "busy"}

    state = load_state(data_dir)
    if not force and not due_for_review(state, now_iso):
        return {"outcome": "not_due"}

    # --- coverage-gap check (V3): structural-only, surface once per shape change
    # Record shape_fingerprint on every successful scan; surface a gap note only
    # when the shape changed AND there's an unassigned module AND we haven't
    # already surfaced for this shape; set gap_surfaced_fingerprint only on a
    # successful surface (a failed surface re-tries next shape-change tick).
    try:
        _repo = repo_root or os.getenv("KERNOS_REPO_DIR", ".")
        _fp = shape_fingerprint(_repo)
        if _fp:
            state["shape_fingerprint"] = _fp   # record every successful scan
            # Gate surfacing on the SURFACED fingerprint, not on "changed since
            # last scan" — else a failed surface (or absent whisper_fn) records
            # the shape but never re-tries (Codex code-review must-fix).
            if (_fp != state.get("gap_surfaced_fingerprint", "")
                    and whisper_fn is not None):
                _gaps = unassigned_modules(REVIEW_SLICES, _repo)
                if _gaps:
                    try:
                        await whisper_fn(_coverage_gap_text(_gaps),
                                         {"kind": "coverage_gap",
                                          "slice": "coverage-gap",
                                          "unassigned": _gaps[:50]})
                        state["gap_surfaced_fingerprint"] = _fp
                    except Exception:
                        pass  # gap_surfaced unchanged → retries next tick
            save_state(data_dir, state)
    except Exception:
        pass

    # --- slice selection (V2) ---------------------------------------------
    if target_slice is not None:
        slice_ = target_slice
        bypass_dedup = True  # an explicitly-targeted review returns raw findings
    else:
        bypass_dedup = False
        root = repo_root or os.getenv("KERNOS_REPO_DIR", ".")
        try:
            signal_scores = collect_signal_scores(
                REVIEW_SLICES, root, data_dir,
                _env_int("KERNOS_SMR_SIGNAL_WINDOW_DAYS", 7),
                _env_int("KERNOS_SMR_SIGNAL_CAP", 5), now_iso)
        except Exception:
            signal_scores = {s.name: 0 for s in REVIEW_SLICES}
        slice_ = select_slice(REVIEW_SLICES, state, signal_scores, now_iso)
    try:
        # consult_fn receives (prompt, slice) — the slice carries the paths a
        # live, tool-less single completion needs to pre-load source.
        text = await consult_fn(build_review_prompt(slice_), slice_)
    except Exception as exc:
        append_receipt(data_dir, {
            "ts": now_iso, "slice": slice_.name, "outcome": "error",
            "error": str(exc)[:200],
        })
        return {"outcome": "error", "slice": slice_.name, "error": str(exc)[:200]}

    report = parse_review(text or "", slice_.name)
    report["constitutional"] = slice_.constitutional

    # Parse failure: the review didn't produce a usable verdict. Do NOT count
    # it as a clean reviewed slice or advance the cursor (Codex code-review #5)
    # — rate-limit (stamp the run) and re-review this same slice next cycle.
    if not report.get("parsed"):
        state["last_run_iso"] = now_iso
        save_state(data_dir, state)
        append_receipt(data_dir, {
            "ts": now_iso, "slice": slice_.name, "outcome": "parse_error",
        })
        return {"outcome": "parse_error", "slice": slice_.name, "report": report}

    pruned = prune_seen(state.get("seen", {}), now_iso)
    if bypass_dedup:
        # Targeted on-demand review: raw findings, no seen-filter, and don't
        # commit fresh fingerprints — you asked about THIS slice, you get its
        # real current state even if a finding was surfaced recently.
        filtered, fresh = dict(report), {}
    else:
        filtered, fresh = filter_seen(report, pruned, now_iso)
    filtered["constitutional"] = slice_.constitutional
    # V3: fold open improvement-docket opportunities (biased to this element)
    # into what the review surfaces — so a healthy slice with lived
    # 'this could be better' moments still raises them.
    try:
        filtered["opportunities"] = open_opportunities(
            data_dir, now_iso, slice_paths=slice_.paths)
    except Exception:
        filtered["opportunities"] = []

    surfaced = False
    if has_anything_to_say(filtered) and whisper_fn is not None:
        try:
            await whisper_fn(to_whisper_text(filtered), filtered)
            surfaced = True
        except Exception:
            surfaced = False

    # Commit fresh fingerprints ONLY after a successful surface, so a failed or
    # absent whisper doesn't bury the concern for the TTL (Codex #2). A quiet
    # healthy slice (nothing fresh) commits just the pruned set.
    state["seen"] = {**pruned, **fresh} if surfaced else pruned
    # V2: record per-slice coverage (replaces the cursor advance). Stamped only
    # on a clean reviewed slice — error/parse_error returned earlier without
    # touching last_reviewed, so a failed read keeps the slice eligible.
    state.setdefault("last_reviewed", {})[slice_.name] = now_iso
    state["last_run_iso"] = now_iso
    save_state(data_dir, state)

    append_receipt(data_dir, {
        "ts": now_iso, "slice": slice_.name,
        "outcome": "reviewed_surfaced" if surfaced else "reviewed_quiet",
        "overall_health": filtered.get("overall_health"),
        "n_findings": len(filtered.get("corrective_findings") or []),
        "has_evolution_idea": bool(filtered.get("evolution_idea")),
        "constitutional": slice_.constitutional,
    })

    return {
        "outcome": "reviewed_surfaced" if surfaced else "reviewed_quiet",
        "slice": slice_.name,
        "report": filtered,
    }


RUN_SELF_REVIEW_TOOL: dict = {
    "name": "run_self_review",
    "description": (
        "Run a self-maintenance review right now and report what you find. "
        "You review ONE rotating slice of your own code through a corrective "
        "lens (drift, decay, unguarded edges vs. the documented intention) and "
        "a generative lens (is there a better way; does this still serve the "
        "whole?), then tell the owner the result in your own voice. This is the "
        "same review the owner can trigger with /selfreview, exposed to you as "
        "a tool so you can actually run it when the owner asks you to review "
        "yourself — not just describe it. Owner-only, and it runs even when the "
        "daily background review is disabled. Reflection only: it surfaces a "
        "note to consider and never changes code on its own (any change still "
        "flows through the approval-gated improve_kernos loop). "
        "Pass an optional `target` to review a specific section by name "
        "(e.g. 'dispatch-gate', 'reasoning', 'memory', 'message-pipeline', "
        "'improvement-loop', 'governing-intention'); omit it and KERNOS picks "
        "the section most worth a look right now (recent friction or code "
        "churn), with a rotation floor so nothing goes unreviewed. An unknown "
        "target lists the valid section names."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "Optional. The section to review by name. Omit to let "
                    "KERNOS choose the most relevant section."
                ),
            },
        },
    },
}


__all__ = [
    "RUN_SELF_REVIEW_TOOL",
    "is_enabled",
    "REVIEW_SLICES",
    "ReviewSlice",
    "slice_for_cursor",
    "load_bounded_source",
    "load_state",
    "save_state",
    "append_receipt",
    "prune_seen",
    "due_for_review",
    "build_review_prompt",
    "parse_review",
    "filter_seen",
    "has_anything_to_say",
    "to_whisper_text",
    "maybe_run_daily",
]

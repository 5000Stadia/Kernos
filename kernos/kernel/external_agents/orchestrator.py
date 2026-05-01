"""Consultation orchestrator — the engine-side glue between the
agent-facing ``consult`` tool surface and the harness substrate.

Responsibilities the harness shouldn't carry:

1. **Reentrancy guard.** Read the ContextVar-based calling context
   and depth; reject early before subprocess spawn.
2. **Workspace-dir resolution.** Per-call override → per-instance
   config → repo-root detection → process cwd. Optional
   allowlist enforcement.
3. **Session-id sanitization.** Hash the agent-supplied raw id to
   safe hex BEFORE handing to the harness so harness paths stay
   bounded (Codex spec-review fold #7 + AC19).
4. **Native-session-ref lookup.** For codex specifically, find the
   most recent ``consultation_log`` row with the same session_id
   and pass its ``native_session_ref`` via
   ``harness_options['prior_native_session_ref']`` so the next
   call resumes Codex's thread.
5. **Audit lifecycle.** Begin a row in ``consultation_log``,
   surface the row id to triage, mark
   succeeded / failed / timed_out per outcome, persist
   native_session_ref + truncated flag.

All three failure modes (HarnessUnavailable, ConsultationTimeout,
ConsultationFailed) are captured into the log row before the
exception propagates so triage can find the failed row.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kernos.kernel.external_agents.consultation_log import (
    ConsultationLog,
)
from kernos.kernel.external_agents.errors import (
    ConsultationFailed,
    ConsultationTimeout,
    ExternalAgentError,
    HarnessUnavailable,
    WorkspaceNotAllowed,
)
from kernos.kernel.external_agents.harness import ConsultResult
from kernos.kernel.external_agents.reentrancy import (
    enter_consult,
    exit_consult,
)
from kernos.kernel.external_agents.registry import HarnessRegistry
from kernos.kernel.external_agents.subprocess_substrate import (
    sanitize_session_id,
)

logger = logging.getLogger(__name__)


# Hard cap on per-call timeout. Per spec D5: max 1800s = 30min.
_TIMEOUT_SECONDS_MAX = 1800
_TIMEOUT_SECONDS_DEFAULT = 600


@dataclass(frozen=True)
class WorkspacePolicy:
    """Workspace-dir resolution policy. Engine bring-up populates
    ``default_dir`` from instance config; ``allowlist`` enforces
    the spec D4 path constraint when configured."""

    default_dir: Path | None = None
    allowlist: tuple[Path, ...] = ()


class ConsultationOrchestrator:
    """Owns the consult lifecycle: gate → resolve workspace → log →
    invoke harness → record outcome."""

    def __init__(
        self,
        *,
        registry: HarnessRegistry,
        log: ConsultationLog,
        workspace_policy: WorkspacePolicy | None = None,
    ) -> None:
        self._registry = registry
        self._log = log
        self._policy = workspace_policy or WorkspacePolicy()

    async def consult(
        self,
        *,
        instance_id: str,
        member_id: str,
        harness: str,
        question: str,
        context: dict | str | None = None,
        session_id_raw: str = "",
        workspace_dir: str | Path | None = None,
        timeout_seconds: int | None = None,
        harness_options: dict[str, Any] | None = None,
    ) -> ConsultResult:
        """Agent-facing entry point. Translates the tool input into
        a fully-validated harness invocation; records the lifecycle
        in consultation_log; returns the harness's :class:`ConsultResult`
        on success or raises one of the
        :mod:`kernos.kernel.external_agents.errors` types."""

        # ---- 1. Reentrancy gate ----------------------------------
        token = enter_consult()  # may raise ReentrancyBlocked / DepthExceeded

        try:
            # ---- 2. Sanitize + resolve --------------------------
            session_id_hex = sanitize_session_id(session_id_raw)
            workspace = self._resolve_workspace(workspace_dir)
            timeout_clamped = self._clamp_timeout(timeout_seconds)
            options = dict(harness_options or {})
            context_text = _serialize_context(context)

            # Look up prior native_session_ref for resume-capable
            # harnesses (codex). Other harnesses ignore the option.
            # Codex post-impl review fold: scope the lookup by
            # (instance_id, member_id, harness) to prevent
            # cross-tenant / cross-member native-session reuse if
            # session_id_hex collides (sanitized hash collisions are
            # negligible but instance+member scoping is the right
            # boundary regardless).
            if session_id_hex and harness in ("codex",):
                prior = await self._lookup_prior_native_ref(
                    session_id_hex=session_id_hex,
                    instance_id=instance_id,
                    member_id=member_id,
                    harness=harness,
                )
                if prior:
                    options.setdefault(
                        "prior_native_session_ref", prior,
                    )

            # ---- 3. Resolve harness -----------------------------
            backend = self._registry.get(harness, mode="consult")

            # ---- 4. Begin log row -------------------------------
            consultation_id = await self._log.begin(
                instance_id=instance_id,
                member_id=member_id,
                harness=harness,
                session_id=session_id_hex,
                question=question,
                context=context_text,
                metadata={
                    "session_id_raw": session_id_raw,
                    "harness_options_keys": sorted(options.keys()),
                },
                workspace_dir=str(workspace) if workspace else "",
                timeout_seconds=timeout_clamped,
            )

            # ---- 5. Invoke harness + record outcome -------------
            # AC7/AC19: every mark_* call carries the begin-time
            # metadata (session_id_raw, harness_options_keys) along
            # with any new fields, so the raw id and option set
            # remain queryable on succeeded/failed/timed_out rows.
            base_meta = {
                "session_id_raw": session_id_raw,
                "harness_options_keys": sorted(options.keys()),
            }
            try:
                result = await backend.consult(
                    question=question,
                    context=context if context is not None else "",
                    session_id=session_id_hex,
                    workspace_dir=workspace or Path.cwd(),
                    timeout_seconds=timeout_clamped,
                    harness_options=options,
                )
            except ConsultationTimeout as exc:
                await self._log.mark_timed_out(
                    consultation_id=consultation_id,
                    timeout_seconds=timeout_clamped,
                    metadata={**base_meta, "error_repr": repr(exc)},
                )
                raise
            except ConsultationFailed as exc:
                await self._log.mark_failed(
                    consultation_id=consultation_id,
                    error=str(exc),
                    exit_status=getattr(exc, "exit_status", 0) or 0,
                    metadata={**base_meta, "error_repr": repr(exc)},
                )
                raise
            except HarnessUnavailable as exc:
                await self._log.mark_failed(
                    consultation_id=consultation_id,
                    error=f"HarnessUnavailable: {exc}",
                    metadata={**base_meta, "error_repr": repr(exc)},
                )
                raise

            success_meta = {**base_meta, **(dict(result.metadata or {}))}
            await self._log.mark_succeeded(
                consultation_id=consultation_id,
                response=result.response,
                native_session_ref=result.native_session_ref,
                truncated=result.truncated,
                metadata=success_meta,
                exit_status=int(success_meta.get("exit_status", 0) or 0),
            )
            return result
        finally:
            exit_consult(token)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_workspace(
        self, workspace_dir: str | Path | None,
    ) -> Path | None:
        """Resolution order per spec D4: per-call override →
        per-instance default → detected repo root → cwd. Allowlist
        enforced when configured."""
        if workspace_dir:
            resolved = Path(workspace_dir).expanduser().resolve()
        elif self._policy.default_dir is not None:
            resolved = self._policy.default_dir.resolve()
        else:
            resolved = self._detect_repo_root() or Path(os.getcwd()).resolve()

        if self._policy.allowlist:
            if not _is_under_any(resolved, self._policy.allowlist):
                allowlist_str = ", ".join(str(p) for p in self._policy.allowlist)
                raise WorkspaceNotAllowed(
                    f"workspace_dir {resolved} is not under any "
                    f"allowlisted prefix: {allowlist_str}"
                )
        return resolved

    @staticmethod
    def _detect_repo_root() -> Path | None:
        cwd = Path(os.getcwd()).resolve()
        for candidate in (cwd, *cwd.parents):
            if (candidate / ".git").exists():
                return candidate
        return None

    @staticmethod
    def _clamp_timeout(value: int | None) -> int:
        if value is None:
            return _TIMEOUT_SECONDS_DEFAULT
        return max(1, min(_TIMEOUT_SECONDS_MAX, int(value)))

    async def _lookup_prior_native_ref(
        self,
        *,
        session_id_hex: str,
        instance_id: str,
        member_id: str,
        harness: str,
    ) -> str:
        """Find the most-recent successful row's native_session_ref
        for a session. Used so codex can resume the captured
        thread_id on subsequent calls. Scoped by
        (instance_id, member_id, harness) so a session_id collision
        across tenants/members/harnesses cannot leak a native ref."""
        rows = await self._log.find_by_session(session_id=session_id_hex)
        # Iterate from newest to oldest; pick the first non-empty
        # native_session_ref from a row that succeeded AND matches
        # the calling tenant/member/harness.
        for row in reversed(rows):
            if (
                row.status == "succeeded"
                and row.native_session_ref
                and row.instance_id == instance_id
                and row.member_id == member_id
                and row.harness == harness
            ):
                return row.native_session_ref
        return ""


def _serialize_context(context: dict | str | None) -> str:
    if context is None or context == "":
        return ""
    if isinstance(context, str):
        return context
    try:
        return json.dumps(context, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        # Fall back to repr; the harness's own _compose_prompt also
        # validates, so this is just for logging.
        return repr(context)


def _is_under_any(path: Path, prefixes: tuple[Path, ...]) -> bool:
    resolved = path.resolve()
    for prefix in prefixes:
        try:
            resolved.relative_to(prefix.resolve())
            return True
        except ValueError:
            continue
    return False


__all__ = [
    "ConsultationOrchestrator",
    "WorkspacePolicy",
]

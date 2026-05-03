"""Install hook runner — shared between `kernos setup` and self_update.

Per Section 7 of the INSTALL-FOR-STOCK-CONNECTORS spec (the design review edit #2):
the hook runner is a SHARED module called from both
`kernos setup` (fresh install) and `self_update.py` (updates).
self_update only runs on updates; fresh-install bootstrapping
needs its own entry point. Same runner; same hook set; same
status store.

Contract:

  - HookDescriptor declares hook_id, optional phase + ordering,
    idempotent flag (must be True in v1), check + apply callables.
  - Registration rejects non-idempotent hooks, hooks declaring
    they attempt credential-key generation, and unresolvable
    ordering cycles.
  - Execution honors topological order (`order_after`); fall-back
    is registration order. Each hook runs check first; apply only
    runs if check returns needs_apply: True.
  - Failed hook is loud (error to operator) and non-fatal (other
    hooks continue; install completes; failed hook persisted in
    install health store).
  - Status writes are atomic per hook; the status store is shared
    across hook runs so `kernos services info` install_health can
    summarize.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from kernos.utils import utc_now


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InstallHookError(ValueError):
    """Raised on hook descriptor validation or registry errors."""


# ---------------------------------------------------------------------------
# Phase + result types
# ---------------------------------------------------------------------------


class HookPhase(str, Enum):
    """When a hook runs.

    `pre_setup` and `post_setup` bracket the first-run flow; both
    fire from `kernos setup`. `post_update` fires from
    self_update.py after pip install. None means "any phase" — the
    hook runs whenever the runner is invoked regardless of phase.
    """

    PRE_SETUP = "pre_setup"
    POST_SETUP = "post_setup"
    POST_UPDATE = "post_update"


@dataclass(frozen=True)
class CheckResult:
    needs_apply: bool
    status: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ApplyResult:
    success: bool
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HookContext:
    """Per-run context handed to each hook.

    Carries the data_dir + phase + a freeze of which call site is
    invoking the runner (setup / self_update / explicit). Hooks
    that need to read service state load it themselves via
    ServiceStateStore — the context doesn't pre-fetch substrates.

    Notably absent: any reference to MemberCredentialStore or the
    credential key. Hooks must not generate credential keys; the
    context withholds the substrate that would let them.
    """

    data_dir: Path
    phase: HookPhase | None
    invoked_by: str  # "kernos_setup" | "self_update" | "explicit"


# ---------------------------------------------------------------------------
# HookDescriptor
# ---------------------------------------------------------------------------


HookCheckCallable = Callable[[HookContext], CheckResult]
HookApplyCallable = Callable[[HookContext], ApplyResult]


@dataclass(frozen=True)
class HookDescriptor:
    """Declarative install hook record.

    `phase` constrains when the hook runs; None = always.
    `order_after` lists hook_ids that must run before this one.
    `idempotent` MUST be True in v1; non-idempotent hooks are
    rejected at registration. `attempts_credential_key_generation`
    MUST be False; True at registration is the spec's
    declaration-time refusal of key-gen hooks (Section 6).

    `check` runs first; if it returns needs_apply: True, `apply`
    runs. `check` returning needs_apply: False means the substrate
    is already in the desired state — the hook records
    skipped_check and apply does not fire.
    """

    hook_id: str
    check: HookCheckCallable
    apply: HookApplyCallable
    phase: HookPhase | None = None
    order_after: tuple[str, ...] = ()
    idempotent: bool = True
    attempts_credential_key_generation: bool = False


# ---------------------------------------------------------------------------
# HookStatus + status store
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookStatus:
    hook_id: str
    last_run_at: str
    last_outcome: str  # "success" | "failed" | "skipped_check"
    last_error: str = ""
    consecutive_failures: int = 0
    last_duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "last_run_at": self.last_run_at,
            "last_outcome": self.last_outcome,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "last_duration_ms": self.last_duration_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HookStatus":
        return cls(
            hook_id=str(data.get("hook_id", "")),
            last_run_at=str(data.get("last_run_at", "")),
            last_outcome=str(data.get("last_outcome", "")),
            last_error=str(data.get("last_error", "")),
            consecutive_failures=int(data.get("consecutive_failures", 0)),
            last_duration_ms=int(data.get("last_duration_ms", 0)),
        )


_STATUS_RELATIVE_PATH = ("install", "hook_status.json")
_STATUS_SCHEMA_VERSION = 1


class HookStatusStore:
    """Persistent record of hook outcomes across runs.

    Atomic writes (write-temp + rename) so concurrent CLI / setup
    invocations never see torn JSON. Read-through cache so a single
    runner doesn't re-parse the file per hook.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir).expanduser().resolve()
        self._path = self._data_dir.joinpath(*_STATUS_RELATIVE_PATH)
        self._cache: dict[str, HookStatus] | None = None
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.exists()

    def list_all(self) -> tuple[HookStatus, ...]:
        with self._lock:
            return tuple(
                sorted(self._load().values(), key=lambda s: s.hook_id)
            )

    def get(self, hook_id: str) -> HookStatus | None:
        with self._lock:
            return self._load().get(hook_id)

    def record(self, status: HookStatus) -> None:
        with self._lock:
            current = dict(self._load())
            current[status.hook_id] = status
            self._write(current)
            self._cache = current

    def invalidate(self) -> None:
        with self._lock:
            self._cache = None

    def _load(self) -> dict[str, HookStatus]:
        if self._cache is not None:
            return self._cache
        if not self._path.exists():
            self._cache = {}
            return self._cache
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InstallHookError(
                f"failed to read hook_status at {self._path}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise InstallHookError(
                f"hook_status.json must be a JSON object; got "
                f"{type(raw).__name__}"
            )
        entries = raw.get("hooks", [])
        parsed: dict[str, HookStatus] = {}
        for entry in entries or []:
            status = HookStatus.from_dict(entry)
            parsed[status.hook_id] = status
        self._cache = parsed
        return self._cache

    def _write(self, mapping: dict[str, HookStatus]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "schema_version": _STATUS_SCHEMA_VERSION,
            "hooks": [s.to_dict() for s in mapping.values()],
        }
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            tmp_path.write_text(
                json.dumps(document, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(tmp_path, self._path)
        except OSError as exc:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:  # pragma: no cover
                pass
            raise InstallHookError(
                f"failed to write hook_status at {self._path}: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Hook registry
# ---------------------------------------------------------------------------


class HookRegistry:
    """Design Review-controlled boot-time registration of hooks.

    Per Section 7: registry rejects non-idempotent hooks, hooks
    declaring credential-key generation, and unresolvable
    `order_after` cycles. Hooks register from boot; subsequent
    specs may add hooks via this registry.
    """

    def __init__(self) -> None:
        self._hooks: list[HookDescriptor] = []
        self._ids: set[str] = set()

    def register(self, descriptor: HookDescriptor) -> None:
        if not isinstance(descriptor, HookDescriptor):
            raise InstallHookError(
                f"register expected HookDescriptor; got "
                f"{type(descriptor).__name__}"
            )
        if not descriptor.hook_id or not descriptor.hook_id.strip():
            raise InstallHookError("hook_id must be a non-empty string")
        if descriptor.hook_id in self._ids:
            raise InstallHookError(
                f"hook {descriptor.hook_id!r} is already registered"
            )
        if not descriptor.idempotent:
            raise InstallHookError(
                f"hook {descriptor.hook_id!r}: idempotent must be True "
                f"in v1. Non-idempotent hooks are rejected at registration "
                f"because the runner re-runs them on every setup/update "
                f"invocation."
            )
        if descriptor.attempts_credential_key_generation:
            raise InstallHookError(
                f"hook {descriptor.hook_id!r}: install hooks MAY NOT "
                f"generate, rotate, or overwrite the credential key. "
                f"Validate permissions and surface instructions only. "
                f"Generation happens on explicit operator events "
                f"(`kernos setup`, `kernos credentials onboard`, first "
                f"credential write) — never from a hook."
            )
        if not callable(descriptor.check) or not callable(descriptor.apply):
            raise InstallHookError(
                f"hook {descriptor.hook_id!r}: check and apply must be "
                f"callables"
            )
        self._hooks.append(descriptor)
        self._ids.add(descriptor.hook_id)

    def list_hooks(self) -> tuple[HookDescriptor, ...]:
        return tuple(self._hooks)

    def has(self, hook_id: str) -> bool:
        return hook_id in self._ids

    def __len__(self) -> int:
        return len(self._hooks)


# ---------------------------------------------------------------------------
# Topological ordering
# ---------------------------------------------------------------------------


def topological_order(
    hooks: Iterable[HookDescriptor],
) -> list[HookDescriptor]:
    """Return hooks in execution order honoring `order_after`.

    Cycles raise InstallHookError. Hooks without ordering
    declarations preserve registration order (stable sort).
    """
    descriptors = list(hooks)
    by_id = {d.hook_id: d for d in descriptors}
    ordering_index = {d.hook_id: i for i, d in enumerate(descriptors)}

    # Validate `order_after` references exist.
    for d in descriptors:
        for dep in d.order_after:
            if dep not in by_id:
                raise InstallHookError(
                    f"hook {d.hook_id!r} declares order_after "
                    f"{dep!r} which is not registered"
                )

    # Kahn's algorithm with deterministic tie-breaking on
    # registration order.
    in_degree: dict[str, int] = {d.hook_id: 0 for d in descriptors}
    edges: dict[str, set[str]] = {d.hook_id: set() for d in descriptors}
    for d in descriptors:
        for dep in d.order_after:
            edges[dep].add(d.hook_id)
            in_degree[d.hook_id] += 1

    ready = sorted(
        [hid for hid, deg in in_degree.items() if deg == 0],
        key=lambda h: ordering_index[h],
    )
    out: list[HookDescriptor] = []
    while ready:
        hid = ready.pop(0)
        out.append(by_id[hid])
        for downstream in sorted(
            edges[hid], key=lambda h: ordering_index[h]
        ):
            in_degree[downstream] -= 1
            if in_degree[downstream] == 0:
                ready.append(downstream)
        ready.sort(key=lambda h: ordering_index[h])

    if len(out) != len(descriptors):
        unresolved = sorted(set(by_id) - {d.hook_id for d in out})
        raise InstallHookError(
            f"hook ordering cycle detected; unresolved hooks: "
            f"{', '.join(unresolved)}"
        )
    return out


# ---------------------------------------------------------------------------
# HookRunner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookRunReport:
    """Summary of a single hook-runner invocation.

    Carries per-hook outcome + total counts so install_health on
    `kernos services info` can render a one-line summary plus the
    failed-hook list without re-reading the status store.
    """

    invoked_by: str
    phase: HookPhase | None
    succeeded: tuple[str, ...]
    failed: tuple[str, ...]
    skipped_check: tuple[str, ...]

    @property
    def total(self) -> int:
        return len(self.succeeded) + len(self.failed) + len(self.skipped_check)


# Audit emit shape mirrors the V1 / cohort runner pattern: a callback
# accepting a dict. Subsequent specs wire to the audit-log substrate.
HookAuditEmitter = Callable[[dict[str, Any]], Awaitable[None] | None]


class HookRunner:
    """Sync runner — executes registered hooks for a phase.

    Hooks are sync callables (consistent with self_update which is
    sync throughout). The audit_emitter may be sync or async; if
    async, the runner schedules it on a fresh event loop only when
    one isn't running. Tests pass a sync callable.
    """

    def __init__(
        self,
        *,
        registry: HookRegistry,
        status_store: HookStatusStore,
        audit_emitter: HookAuditEmitter | None = None,
    ) -> None:
        self._registry = registry
        self._status_store = status_store
        self._audit_emitter = audit_emitter

    def run(
        self,
        *,
        phase: HookPhase | None,
        invoked_by: str,
        data_dir: str | Path,
    ) -> HookRunReport:
        context = HookContext(
            data_dir=Path(data_dir).expanduser().resolve(),
            phase=phase,
            invoked_by=invoked_by,
        )
        applicable = [
            h for h in self._registry.list_hooks()
            if h.phase is None or h.phase is phase
        ]
        ordered = topological_order(applicable)

        succeeded: list[str] = []
        failed: list[str] = []
        skipped_check: list[str] = []

        for descriptor in ordered:
            outcome, error, duration_ms = self._run_one(descriptor, context)
            if outcome == "success":
                succeeded.append(descriptor.hook_id)
            elif outcome == "failed":
                failed.append(descriptor.hook_id)
            else:
                skipped_check.append(descriptor.hook_id)

            self._record_status(
                descriptor=descriptor,
                outcome=outcome,
                error=error,
                duration_ms=duration_ms,
            )
            self._maybe_emit_audit(
                descriptor=descriptor,
                context=context,
                outcome=outcome,
                error=error,
                duration_ms=duration_ms,
            )

        return HookRunReport(
            invoked_by=invoked_by,
            phase=phase,
            succeeded=tuple(succeeded),
            failed=tuple(failed),
            skipped_check=tuple(skipped_check),
        )

    # ----- internals -----

    def _run_one(
        self, descriptor: HookDescriptor, context: HookContext
    ) -> tuple[str, str, int]:
        """Execute a single hook. Returns (outcome, error_summary, duration_ms).

        outcome ∈ {"success", "failed", "skipped_check"}.
        """
        import time as _time
        started = _time.monotonic()
        # Runtime guard: any hook attempting credential-key generation
        # is refused via the thread-local flag in credentials_member.
        from kernos.kernel import credentials_member as _cm
        with _cm.refuse_credential_key_generation(
            f"install-hook:{descriptor.hook_id}"
        ):
            try:
                check = descriptor.check(context)
            except Exception as exc:
                duration = _ms_since(started)
                return (
                    "failed",
                    _summarize_exception(exc),
                    duration,
                )
            if not isinstance(check, CheckResult):
                duration = _ms_since(started)
                return (
                    "failed",
                    f"hook check returned {type(check).__name__}; "
                    f"expected CheckResult",
                    duration,
                )
            if not check.needs_apply:
                duration = _ms_since(started)
                return ("skipped_check", "", duration)
            try:
                apply_result = descriptor.apply(context)
            except Exception as exc:
                duration = _ms_since(started)
                return (
                    "failed",
                    _summarize_exception(exc),
                    duration,
                )
        if not isinstance(apply_result, ApplyResult):
            return (
                "failed",
                f"hook apply returned {type(apply_result).__name__}; "
                f"expected ApplyResult",
                _ms_since(started),
            )
        if not apply_result.success:
            return (
                "failed",
                apply_result.message or "hook reported failure with no message",
                _ms_since(started),
            )
        return ("success", "", _ms_since(started))

    def _record_status(
        self,
        *,
        descriptor: HookDescriptor,
        outcome: str,
        error: str,
        duration_ms: int,
    ) -> None:
        previous = self._status_store.get(descriptor.hook_id)
        consecutive = 0
        if outcome == "failed":
            consecutive = (previous.consecutive_failures + 1) if previous else 1
        new = HookStatus(
            hook_id=descriptor.hook_id,
            last_run_at=utc_now(),
            last_outcome=outcome,
            last_error=error,
            consecutive_failures=consecutive,
            last_duration_ms=duration_ms,
        )
        try:
            self._status_store.record(new)
        except InstallHookError:
            logger.warning(
                "INSTALL_HOOK_STATUS_WRITE_FAILED: hook=%s",
                descriptor.hook_id,
                exc_info=True,
            )

    def _maybe_emit_audit(
        self,
        *,
        descriptor: HookDescriptor,
        context: HookContext,
        outcome: str,
        error: str,
        duration_ms: int,
    ) -> None:
        if self._audit_emitter is None:
            return
        entry = {
            "audit_category": "install.hook_executed",
            "hook_id": descriptor.hook_id,
            "phase": context.phase.value if context.phase else "any",
            "invoked_by": context.invoked_by,
            "outcome": outcome,
            "duration_ms": duration_ms,
            "error_summary": error,
        }
        try:
            result = self._audit_emitter(entry)
            if hasattr(result, "__await__"):
                # Async emitter: schedule on a fresh loop only if no
                # loop is currently running. Sync callers (self_update)
                # don't have an event loop.
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(result)  # type: ignore[arg-type]
                except RuntimeError:
                    asyncio.run(result)  # type: ignore[arg-type]
        except Exception:
            logger.warning(
                "INSTALL_HOOK_AUDIT_EMIT_FAILED: hook=%s",
                descriptor.hook_id,
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ms_since(start: float) -> int:
    import time as _time
    return max(0, int((_time.monotonic() - start) * 1000))


def _summarize_exception(exc: BaseException) -> str:
    cls = type(exc).__name__
    msg = str(exc)
    out = f"{cls}: {msg}" if msg else cls
    if len(out) > 500:
        out = out[:499] + "…"
    return out


__all__ = [
    "ApplyResult",
    "CheckResult",
    "HookAuditEmitter",
    "HookContext",
    "HookDescriptor",
    "HookPhase",
    "HookRegistry",
    "HookRunReport",
    "HookRunner",
    "HookStatus",
    "HookStatusStore",
    "InstallHookError",
    "topological_order",
]

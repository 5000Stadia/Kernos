"""First-run setup flow.

Per Section 5 of the INSTALL-FOR-STOCK-CONNECTORS spec.

This module is the explicit setup surface. It is NOT invoked from
the daemon or message-handler boot path — those treat
`service_state.json` absence as "all disabled, surface nothing"
and continue without blocking. The operator runs this through
`kernos setup` (top-level subcommand) when they're ready to
configure which services are enabled.

Behavior matrix:

  | Context                                    | Behavior                                      |
  |--------------------------------------------|-----------------------------------------------|
  | TTY, no service_state.json                 | Interactive prompt; persist with source=setup |
  | Non-TTY, no service_state.json             | Auto-bootstrap all-disabled, log instructions |
  | --non-interactive --enable-services X,Y    | Enable only the named services                |
  | --non-interactive --all-enabled            | Enable every shipped service                  |
  | Existing service_state.json (any context)  | Re-prompt on TTY; non-TTY warns and exits     |

No TUI dependency in v1; stdlib `input()` and `print()` only
(the design review edit #7 / spec acceptance criterion 10).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from kernos.kernel.services import ServiceRegistry
from kernos.setup.service_state import (
    ServiceState,
    ServiceStateError,
    ServiceStateSource,
    ServiceStateStore,
    ServiceStateUpdatedBy,
)
from kernos.utils import utc_now


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FirstRunSetupError(RuntimeError):
    """Raised when first-run setup hits an unrecoverable condition."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_data_dir(args: argparse.Namespace | None = None) -> Path:
    raw = ""
    if args is not None:
        raw = getattr(args, "data_dir", "") or ""
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(os.environ.get("KERNOS_DATA_DIR", "./data")).resolve()


def _load_stock_registry() -> ServiceRegistry:
    registry = ServiceRegistry()
    stock_dir = (
        Path(__file__).resolve().parent.parent
        / "kernel"
        / "services"
    )
    if stock_dir.exists():
        registry.load_stock_dir(stock_dir)
    return registry


def is_interactive(
    stdin: object | None = None,
    stdout: object | None = None,
) -> bool:
    """True iff both stdin and stdout are connected to a TTY.

    Both checks matter: stdout-only TTY happens with redirected
    input (script piping), which we treat as non-interactive even
    though `print()` lands on the user's terminal.
    """
    _stdin = stdin if stdin is not None else sys.stdin
    _stdout = stdout if stdout is not None else sys.stdout
    try:
        return bool(
            getattr(_stdin, "isatty", lambda: False)()
            and getattr(_stdout, "isatty", lambda: False)()
        )
    except Exception:
        return False


def _service_id_set(raw: str) -> set[str]:
    """Parse a comma-separated --enable-services string into a set."""
    if not raw:
        return set()
    return {part.strip() for part in raw.split(",") if part.strip()}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FirstRunResult:
    """Outcome of a first-run flow invocation.

    Carries enough detail for the CLI / hook runner / tests to
    decide what to log / what to print / what return code to use.
    """

    ran_interactive: bool
    bootstrapped_disabled: bool  # non-TTY auto-bootstrap path
    enabled_service_ids: tuple[str, ...]
    disabled_service_ids: tuple[str, ...]
    skipped_existing_state: bool  # service_state.json already present
    state_path: Path

    @property
    def changed_state(self) -> bool:
        return (
            self.ran_interactive
            or self.bootstrapped_disabled
            or bool(self.enabled_service_ids)
            or bool(self.disabled_service_ids and not self.skipped_existing_state)
        )


# ---------------------------------------------------------------------------
# Core flow
# ---------------------------------------------------------------------------


def run_first_run(
    *,
    data_dir: Path,
    non_interactive: bool = False,
    enable_services: Iterable[str] = (),
    all_enabled: bool = False,
    interactive_override: bool | None = None,
    input_fn=input,
    print_fn=print,
) -> FirstRunResult:
    """Execute the first-run flow.

    `interactive_override` exists for tests; production callers
    leave it at None and the function uses is_interactive(). The
    `input_fn` and `print_fn` knobs let tests inject deterministic
    I/O without touching sys.stdin / sys.stdout.

    Decision flow:

      1. Load registered services from the stock registry.
      2. Compute the effective context (interactive / non-interactive).
      3. Load existing state. If present:
           - On TTY: re-prompt (operator can re-toggle).
           - Non-TTY: skip and report "skipped_existing_state".
      4. Persist the chosen states atomically.
    """
    registry = _load_stock_registry()
    services = sorted(registry.list_services(), key=lambda s: s.service_id)
    if not services:
        # Nothing to set up. Persist an empty state so the daemon
        # boot path can detect "setup ran" vs "never ran."
        store = ServiceStateStore(data_dir)
        store.replace_all(())
        return FirstRunResult(
            ran_interactive=False,
            bootstrapped_disabled=False,
            enabled_service_ids=(),
            disabled_service_ids=(),
            skipped_existing_state=False,
            state_path=store.path,
        )

    store = ServiceStateStore(data_dir)
    state_existed = store.exists()
    interactive = (
        interactive_override
        if interactive_override is not None
        else is_interactive()
    )

    # Non-interactive paths take priority — explicit flags trump TTY.
    explicit_set = _service_id_set(",".join(enable_services))
    if non_interactive or all_enabled or explicit_set:
        return _run_non_interactive(
            store=store,
            services=services,
            enable_set=explicit_set,
            all_enabled=all_enabled,
            print_fn=print_fn,
        )

    if not interactive:
        return _run_headless_bootstrap(
            store=store,
            services=services,
            print_fn=print_fn,
        )

    return _run_interactive_prompt(
        store=store,
        services=services,
        state_existed=state_existed,
        input_fn=input_fn,
        print_fn=print_fn,
    )


# ---------------------------------------------------------------------------
# Path: non-interactive (explicit flags)
# ---------------------------------------------------------------------------


def _run_non_interactive(
    *,
    store: ServiceStateStore,
    services: Sequence,
    enable_set: set[str],
    all_enabled: bool,
    print_fn,
) -> FirstRunResult:
    if all_enabled:
        chosen_enabled = {s.service_id for s in services}
    else:
        chosen_enabled = enable_set

    unknown = chosen_enabled - {s.service_id for s in services}
    if unknown:
        raise FirstRunSetupError(
            f"--enable-services names unknown service(s): "
            f"{', '.join(sorted(unknown))}. Run `kernos services list` "
            f"to see the available services."
        )

    states = []
    timestamp = utc_now()
    for descriptor in services:
        states.append(
            ServiceState(
                service_id=descriptor.service_id,
                enabled=descriptor.service_id in chosen_enabled,
                source=ServiceStateSource.SETUP,
                updated_at=timestamp,
                updated_by=ServiceStateUpdatedBy.OPERATOR,
                reason="non-interactive setup",
            )
        )
    store.replace_all(states)

    enabled = sorted(chosen_enabled)
    disabled = sorted(s.service_id for s in services if s.service_id not in chosen_enabled)
    print_fn(
        f"Setup complete (non-interactive). Enabled: {len(enabled)}, "
        f"Disabled: {len(disabled)}."
    )
    return FirstRunResult(
        ran_interactive=False,
        bootstrapped_disabled=False,
        enabled_service_ids=tuple(enabled),
        disabled_service_ids=tuple(disabled),
        skipped_existing_state=False,
        state_path=store.path,
    )


# ---------------------------------------------------------------------------
# Path: headless bootstrap (non-TTY, no flags)
# ---------------------------------------------------------------------------


def _run_headless_bootstrap(
    *,
    store: ServiceStateStore,
    services: Sequence,
    print_fn,
) -> FirstRunResult:
    """Auto-bootstrap with all services disabled. Log enable instructions.

    Per Section 5: daemon never blocks. The headless path completes
    without prompting and never raises. The operator runs `kernos
    services enable <id>` (or `kernos setup` interactively later)
    to flip services on.
    """
    if store.exists():
        # Nothing to do — existing state preserved. Operator may
        # have set this manually; respect it.
        message = (
            "Setup skipped: service_state already exists at "
            f"{store.path}. Run `kernos setup` interactively to "
            "review, or use `kernos services list` to see current "
            "state."
        )
        print_fn(message)
        logger.info("FIRST_RUN: skipped (state exists)")
        return FirstRunResult(
            ran_interactive=False,
            bootstrapped_disabled=False,
            enabled_service_ids=(),
            disabled_service_ids=(),
            skipped_existing_state=True,
            state_path=store.path,
        )

    states = []
    timestamp = utc_now()
    for descriptor in services:
        states.append(
            ServiceState(
                service_id=descriptor.service_id,
                enabled=False,
                source=ServiceStateSource.SETUP,
                updated_at=timestamp,
                updated_by=ServiceStateUpdatedBy.SYSTEM,
                reason="auto-bootstrap (non-interactive context)",
            )
        )
    store.replace_all(states)

    print_fn(
        "Services are disabled by default in non-interactive contexts.\n"
        "  Enable a service:    kernos services enable <service_id>\n"
        "  Run setup:           kernos setup\n"
        f"  Available services:  {', '.join(s.service_id for s in services)}"
    )
    logger.info(
        "FIRST_RUN: headless bootstrap (all-disabled) for %d services",
        len(services),
    )
    return FirstRunResult(
        ran_interactive=False,
        bootstrapped_disabled=True,
        enabled_service_ids=(),
        disabled_service_ids=tuple(s.service_id for s in services),
        skipped_existing_state=False,
        state_path=store.path,
    )


# ---------------------------------------------------------------------------
# Path: interactive prompt (TTY)
# ---------------------------------------------------------------------------


def _run_interactive_prompt(
    *,
    store: ServiceStateStore,
    services: Sequence,
    state_existed: bool,
    input_fn,
    print_fn,
) -> FirstRunResult:
    if state_existed:
        print_fn(
            "Existing service state detected. You can re-toggle each "
            "service below."
        )
    else:
        print_fn("Welcome to Kernos setup. Select services to enable.")
    print_fn(
        "  Press [y]es / [n]o for each. Press Enter to accept the "
        "suggested default."
    )
    print_fn("")

    state_by_id = {s.service_id: s for s in store.list_all()}
    chosen_enabled: set[str] = set()
    chosen_disabled: set[str] = set()

    for descriptor in services:
        existing = state_by_id.get(descriptor.service_id)
        default_enabled = existing.enabled if existing else False
        suggestion = "Y/n" if default_enabled else "y/N"
        prompt = (
            f"  {descriptor.service_id} "
            f"({descriptor.display_name}, auth_type="
            f"{descriptor.auth_type.value}) — enable? [{suggestion}] "
        )
        try:
            answer = input_fn(prompt)
        except (EOFError, KeyboardInterrupt):
            # Treat early termination as "abort setup, keep existing
            # state if any". We don't write anything in that case.
            print_fn("\nSetup aborted; no changes saved.")
            return FirstRunResult(
                ran_interactive=True,
                bootstrapped_disabled=False,
                enabled_service_ids=(),
                disabled_service_ids=(),
                skipped_existing_state=state_existed,
                state_path=store.path,
            )
        normalized = (answer or "").strip().lower()
        if normalized == "":
            decision = default_enabled
        elif normalized in {"y", "yes"}:
            decision = True
        elif normalized in {"n", "no"}:
            decision = False
        else:
            print_fn(
                f"    (unrecognised answer {answer!r}; treating as "
                f"{'yes' if default_enabled else 'no'} per default)"
            )
            decision = default_enabled

        if decision:
            chosen_enabled.add(descriptor.service_id)
        else:
            chosen_disabled.add(descriptor.service_id)

    timestamp = utc_now()
    states = [
        ServiceState(
            service_id=descriptor.service_id,
            enabled=descriptor.service_id in chosen_enabled,
            source=ServiceStateSource.SETUP,
            updated_at=timestamp,
            updated_by=ServiceStateUpdatedBy.OPERATOR,
            reason="interactive setup",
        )
        for descriptor in services
    ]
    store.replace_all(states)

    print_fn("")
    print_fn(
        f"Setup complete. Enabled: {len(chosen_enabled)}, "
        f"Disabled: {len(chosen_disabled)}."
    )
    if chosen_enabled:
        print_fn(
            "  Onboard credentials per service: "
            "`kernos credentials onboard --service <service_id>`"
        )

    logger.info(
        "FIRST_RUN: interactive complete (enabled=%d disabled=%d)",
        len(chosen_enabled),
        len(chosen_disabled),
    )

    return FirstRunResult(
        ran_interactive=True,
        bootstrapped_disabled=False,
        enabled_service_ids=tuple(sorted(chosen_enabled)),
        disabled_service_ids=tuple(sorted(chosen_disabled)),
        skipped_existing_state=False,
        state_path=store.path,
    )


# ---------------------------------------------------------------------------
# CLI entry — invoked by `kernos setup` (no subtarget)
# ---------------------------------------------------------------------------


def cmd_setup_first_run(args: argparse.Namespace) -> int:
    """Top-level `kernos setup` (with no subtarget) handler.

    Distinct from `kernos setup llm` (that goes through
    kernos.setup.console). The first-run flow handles service-state
    bootstrapping; LLM configuration is its own surface.

    The shared install-hook runner (Section 7) fires twice: pre-
    setup (substrate bootstrap, e.g., create data/install/ dir) and
    post-setup (validate credential dirs, etc.). Failed hooks are
    loud but non-fatal; the install completes and `kernos services
    info` install_health surfaces the failures.
    """
    data_dir = _resolve_data_dir(args)
    _run_install_hooks(data_dir=data_dir, phase="pre_setup")

    try:
        result = run_first_run(
            data_dir=data_dir,
            non_interactive=getattr(args, "non_interactive", False),
            enable_services=_service_id_set(
                getattr(args, "enable_services", "") or ""
            ),
            all_enabled=getattr(args, "all_enabled", False),
        )
    except FirstRunSetupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except ServiceStateError as exc:
        print(f"ERROR writing service state: {exc}", file=sys.stderr)
        return 1

    if result.bootstrapped_disabled:
        print(f"  State written: {result.state_path}")

    _run_install_hooks(data_dir=data_dir, phase="post_setup")
    return 0


def _run_install_hooks(*, data_dir: Path, phase: str) -> None:
    """Invoke the shared install-hook runner. Best-effort.

    Failed hooks are reported to stdout so the operator sees them,
    persisted to the hook_status store, and never fatal — install
    proceeds regardless.
    """
    from kernos.setup.install_hooks import (
        HookPhase,
        HookRunner,
        HookStatusStore,
        build_default_registry,
    )
    registry = build_default_registry()
    status_store = HookStatusStore(data_dir)
    runner = HookRunner(registry=registry, status_store=status_store)
    phase_enum = HookPhase(phase) if phase in (p.value for p in HookPhase) else None
    report = runner.run(
        phase=phase_enum,
        invoked_by="kernos_setup",
        data_dir=data_dir,
    )
    if report.failed:
        print(
            f"  install hooks ({phase}): {len(report.failed)} failed; "
            f"see `kernos services info <id>` install_health for details"
        )
    elif report.total > 0:
        print(
            f"  install hooks ({phase}): {len(report.succeeded)} succeeded, "
            f"{len(report.skipped_check)} skipped"
        )


def add_first_run_args(parser: argparse.ArgumentParser) -> None:
    """Register --non-interactive / --enable-services / --all-enabled
    flags + --data-dir on the given parser. Used by both the
    top-level `kernos setup` parser and the install-hook
    integration in C5.
    """
    parser.add_argument(
        "--data-dir",
        default="",
        help="Kernos data directory (default: KERNOS_DATA_DIR or './data').",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help=(
            "Skip interactive prompts. Combine with --enable-services "
            "or --all-enabled to write state directly."
        ),
    )
    parser.add_argument(
        "--enable-services",
        default="",
        help=(
            "Comma-separated service ids to enable. Implies "
            "--non-interactive."
        ),
    )
    parser.add_argument(
        "--all-enabled",
        action="store_true",
        help=(
            "Enable every shipped service (development / test installs). "
            "Implies --non-interactive."
        ),
    )


__all__ = [
    "FirstRunResult",
    "FirstRunSetupError",
    "add_first_run_args",
    "cmd_setup_first_run",
    "is_interactive",
    "run_first_run",
]

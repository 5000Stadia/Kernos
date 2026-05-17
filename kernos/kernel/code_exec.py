"""Sandboxed code execution engine for the Agentic Workspace.

Runs Python code in a subprocess scoped to the current space's directory.
Hard security walls: tenant isolation, no parent env inheritance, no network
credentials, no Kernos internals access.

Two install-time toggles shape execution:

``KERNOS_WORKSPACE_SCOPE`` (``isolated`` default / ``unleashed``) — when
isolated, a Python preamble monkey-patches filesystem entry points so reads
and writes that resolve outside the space directory raise ``PermissionError``.
Honest ceiling: catches accidental and casually-malicious access, not a
determined adversary with ctypes or a native binary.

``KERNOS_BUILDER`` (``native`` default / ``aider`` / ``claude-code`` / ``codex``)
— which backend fulfills the build/execute request. ``native`` runs the local
subprocess below; the other three route through the builder dispatcher and
currently return a structured not-implemented response from a shared stub
until their adapter batches ship.
"""
import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from kernos.utils import _safe_name

logger = logging.getLogger(__name__)

MAX_TIMEOUT = 300  # seconds
DEFAULT_TIMEOUT = 30
STDOUT_BUDGET = 4000  # chars
STDERR_BUDGET = 2000

#: Valid values for ``KERNOS_WORKSPACE_SCOPE``. Order matters for error
#: messages.
VALID_SCOPES: tuple[str, ...] = ("isolated", "unleashed")

#: Where the sandbox preamble lives inside a space directory. Dot-prefixed so
#: it stays out of casual listings (``Path.glob('*.py')``, ``ls`` without
#: ``-a``, etc.) while remaining plain Python accessible to the subprocess.
_PREAMBLE_SUBDIR = ".kernos_sandbox"
_PREAMBLE_FILENAME = "sandbox_preamble.py"


EXECUTE_CODE_TOOL = {
    "name": "execute_code",
    "description": (
        "Execute Python code in a sandboxed environment. "
        "Code runs in the current space's directory with access "
        "to files in this space. "
        "Use for: building tools, processing data, generating "
        "reports, testing implementations, running computations. "
        "Output (stdout/stderr) is returned as the tool result."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python code to execute.",
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "Maximum execution time in seconds. Default 30, max 300.",
            },
            "write_file": {
                "type": "string",
                "description": (
                    "Optional: write the code to this filename in "
                    "the space's directory before executing. Useful "
                    "for creating persistent scripts and tools."
                ),
            },
            "backend": {
                "type": "string",
                "enum": ["native", "aider"],
                "description": (
                    "Optional: which builder backend executes the "
                    "code. Defaults to KERNOS_BUILDER (typically "
                    "'native'). 'native' runs the code in a "
                    "sandboxed Python subprocess. 'aider' routes to "
                    "the Aider CLI for task-shaped edits to specific "
                    "files. NOTE: this is sandboxed code execution, "
                    "not a broker-dispatch channel — to hand off "
                    "implementation work to Claude Code or Codex "
                    "(broker role), use the `consult` tool. The "
                    "'claude-code' and 'codex' execute_code backends "
                    "are reserved namespace, NOT yet implemented."
                ),
            },
        },
        "required": ["code"],
    },
}


def _sandbox_env(space_dir: str) -> dict[str, str]:
    """Build a restricted environment for code execution.

    The subprocess inherits NOTHING from the parent process except
    what is explicitly set here. No API keys, tokens, or credentials.
    """
    return {
        "PATH": "/usr/bin:/usr/local/bin",
        "HOME": space_dir,
        "PYTHONPATH": space_dir,
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
    }


def _validate_filename(name: str) -> bool:
    """Validate a filename against path traversal."""
    if not name or not name.strip():
        return False
    if "/" in name or "\\" in name or ".." in name:
        return False
    if name.startswith("."):
        return False
    return True


def _effective_scope() -> str:
    """Read ``KERNOS_WORKSPACE_SCOPE`` with default + normalization."""
    raw = (os.getenv("KERNOS_WORKSPACE_SCOPE", "") or "isolated").strip().lower()
    if raw not in VALID_SCOPES:
        # Server startup validation rejects invalid values up front; this
        # belt-and-suspenders path falls back to the safer default so a
        # misconfigured worker never silently loses scope enforcement.
        logger.warning(
            "WORKSPACE_SCOPE: unknown value %r — defaulting to 'isolated'", raw,
        )
        return "isolated"
    return raw


def _effective_builder() -> str:
    """Read ``KERNOS_BUILDER`` with default + normalization."""
    raw = (os.getenv("KERNOS_BUILDER", "") or "native").strip().lower()
    return raw


def _install_preamble(space_dir: str) -> str:
    """Copy sandbox_preamble.py into the space's ``.kernos_sandbox/`` dir.

    Returns the absolute path to the preamble's parent directory, which is
    what the launcher puts on ``sys.path``.
    """
    preamble_dir = os.path.join(space_dir, _PREAMBLE_SUBDIR)
    os.makedirs(preamble_dir, exist_ok=True)
    src = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "sandbox_preamble.py",
    )
    dst = os.path.join(preamble_dir, _PREAMBLE_FILENAME)
    # Copy unconditionally; preamble is tiny and this keeps the sandbox
    # dir in sync with the shipped module even after an upgrade.
    shutil.copyfile(src, dst)
    return preamble_dir


def _resolve_sandbox_extra_read_dirs(data_dir: str) -> list[str]:
    """Compute the sandbox read allow-list for execute_code subprocesses.

    Default: the bot's ``data_dir`` so Kernos can introspect its own
    persisted state (instance.db, event stream, canvases, conversation
    logs) from within an execute_code script. Without this, the agent
    has no path to self-inspect substrate from a conversation.

    Operators can extend via ``KERNOS_SANDBOX_EXTRA_READ_DIRS``
    (comma-separated absolute paths). Writes stay strictly
    scope-confined to the space directory; this allow-list is
    read-only per :func:`sandbox_preamble.install_scope_wrapper`'s
    contract.
    """
    extras: list[str] = [os.path.abspath(data_dir)]
    raw = os.getenv("KERNOS_SANDBOX_EXTRA_READ_DIRS", "")
    for p in raw.split(","):
        p = p.strip()
        if p:
            extras.append(os.path.abspath(p))
    return extras


def _build_launcher(
    space_dir: str, preamble_dir: str, source_path: str,
    extra_read_dirs: list[str] | None = None,
) -> str:
    """Render the launcher body that installs scope and execs the user source."""
    extras_repr = repr(list(extra_read_dirs or []))
    return (
        "import sys\n"
        f"sys.path.insert(0, {preamble_dir!r})\n"
        "from sandbox_preamble import install_scope_wrapper\n"
        f"install_scope_wrapper({space_dir!r}, {extras_repr})\n"
        f"with open({source_path!r}) as _kernos_launcher_f:\n"
        "    _kernos_launcher_src = _kernos_launcher_f.read()\n"
        "exec(\n"
        f"    compile(_kernos_launcher_src, {source_path!r}, 'exec'),\n"
        "    {'__name__': '__main__', '__file__': "
        f"{source_path!r}" "},\n"
        ")\n"
    )


async def _run_native(
    instance_id: str,
    space_id: str,
    code: str,
    timeout_seconds: int = DEFAULT_TIMEOUT,
    write_file_name: str | None = None,
    data_dir: str = "./data",
    scope: str | None = None,
) -> dict[str, Any]:
    """Native backend: run Python in a sandboxed subprocess.

    Honors ``scope`` (``isolated`` applies the preamble wrapper). Called by
    :class:`kernos.kernel.builders.native.NativeBuilder`.

    Returns dict with: success, stdout, stderr, exit_code, error (optional).
    """
    # Clamp timeout
    timeout_seconds = max(1, min(MAX_TIMEOUT, timeout_seconds))
    effective_scope = (scope or _effective_scope()).lower()
    if effective_scope not in VALID_SCOPES:
        effective_scope = "isolated"

    # Resolve the space's working directory. MUST be absolute: the
    # launcher renders these paths as string literals into the
    # subprocess source, then the subprocess runs with ``cwd=space_dir``.
    # A relative path resolves once during launcher generation (parent
    # cwd) and a second time inside the subprocess (cwd=space_dir),
    # producing a doubled path like ``<abs>/files/data/.../files/...``
    # that doesn't exist — yielding ``ModuleNotFoundError: sandbox_preamble``
    # at line 3 of the launcher. Tests use pytest's absolute ``tmp_path``
    # so this never surfaces in CI; production uses ``./data`` and
    # silently breaks every isolated-mode execute_code call.
    data_dir = os.path.abspath(data_dir)
    space_dir = str(
        Path(data_dir) / _safe_name(instance_id) / "spaces" / space_id / "files"
    )
    os.makedirs(space_dir, exist_ok=True)

    # Paths we create during this call, tracked so finally: can clean up.
    source_path: str = ""
    launcher_path: str = ""
    is_temp_source = False

    try:
        # 1. Persist user code to source file. write_file_name path keeps
        #    the persisted copy clean — no preamble boilerplate in it.
        if write_file_name:
            if not _validate_filename(write_file_name):
                return {
                    "success": False,
                    "error": "Invalid filename — no path separators or '..' allowed",
                    "exit_code": -1,
                }
            source_path = os.path.join(space_dir, write_file_name)
            with open(source_path, "w", encoding="utf-8") as f:
                f.write(code)
            logger.info(
                "CODE_EXEC_WRITE: space=%s file=%s bytes=%d",
                space_id, write_file_name, len(code),
            )
        else:
            fd, source_path = tempfile.mkstemp(
                prefix="_kernos_src_", suffix=".py", dir=space_dir,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(code)
            is_temp_source = True

        # 2. Choose exec_path. In isolated mode, a launcher wraps the source
        #    with preamble-load + exec; in unleashed mode, the source runs
        #    directly.
        if effective_scope == "isolated":
            preamble_dir = _install_preamble(space_dir)
            launcher_body = _build_launcher(
                space_dir=space_dir,
                preamble_dir=preamble_dir,
                source_path=source_path,
                extra_read_dirs=_resolve_sandbox_extra_read_dirs(data_dir),
            )
            fd, launcher_path = tempfile.mkstemp(
                prefix="_kernos_launcher_", suffix=".py", dir=space_dir,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(launcher_body)
            exec_path = launcher_path
        else:
            exec_path = source_path

        _display_name = write_file_name or os.path.basename(source_path)
        logger.info(
            "CODE_EXEC: space=%s file=%s scope=%s timeout=%d",
            space_id, _display_name, effective_scope, timeout_seconds,
        )

        # 3. Subprocess exec.
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: subprocess.run(
            ["python3", exec_path],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=space_dir,
            env=_sandbox_env(space_dir),
        ))

        stdout = result.stdout[:STDOUT_BUDGET] if result.stdout else ""
        stderr = result.stderr[:STDERR_BUDGET] if result.stderr else ""

        logger.info(
            "CODE_EXEC_RESULT: space=%s success=%s exit_code=%d stdout_len=%d stderr_len=%d",
            space_id, result.returncode == 0, result.returncode,
            len(result.stdout or ""), len(result.stderr or ""),
        )

        out: dict[str, Any] = {
            "success": result.returncode == 0,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": result.returncode,
        }
        if len(result.stdout or "") > STDOUT_BUDGET:
            out["stdout_truncated"] = True
            out["full_stdout_chars"] = len(result.stdout)
        return out

    except subprocess.TimeoutExpired:
        logger.warning(
            "CODE_EXEC_TIMEOUT: space=%s timeout=%ds file=%s",
            space_id, timeout_seconds, write_file_name or "temp",
        )
        return {
            "success": False,
            "error": f"Execution timed out after {timeout_seconds}s",
            "exit_code": -1,
        }
    except Exception as exc:
        logger.warning("CODE_EXEC_ERROR: space=%s error=%s", space_id, exc)
        return {
            "success": False,
            "error": str(exc)[:500],
            "exit_code": -1,
        }
    finally:
        # Always delete launcher; delete source only if it was a temp.
        for _path, _should_delete in (
            (launcher_path, bool(launcher_path)),
            (source_path, is_temp_source and bool(source_path)),
        ):
            if _should_delete and os.path.exists(_path):
                try:
                    os.unlink(_path)
                except OSError as exc:
                    logger.warning(
                        "CODE_EXEC_CLEANUP: failed to delete %s: %s",
                        _path, exc,
                    )


async def execute_code(
    instance_id: str,
    space_id: str,
    code: str,
    timeout_seconds: int = DEFAULT_TIMEOUT,
    write_file_name: str | None = None,
    data_dir: str = "./data",
    backend: str | None = None,
) -> dict[str, Any]:
    """Execute code via a builder backend.

    Backend selection (EXTERNAL-AGENT-CONSULTATION v1 C5):

    1. **Per-call ``backend`` param** wins when supplied. Lets the
       agent pick "native" / "aider" / "claude-code" / "codex" per
       call instead of relying on the global env var.
    2. **``KERNOS_BUILDER`` env var** preserved as the fallback when
       ``backend`` is not supplied. Existing operator workflows
       continue working unchanged.
    3. ``KERNOS_WORKSPACE_SCOPE`` continues to govern sandbox scope
       independently of backend choice.

    Returns dict with: success, stdout, stderr, exit_code, error (optional).
    """
    # Local import to keep this module import-safe for tests that don't need
    # the builder surface yet.
    from kernos.kernel.builders import UnknownBuilderError, get_builder

    scope = _effective_scope()
    builder_name = (
        backend.strip().lower() if backend else _effective_builder()
    )

    try:
        backend_obj = get_builder(builder_name)
    except UnknownBuilderError as exc:
        # Startup validation catches the env-var path; per-call
        # invalid backend lands here. Either way, return a
        # structured error rather than crashing the turn.
        logger.warning("CODE_EXEC: %s", exc)
        return {
            "success": False,
            "error": str(exc),
            "exit_code": -1,
        }

    result = await backend_obj.build(
        instance_id=instance_id,
        space_id=space_id,
        code=code,
        timeout_seconds=timeout_seconds,
        write_file_name=write_file_name,
        data_dir=data_dir,
        scope=scope,
    )
    return result.to_dict()

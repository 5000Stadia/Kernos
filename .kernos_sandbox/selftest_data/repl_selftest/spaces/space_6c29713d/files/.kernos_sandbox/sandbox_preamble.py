"""Sandbox preamble — install filesystem scope wrappers.

This module is copied into the space directory at subprocess launch time.
It MUST be self-contained: no imports from the kernos package, because the
subprocess runs without the Kernos package on its PYTHONPATH.

The wrappers confine filesystem access to a configured space directory,
with a limited read-only carve-out for Python runtime paths (the
interpreter's own ``sys.prefix``/``sys.base_prefix`` and paths passed via
the ``extra_read_dirs`` argument). Writes remain strictly confined to the
space directory regardless of the read allow-list.

The carve-out exists because Python itself performs filesystem reads at
import time — ``importlib.metadata`` reads distribution ``METADATA`` files
from ``site-packages/``, ``pkg_resources`` walks the runtime tree, various
packages probe ``sys.path`` entries for data files. An overly-strict scope
blocks all of these and the subprocess can't even complete its own imports.
Allowing reads (not writes) from Python runtime locations is the minimum
relaxation that makes third-party tools (Aider, etc.) actually run.

Honest ceiling: this catches accidental and casually-malicious filesystem
access. A determined adversary who can run Python can bypass it via ctypes,
raw syscalls, native binaries, or simply by un-patching the wrappers. Real
isolation against hostile code needs OS-level primitives (chroot, Landlock,
container). This module is a *scope* boundary against workspace spillover,
not a security boundary against hostile code.

Wrapped entry points:
  - builtins.open          — mode-aware: writes always scope-confined,
                             reads allowed from extra_read_dirs too
  - os.open                — flag-aware (O_WRONLY/O_RDWR/O_CREAT imply write)
  - os.listdir             — read-only by nature; extra_read_dirs allowed
  - os.scandir             — read-only by nature; extra_read_dirs allowed
  - os.chdir               — navigation; allowed to extra_read_dirs too
  - pathlib.Path.open      — mode-aware
  - pathlib.Path.iterdir   — read-only by nature

Notable gaps (knowingly out of scope):
  - os.stat / os.path.exists / Path.exists — metadata probes
  - os.remove / os.unlink / os.rename — deletion / rename paths
  - shutil.* — most call through open() and inherit protection
  - network / subprocess / ctypes — out of scope
"""
import builtins
import os
import pathlib

# Set by install_scope_wrapper(). Empty string means "scope not installed".
_SCOPE_ROOT = ""

#: Paths from which reads are permitted in addition to ``_SCOPE_ROOT``.
#: Populated at install time from the ``extra_read_dirs`` argument plus the
#: running interpreter's ``sys.prefix`` / ``sys.base_prefix``. Writes are
#: NEVER permitted to these paths; only reads.
_EXTRA_READ_DIRS: tuple = ()

# Originals, captured once at install time. Also used as the "installed" flag.
_orig: dict = {}


def _abs_real(p: str) -> str:
    return os.path.realpath(os.path.abspath(p))


def _is_under(abs_p: str, root: str) -> bool:
    return bool(root) and (abs_p == root or abs_p.startswith(root + os.sep))


def _resolve_and_check(path_like, op: str, is_write: bool = False) -> str:
    """Resolve path to absolute canonical form. Raise PermissionError if out of scope.

    ``is_write=True`` enforces the strict confinement (scope_dir only).
    ``is_write=False`` also permits reads from ``_EXTRA_READ_DIRS``.
    """
    if path_like is None:
        raise PermissionError(f"Sandbox scope: {op} rejected — path is None")
    try:
        p = os.fspath(path_like)
    except TypeError:
        raise PermissionError(
            f"Sandbox scope: {op} rejected — non-path argument of type "
            f"{type(path_like).__name__}"
        )
    if isinstance(p, bytes):
        p = p.decode("utf-8", errors="replace")
    abs_p = _abs_real(p)

    # In-scope is always allowed
    if _is_under(abs_p, _SCOPE_ROOT):
        return abs_p

    # Reads can additionally reach Python runtime dirs + any operator allow-list
    if not is_write:
        for extra in _EXTRA_READ_DIRS:
            if _is_under(abs_p, extra):
                return abs_p

    raise PermissionError(
        f"Sandbox scope: {op} rejected — {p!r} resolves outside space directory"
    )


def _is_write_mode(mode: str) -> bool:
    """True if the ``mode`` string passed to ``open`` implies writing."""
    if not mode:
        return False
    m = mode.lower()
    return "w" in m or "a" in m or "x" in m or "+" in m


def _is_write_flags(flags: int) -> bool:
    """True if the ``flags`` int passed to ``os.open`` implies writing."""
    # O_WRONLY=1, O_RDWR=2, O_APPEND, O_CREAT, O_TRUNC all imply write intent.
    write_mask = (
        os.O_WRONLY | os.O_RDWR | getattr(os, "O_APPEND", 0)
        | getattr(os, "O_CREAT", 0) | getattr(os, "O_TRUNC", 0)
    )
    return bool(flags & write_mask)


def _default_python_read_dirs() -> list[str]:
    """Python-runtime dirs that must remain read-accessible for imports to work.

    ``sys.prefix`` covers the venv / interpreter install; ``sys.base_prefix``
    covers the system Python when running inside a venv.
    """
    import sys

    roots: list[str] = []
    for attr in ("prefix", "base_prefix", "exec_prefix", "base_exec_prefix"):
        val = getattr(sys, attr, None)
        if val:
            roots.append(_abs_real(val))
    # De-dupe while preserving order
    seen: set = set()
    unique: list[str] = []
    for r in roots:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    return unique


def install_scope_wrapper(
    space_dir: str, extra_read_dirs: list[str] | None = None,
) -> None:
    """Install filesystem scope wrappers. Idempotent within a process.

    :param space_dir: the one directory where writes (and reads) are
        permitted unconditionally.
    :param extra_read_dirs: optional list of paths where READS are also
        permitted (writes remain scope-confined). Python runtime dirs
        (``sys.prefix``, ``sys.base_prefix``) are automatically added so
        the subprocess can import its own dependencies.
    """
    global _SCOPE_ROOT, _EXTRA_READ_DIRS
    _SCOPE_ROOT = _abs_real(space_dir)

    # Start with Python runtime paths so imports work, then union in the
    # operator-supplied allow-list.
    allow: list[str] = _default_python_read_dirs()
    if extra_read_dirs:
        for d in extra_read_dirs:
            if d:
                allow.append(_abs_real(d))
    # De-dupe
    seen: set = set()
    unique: list[str] = []
    for a in allow:
        if a and a not in seen:
            seen.add(a)
            unique.append(a)
    _EXTRA_READ_DIRS = tuple(unique)

    if _orig:
        # Already installed in this process. Update root + allow-list;
        # leave wrappers as-is.
        return

    _orig["builtins.open"] = builtins.open
    _orig["os.open"] = os.open
    _orig["os.listdir"] = os.listdir
    _orig["os.scandir"] = os.scandir
    _orig["os.chdir"] = os.chdir
    _orig["Path.open"] = pathlib.Path.open
    _orig["Path.iterdir"] = pathlib.Path.iterdir

    def _wrap_builtin_open(file, mode="r", *args, **kwargs):
        # fd-based access cannot be intercepted at this layer
        if isinstance(file, int):
            return _orig["builtins.open"](file, mode, *args, **kwargs)
        _resolve_and_check(file, "open", is_write=_is_write_mode(mode))
        return _orig["builtins.open"](file, mode, *args, **kwargs)

    def _wrap_os_open(path, flags, *args, **kwargs):
        _resolve_and_check(path, "os.open", is_write=_is_write_flags(flags))
        return _orig["os.open"](path, flags, *args, **kwargs)

    def _wrap_os_listdir(path="."):
        _resolve_and_check(path, "os.listdir", is_write=False)
        return _orig["os.listdir"](path)

    def _wrap_os_scandir(path="."):
        _resolve_and_check(path, "os.scandir", is_write=False)
        return _orig["os.scandir"](path)

    def _wrap_os_chdir(path):
        _resolve_and_check(path, "os.chdir", is_write=False)
        return _orig["os.chdir"](path)

    def _wrap_path_open(self, mode="r", *args, **kwargs):
        _resolve_and_check(str(self), "Path.open", is_write=_is_write_mode(mode))
        return _orig["Path.open"](self, mode, *args, **kwargs)

    def _wrap_path_iterdir(self):
        _resolve_and_check(str(self), "Path.iterdir", is_write=False)
        return _orig["Path.iterdir"](self)

    builtins.open = _wrap_builtin_open
    os.open = _wrap_os_open
    os.listdir = _wrap_os_listdir
    os.scandir = _wrap_os_scandir
    os.chdir = _wrap_os_chdir
    pathlib.Path.open = _wrap_path_open
    pathlib.Path.iterdir = _wrap_path_iterdir


def uninstall_scope_wrapper() -> None:
    """Restore original filesystem entry points. Test helper."""
    global _SCOPE_ROOT, _EXTRA_READ_DIRS
    if not _orig:
        return
    builtins.open = _orig["builtins.open"]
    os.open = _orig["os.open"]
    os.listdir = _orig["os.listdir"]
    os.scandir = _orig["os.scandir"]
    os.chdir = _orig["os.chdir"]
    pathlib.Path.open = _orig["Path.open"]
    pathlib.Path.iterdir = _orig["Path.iterdir"]
    _orig.clear()
    _SCOPE_ROOT = ""
    _EXTRA_READ_DIRS = ()


def _set_scope_root_for_tests(
    root: str, extra_read_dirs: list[str] | None = None,
) -> None:
    """Unit-test helper: set scope state without installing wrappers.

    Lets tests exercise :func:`_resolve_and_check` against a fake root
    without monkey-patching ``builtins.open`` in the test process (which
    would interfere with pytest's own file IO).
    """
    global _SCOPE_ROOT, _EXTRA_READ_DIRS
    _SCOPE_ROOT = _abs_real(root) if root else ""
    if root and extra_read_dirs:
        _EXTRA_READ_DIRS = tuple(
            _abs_real(d) for d in extra_read_dirs if d
        )
    elif root:
        _EXTRA_READ_DIRS = tuple(_default_python_read_dirs())
    else:
        _EXTRA_READ_DIRS = ()

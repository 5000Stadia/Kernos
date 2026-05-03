"""Storage backends for provider API keys.

Three backends:
  * ``keychain``      — OS credential store via the ``keyring`` library.
    macOS Keychain, Linux Secret Service, Windows Credential Locker. Keys
    never land on disk as files.
  * ``env_hardened``  — .env file on disk with mode 0600 (parent 0700).
    Default fallback when the OS keychain isn't available.
  * ``env_plaintext`` — Plain .env file. Requires explicit opt-in.

Per-install backend choice is recorded in ``config/storage_backend.yml``.

**the design review's implementation hazard:** switching backends must be a cleanup
operation, not a new write target. Ordering for every switch:

  1. Write every known secret to the target backend.
  2. Read-back verify.
  3. Remove the secret from the old backend only after read-back succeeds.
  4. If any read-back fails, abort and leave the old backend untouched.

``switch_storage_backend`` enforces that ordering. ``write_secret`` and
``remove_secret`` are the single-key primitives and callers outside the
switch path should use them directly.

Zero-LLM-call invariant: nothing here imports an LLM client or calls any
LLM endpoint. File IO, environment mutation, and ``keyring`` calls only.
"""
from __future__ import annotations

import logging
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

logger = logging.getLogger(__name__)

StorageBackendName = Literal["keychain", "env_hardened", "env_plaintext"]
VALID_BACKENDS: tuple[StorageBackendName, ...] = (
    "keychain",
    "env_hardened",
    "env_plaintext",
)

# Used to namespace Kernos secrets inside the OS keychain.
_KEYRING_SERVICE = "kernos"

# Config file recording the chosen backend.
_CONFIG_PATH = Path("config/storage_backend.yml")
_ENV_PATH = Path(".env")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StorageBackendError(Exception):
    """Generic storage-backend failure."""


class StorageBackendUnavailable(StorageBackendError):
    """The backend is not usable on this platform / in this environment."""


class StorageBackendSwitchAborted(StorageBackendError):
    """A switch was aborted because a read-back check failed. Old backend untouched."""


# ---------------------------------------------------------------------------
# Backend interface
# ---------------------------------------------------------------------------


class StorageBackend(Protocol):
    """Minimal interface every backend must implement.

    All methods are synchronous — setup is single-user, single-process, and
    the calls are cheap.
    """

    name: StorageBackendName

    def is_available(self) -> bool: ...

    def write_secret(self, key_env_var: str, value: str) -> None: ...

    def read_secret(self, key_env_var: str) -> str | None: ...

    def remove_secret(self, key_env_var: str) -> None: ...

    def has_secret(self, key_env_var: str) -> bool: ...


# ---------------------------------------------------------------------------
# OS keychain backend
# ---------------------------------------------------------------------------


@dataclass
class KeychainBackend:
    name: StorageBackendName = "keychain"

    def is_available(self) -> bool:
        try:
            import keyring
            # Probe the default backend — ``get_keyring()`` raises if none works.
            backend = keyring.get_keyring()
            # Null backend (``keyring.backends.fail.Keyring``) is "available" in
            # the import sense but can't actually store anything. Detect by class
            # name — good enough without importing the internal module.
            cls = type(backend).__name__
            if cls in {"Keyring", "NullKeyring"} and "fail" in type(backend).__module__:
                return False
            return True
        except Exception as exc:  # pragma: no cover — depends on platform
            logger.debug("keyring unavailable: %s", exc)
            return False

    def write_secret(self, key_env_var: str, value: str) -> None:
        import keyring

        keyring.set_password(_KEYRING_SERVICE, key_env_var, value)

    def read_secret(self, key_env_var: str) -> str | None:
        import keyring

        return keyring.get_password(_KEYRING_SERVICE, key_env_var)

    def remove_secret(self, key_env_var: str) -> None:
        import keyring

        try:
            keyring.delete_password(_KEYRING_SERVICE, key_env_var)
        except keyring.errors.PasswordDeleteError:
            # Not present — idempotent remove.
            pass

    def has_secret(self, key_env_var: str) -> bool:
        return self.read_secret(key_env_var) is not None


# ---------------------------------------------------------------------------
# Dotenv-backed backends (hardened + plaintext share file IO)
# ---------------------------------------------------------------------------


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict. Preserves unknown keys. No substitutions."""
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        # Strip surrounding quotes if both ends match.
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        out[key] = value
    return out


def _write_env_file(path: Path, data: dict[str, str], *, hardened: bool) -> None:
    """Atomically rewrite a .env file, preserving key order (sorted for stability).

    When ``hardened`` is True, ensures parent dir is 0700 and file is 0600.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if hardened:
        try:
            os.chmod(path.parent, 0o700)
        except OSError as exc:  # pragma: no cover — best-effort
            logger.debug("Could not harden parent dir %s: %s", path.parent, exc)
    lines = [f'{k}="{v}"' for k, v in sorted(data.items())]
    content = "\n".join(lines) + "\n"
    # Atomic write via temp file in the same dir (preserves fs semantics).
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix=".env.", suffix=".tmp", delete=False
    ) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    if hardened:
        os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)


@dataclass
class HardenedEnvBackend:
    name: StorageBackendName = "env_hardened"
    env_path: Path = _ENV_PATH

    def is_available(self) -> bool:
        # File-backed storage is always available on any POSIX/Windows system
        # we support.
        return True

    def write_secret(self, key_env_var: str, value: str) -> None:
        data = _read_env_file(self.env_path)
        data[key_env_var] = value
        _write_env_file(self.env_path, data, hardened=True)

    def read_secret(self, key_env_var: str) -> str | None:
        data = _read_env_file(self.env_path)
        return data.get(key_env_var)

    def remove_secret(self, key_env_var: str) -> None:
        data = _read_env_file(self.env_path)
        if key_env_var in data:
            del data[key_env_var]
            _write_env_file(self.env_path, data, hardened=True)

    def has_secret(self, key_env_var: str) -> bool:
        return self.read_secret(key_env_var) is not None


@dataclass
class PlaintextEnvBackend:
    name: StorageBackendName = "env_plaintext"
    env_path: Path = _ENV_PATH

    def is_available(self) -> bool:
        return True

    def write_secret(self, key_env_var: str, value: str) -> None:
        data = _read_env_file(self.env_path)
        data[key_env_var] = value
        _write_env_file(self.env_path, data, hardened=False)

    def read_secret(self, key_env_var: str) -> str | None:
        data = _read_env_file(self.env_path)
        return data.get(key_env_var)

    def remove_secret(self, key_env_var: str) -> None:
        data = _read_env_file(self.env_path)
        if key_env_var in data:
            del data[key_env_var]
            _write_env_file(self.env_path, data, hardened=False)

    def has_secret(self, key_env_var: str) -> bool:
        return self.read_secret(key_env_var) is not None


# ---------------------------------------------------------------------------
# Resolver / factory
# ---------------------------------------------------------------------------


def get_backend(name: StorageBackendName) -> StorageBackend:
    """Return a fresh backend instance by name."""
    if name == "keychain":
        return KeychainBackend()
    if name == "env_hardened":
        return HardenedEnvBackend()
    if name == "env_plaintext":
        return PlaintextEnvBackend()
    raise ValueError(f"Unknown storage backend: {name!r}")


def active_backend_name(config_path: Path | None = None) -> StorageBackendName | None:
    """Read the per-install active backend name from config, or None if unset."""
    import yaml

    path = config_path or _CONFIG_PATH
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        logger.warning("storage_backend.yml malformed: %s", exc)
        return None
    name = data.get("storage_backend")
    if name in VALID_BACKENDS:
        return name  # type: ignore[return-value]
    return None


def set_active_backend_name(
    name: StorageBackendName,
    config_path: Path | None = None,
) -> None:
    """Persist the active backend name to ``config/storage_backend.yml``."""
    import yaml

    if name not in VALID_BACKENDS:
        raise ValueError(f"Unknown storage backend: {name!r}")
    path = config_path or _CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"storage_backend": name}, sort_keys=False))


def active_backend(config_path: Path | None = None) -> StorageBackend | None:
    """Return the currently-active backend instance, or None if unset."""
    name = active_backend_name(config_path)
    if name is None:
        return None
    return get_backend(name)


# ---------------------------------------------------------------------------
# Switch (enforces the design review's ordering constraint)
# ---------------------------------------------------------------------------


def switch_storage_backend(
    target: StorageBackendName,
    managed_env_vars: list[str],
    *,
    config_path: Path | None = None,
) -> None:
    """Switch storage backends with cleanup-after-verify ordering.

    1. Copy every known secret from the current backend to ``target``.
    2. Read-back verify on the target.
    3. Remove the secret from the old backend only after step 2 succeeds.
    4. If read-back fails at step 2, abort: old backend untouched. Raise
       ``StorageBackendSwitchAborted``.
    5. If everything succeeds, write ``target`` to ``config/storage_backend.yml``.

    If no backend is currently active, this is a "first-time set" rather than
    a switch — nothing to migrate, just records the choice.

    ``managed_env_vars`` is the list of env-var names whose values should be
    migrated (typically the ``key_env_var`` field across the provider registry
    for every configured provider).
    """
    if target not in VALID_BACKENDS:
        raise ValueError(f"Unknown target backend: {target!r}")

    current_name = active_backend_name(config_path)
    target_backend = get_backend(target)

    if current_name is None:
        # First-time set — nothing to migrate.
        if not target_backend.is_available():
            raise StorageBackendUnavailable(
                f"Target backend {target!r} is not available on this system."
            )
        set_active_backend_name(target, config_path)
        logger.info("Storage backend set to %s (first-time)", target)
        return

    if current_name == target:
        # No-op switch.
        logger.info("Storage backend already set to %s; no switch needed.", target)
        return

    if not target_backend.is_available():
        raise StorageBackendUnavailable(
            f"Target backend {target!r} is not available on this system."
        )

    current_backend = get_backend(current_name)

    # Step 1: collect values from current backend for the managed env vars.
    values: dict[str, str] = {}
    for var in managed_env_vars:
        existing = current_backend.read_secret(var)
        if existing is not None:
            values[var] = existing

    # Step 2: write to target backend.
    for var, value in values.items():
        target_backend.write_secret(var, value)

    # Step 3: read-back verify. Any mismatch aborts and leaves old backend intact.
    for var, expected in values.items():
        actual = target_backend.read_secret(var)
        if actual != expected:
            # Clean up partial writes on target before aborting.
            for v in values:
                try:
                    target_backend.remove_secret(v)
                except Exception:  # pragma: no cover — best-effort cleanup
                    logger.warning("Cleanup of partial target write failed for %s", v)
            raise StorageBackendSwitchAborted(
                f"Read-back verify failed for {var} on {target}; old backend unchanged."
            )

    # Step 4: remove from old backend ONLY after verify succeeded.
    for var in values:
        try:
            current_backend.remove_secret(var)
        except Exception as exc:  # pragma: no cover — log but continue
            logger.warning(
                "Failed to remove %s from old backend %s: %s (target still holds secret)",
                var, current_name, exc,
            )

    # Step 5: record the new active backend.
    set_active_backend_name(target, config_path)
    logger.info(
        "Storage backend switched: %s → %s (%d secrets migrated)",
        current_name, target, len(values),
    )


def detect_default_backend() -> StorageBackendName:
    """Return the best-available default when no backend is configured yet.

    ``keychain`` when the OS credential store works; ``env_hardened`` otherwise.
    ``env_plaintext`` is never auto-selected — it requires explicit opt-in.
    """
    if KeychainBackend().is_available():
        return "keychain"
    return "env_hardened"

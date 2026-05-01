"""Aider harness — build mode only.

Aider's CLI is task-shaped (chat-driven file modifications), not
Q&A-shaped. v1 exposes Aider via the :class:`Harness` protocol's
``build()`` method ONLY; ``consult()`` raises
:class:`HarnessUnavailable`.

The existing ``kernos/kernel/builders/aider.py`` ships a fully-
debugged AiderBuilder with credential pass-through, scope-wrapping
via sitecustomize, and file-modification tracking. This harness
adapts that builder's wider interface (instance_id / space_id /
code / write_file_name / data_dir / scope) to the unified Harness
interface (task / workspace_dir / timeout_seconds /
harness_options) by deriving the legacy fields from harness_options
and the workspace_dir.

Spec AC18 + commit C3.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from kernos.kernel.builders.aider import AiderBuilder
from kernos.kernel.builders.base import BuildResult as LegacyBuildResult
from kernos.kernel.external_agents.errors import HarnessUnavailable
from kernos.kernel.external_agents.harness import (
    BuildResult,
    ConsultResult,
    HarnessHealth,
)

logger = logging.getLogger(__name__)


class AiderHarness:
    """Thin adapter wrapping :class:`AiderBuilder` to the unified
    :class:`Harness` protocol. Build mode only; consult mode
    structurally unsupported."""

    name = "aider"

    def __init__(self) -> None:
        self._builder = AiderBuilder()

    def health_check(self) -> HarnessHealth:
        import shutil
        path = shutil.which("aider")
        if not path:
            return HarnessHealth(
                name=self.name, installed=False,
                detail="aider binary not on PATH (install `aider-chat`)",
            )
        return HarnessHealth(
            name=self.name, installed=True, authenticated=True,
            detail=f"binary at {path}",
        )

    async def consult(
        self, **_,
    ) -> ConsultResult:
        # AC18: Aider in consult mode raises HarnessUnavailable with
        # a clear message. Aider's CLI is task-execution-shaped, not
        # Q&A-shaped; the registry should normally reject this at the
        # boundary, but the harness method also defends.
        raise HarnessUnavailable(
            "aider does not support consult mode in v1; its CLI is "
            "task-shaped, not Q&A-shaped. Use code_exec with "
            "backend='aider' for build mode."
        )

    async def build(
        self,
        *,
        task: str,
        workspace_dir: Path,
        timeout_seconds: int,
        harness_options: dict[str, Any],
    ) -> BuildResult:
        # The legacy AiderBuilder takes instance_id / space_id /
        # data_dir to compute the space directory. The harness
        # interface gives us workspace_dir directly. Map back via
        # harness_options for the metadata the legacy builder uses,
        # falling back to sensible defaults derived from the
        # workspace path.
        opts = harness_options or {}
        instance_id = opts.get("instance_id", "default")
        space_id = opts.get("space_id", "default")
        data_dir = opts.get("data_dir", str(workspace_dir.parent))
        scope = opts.get("scope", "isolated")
        write_file_name = opts.get("write_file_name")

        legacy: LegacyBuildResult = await self._builder.build(
            instance_id=instance_id,
            space_id=space_id,
            code=task,
            timeout_seconds=timeout_seconds,
            write_file_name=write_file_name,
            data_dir=data_dir,
            scope=scope,
        )
        return BuildResult(
            success=legacy.success,
            stdout=legacy.stdout,
            stderr=legacy.stderr,
            exit_code=legacy.exit_code,
            error=legacy.error,
            files_modified=list(legacy.files_modified),
        )


__all__ = ["AiderHarness"]

#!/usr/bin/env bash
# SUBSTRATE-SELF-TEST-V1 (2026-05-26) AC10 — live-provider soak.
# Invokes ONE real ACPX dispatch end-to-end against the live
# binary as a cross-check that the deterministic suite's
# fake-binary assumptions still match the actual ACPX contract.
#
# EXPLICITLY EXCLUDED FROM CI MERGE GATES. Live providers are
# too flaky to gate pre-merge on. Run this:
#   - on-demand (manually before a substrate change that touches
#     external_agents)
#   - scheduled (daily cron, results land in a dashboard)
#
# Usage:
#   scripts/run_live_provider_soak.sh             # default harness
#   HARNESS=codex scripts/run_live_provider_soak.sh
#   HARNESS=claude_code scripts/run_live_provider_soak.sh
#
# Failure here is loud-signal-only. Operator decides whether to
# investigate; the merge gate continues to depend on the
# deterministic soak suite only.

set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -z "${VIRTUAL_ENV:-}" ]] && [[ -f .venv/bin/activate ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

HARNESS="${HARNESS:-claude_code}"
PROMPT="${PROMPT:-Reply with exactly the word PONG and nothing else.}"

echo "LIVE_PROVIDER_SOAK harness=${HARNESS} prompt=\"${PROMPT}\""
echo "(running real ACPX dispatch — this calls the actual external"
echo "agent binary, not the deterministic fake)"
echo

python <<PYEOF
import asyncio
import sys
from kernos.kernel.external_agents.acpx_adapter import dispatch

async def _main():
    try:
        result = await dispatch(
            target="${HARNESS}",
            prompt="${PROMPT}",
            session_id=None,
            workspace_dir=".",
            timeout_seconds=120,
        )
        print(f"LIVE_PROVIDER_SOAK_OK response_chars={len(result.text)} "
              f"stop_reason={result.metadata.get('acpx_stop_reason')}")
        print(f"--- response ---")
        print(result.text[:500])
        return 0
    except Exception as exc:
        print(f"LIVE_PROVIDER_SOAK_FAIL exc={type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1

sys.exit(asyncio.run(_main()))
PYEOF

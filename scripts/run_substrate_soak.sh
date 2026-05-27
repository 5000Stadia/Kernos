#!/usr/bin/env bash
# SUBSTRATE-SELF-TEST-V1 (2026-05-26) — local-developer entry point
# to the substrate-soak suite. Invokes the same single canonical
# contract CI uses + the post-bring-up hook uses:
#
#     python -m kernos.kernel.self_test_gate --include-soak --json
#
# Per spec design principle: one path, no drift. Don't shell out to
# pytest tests/substrate_soak/ directly — that's a parallel gate
# that can diverge from "what the substrate runs against itself."
#
# Usage:
#   scripts/run_substrate_soak.sh            # human-readable prose
#   scripts/run_substrate_soak.sh --json     # machine-readable JSON
#
# Exit code 0 on full pass; non-zero on any failure. Returns the
# JSON payload (or prose) on stdout for inspection.

set -euo pipefail

cd "$(dirname "$0")/.."

# Activate venv if present + not already active.
if [[ -z "${VIRTUAL_ENV:-}" ]] && [[ -f .venv/bin/activate ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

exec python -m kernos.kernel.self_test_gate --include-soak "$@"

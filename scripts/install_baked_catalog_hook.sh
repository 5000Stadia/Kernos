#!/usr/bin/env bash
# Install the baked-reference-catalog freshness check as a git pre-commit hook.
#
# Run once after cloning the repository. The hook runs the hash-only
# freshness check before each commit; if any docs/*.md change is staged
# without a corresponding regen, the commit is blocked with diagnostics.
#
# This hook spends ZERO LLM calls. It only validates that the
# contributor ran `python scripts/regenerate_reference_catalog.py`
# locally before committing the docs change. The regen script itself
# is the LLM-spending surface, and it runs at contribution time, not
# in CI.
#
# Architect verdict: REFERENCE-CATALOG-BAKED-V1, principle (2) —
# "CI gate is hash-comparison only — never an LLM-spending surface."

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOK_PATH="$REPO_ROOT/.git/hooks/pre-commit"

if [[ -e "$HOOK_PATH" ]]; then
    cat <<EOF >&2
Refusing to overwrite existing $HOOK_PATH.
If you already have a pre-commit hook, append this snippet to it:

    python scripts/check_reference_catalog_freshness.py || exit 1

EOF
    exit 1
fi

cat > "$HOOK_PATH" <<'HOOK'
#!/usr/bin/env bash
# Reference-catalog freshness check (REFERENCE-CATALOG-BAKED-V1).
# Hash-only; never spends LLM calls.
set -euo pipefail
python "$(git rev-parse --show-toplevel)/scripts/check_reference_catalog_freshness.py"
HOOK
chmod +x "$HOOK_PATH"
echo "Installed pre-commit hook at $HOOK_PATH"
echo "Test it: python scripts/check_reference_catalog_freshness.py"

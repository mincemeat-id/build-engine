#!/usr/bin/env bash
# =============================================================================
# install-git-hooks.sh — point git at the repo's tracked .githooks/ directory.
# =============================================================================
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

if [ ! -d .githooks ]; then
    echo "✗ .githooks/ directory not found at $REPO_ROOT" >&2
    exit 1
fi

chmod +x .githooks/pre-commit .githooks/pre-push 2>/dev/null || true

git config core.hooksPath .githooks

echo "✓ Git hooks installed."
echo "  hooksPath = $(git config --get core.hooksPath)"
echo ""
echo "  Hooks active:"
echo "    • pre-commit — fast Ruff lint/format on staged Python files"
echo "    • pre-push   — full deployment-readiness gate (mirrors CI)"
echo ""
echo "  Bypass once with: BYPASS_HOOKS=1 git commit ..."
echo "                or: git commit --no-verify"


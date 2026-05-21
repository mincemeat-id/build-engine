#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if ! grep -q '^VERSION_ID="24.04"$\|^VERSION_ID=24.04$' /etc/os-release; then
  echo "This smoke is intended for Ubuntu 24.04 hosts." >&2
  exit 1
fi

uv run pyinstaller packaging/pyinstaller/build-engine.spec --noconfirm

ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT"' EXIT

DESTDIR="$ROOT" BUILD_ENGINE_BINARY="dist/build-engine" bash scripts/install-build-engine.sh
DESTDIR="$ROOT" BUILD_ENGINE_BINARY="dist/build-engine" bash scripts/install-build-engine.sh

"$ROOT/usr/local/bin/build-engine" --version
"$ROOT/usr/local/bin/build-engine" drain

test -x "$ROOT/usr/local/bin/build-engine"
test -f "$ROOT/etc/systemd/system/build-engine.service"
test -f "$ROOT/etc/mincemeat/build-engine/config.toml"
test -f "$ROOT/usr/share/doc/build-engine/operations.md"

echo "Ubuntu 24.04 install/upgrade/drain smoke passed."

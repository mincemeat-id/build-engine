#!/usr/bin/env bash
set -euo pipefail

ROOT="${DESTDIR:-}"
PREFIX="${PREFIX:-/usr/local}"
SYSCONFDIR="${SYSCONFDIR:-/etc/mincemeat/build-engine}"
STATE_DIR="${STATE_DIR:-/var/lib/build-engine}"
LOG_DIR="${LOG_DIR:-/var/log/build-engine}"
SERVICE_DIR="${SERVICE_DIR:-/etc/systemd/system}"
DOC_DIR="${DOC_DIR:-/usr/share/doc/build-engine}"
BINARY_SRC="${BUILD_ENGINE_BINARY:-dist/build-engine}"
BINARY_DST="${ROOT}${PREFIX}/bin/build-engine"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

install_file() {
  local src="$1"
  local dst="$2"
  local mode="$3"
  install -D -m "$mode" "$src" "$dst"
}

if [[ ! -f "$BINARY_SRC" ]]; then
  echo "build-engine binary not found at $BINARY_SRC" >&2
  echo "Set BUILD_ENGINE_BINARY=/path/to/build-engine or run make binary-smoke first." >&2
  exit 1
fi

if [[ -z "$ROOT" && "$(id -u)" != "0" ]]; then
  echo "install-build-engine.sh must run as root unless DESTDIR is set" >&2
  exit 1
fi

install_file "$BINARY_SRC" "$BINARY_DST" 0755
install_file "$REPO_ROOT/packaging/systemd/build-engine.service" \
  "${ROOT}${SERVICE_DIR}/build-engine.service" 0644
install_file "$REPO_ROOT/docs/operations.md" \
  "${ROOT}${DOC_DIR}/operations.md" 0644

install -d -m 0755 "${ROOT}${SYSCONFDIR}" "${ROOT}${STATE_DIR}" "${ROOT}${LOG_DIR}"

if [[ ! -f "${ROOT}${SYSCONFDIR}/config.toml" ]]; then
  install -m 0644 /dev/stdin "${ROOT}${SYSCONFDIR}/config.toml" <<EOF
# Mincemeat Build Engine host config.
# Registration writes credentials.toml.
state_dir = "${STATE_DIR}"
max_concurrency = 2
heartbeat_interval_seconds = 15
EOF
fi

if command -v getent >/dev/null 2>&1 && [[ -z "$ROOT" ]]; then
  if ! getent group build-engine >/dev/null; then
    groupadd --system build-engine
  fi
  if ! id build-engine >/dev/null 2>&1; then
    useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin \
      --gid build-engine --groups docker build-engine
  fi
  chown -R build-engine:build-engine "$STATE_DIR" "$LOG_DIR"
  chgrp -R build-engine "$SYSCONFDIR"
  chmod 0750 "$SYSCONFDIR"
fi

if command -v systemctl >/dev/null 2>&1 && [[ -z "$ROOT" ]]; then
  systemctl daemon-reload
fi

echo "Installed build-engine to $BINARY_DST"
echo "Register with: build-engine register --backend-url <url> --token <token> --name <name>"

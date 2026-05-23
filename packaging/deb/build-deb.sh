#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIST_DIR="${DIST_DIR:-${REPO_ROOT}/dist}"
BINARY_SRC="${BUILD_ENGINE_BINARY:-${DIST_DIR}/build-engine}"
PACKAGE_NAME="${PACKAGE_NAME:-mincemeat-build-engine}"

if [[ ! -x "$BINARY_SRC" ]]; then
  echo "build-engine binary not found or not executable at $BINARY_SRC" >&2
  echo "Run: make binary-smoke or set BUILD_ENGINE_BINARY=/path/to/build-engine" >&2
  exit 1
fi

VERSION="${VERSION:-$("$BINARY_SRC" --version | awk '{print $2}')}"
RELEASE_ARCH="${RELEASE_ARCH:-linux-amd64}"
case "$RELEASE_ARCH" in
  linux-amd64) DEB_ARCH="${DEB_ARCH:-amd64}" ;;
  linux-arm64) DEB_ARCH="${DEB_ARCH:-arm64}" ;;
  *) DEB_ARCH="${DEB_ARCH:-$(dpkg --print-architecture)}" ;;
esac

if ! command -v dpkg-deb >/dev/null 2>&1; then
  echo "dpkg-deb is required to build the Debian package" >&2
  exit 1
fi

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

PACKAGE_ROOT="${WORK_DIR}/${PACKAGE_NAME}_${VERSION}_${DEB_ARCH}"
mkdir -p "$PACKAGE_ROOT/DEBIAN" "$DIST_DIR"

DESTDIR="$PACKAGE_ROOT" \
  PREFIX=/usr/local \
  BUILD_ENGINE_BINARY="$BINARY_SRC" \
  bash "${REPO_ROOT}/scripts/install-build-engine.sh" >/dev/null

cat > "${PACKAGE_ROOT}/DEBIAN/control" <<EOF
Package: ${PACKAGE_NAME}
Version: ${VERSION}
Section: web
Priority: optional
Architecture: ${DEB_ARCH}
Maintainer: Mincemeat maintainers
Depends: docker.io | docker-ce, ca-certificates, tzdata
Homepage: https://github.com/mincemeat-id/build-engine
Description: Standalone Mincemeat static-site build engine agent
 Executes static-site builds in curated Docker builder images and connects
 outbound to Mincemeat coreapp over the build-engine agent protocol.
EOF

cat > "${PACKAGE_ROOT}/DEBIAN/conffiles" <<'EOF'
/etc/mincemeat/build-engine/config.toml
EOF

cat > "${PACKAGE_ROOT}/DEBIAN/postinst" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

if command -v getent >/dev/null 2>&1; then
  if ! getent group build-engine >/dev/null; then
    groupadd --system build-engine
  fi
  if ! id build-engine >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/build-engine --shell /usr/sbin/nologin \
      --gid build-engine --groups docker build-engine
  fi
  chown -R build-engine:build-engine /var/lib/build-engine /var/log/build-engine
  chgrp -R build-engine /etc/mincemeat/build-engine
  chmod 0750 /etc/mincemeat/build-engine
fi

if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload || true
fi
EOF

cat > "${PACKAGE_ROOT}/DEBIAN/prerm" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" = "remove" ]] && command -v systemctl >/dev/null 2>&1; then
  systemctl stop build-engine.service >/dev/null 2>&1 || true
fi
EOF

chmod 0755 "${PACKAGE_ROOT}/DEBIAN/postinst" "${PACKAGE_ROOT}/DEBIAN/prerm"

OUTPUT="${DIST_DIR}/${PACKAGE_NAME}_${VERSION}_${DEB_ARCH}.deb"
dpkg-deb --build --root-owner-group "$PACKAGE_ROOT" "$OUTPUT"
echo "Wrote $OUTPUT"

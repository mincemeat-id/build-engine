#!/usr/bin/env bash
set -euo pipefail

DIST_DIR="${DIST_DIR:-dist}"
BINARY="${BUILD_ENGINE_BINARY:-${DIST_DIR}/build-engine}"
VERSION="$("${BINARY}" --version | awk '{print $2}')"
ARTIFACT="${DIST_DIR}/build-engine-${VERSION}-linux-amd64"
CHECKSUMS="${DIST_DIR}/SHA256SUMS"

if [[ ! -x "$BINARY" ]]; then
  echo "release binary not found or not executable at $BINARY" >&2
  echo "Run: uv run pyinstaller packaging/pyinstaller/build-engine.spec --noconfirm" >&2
  exit 1
fi

cp "$BINARY" "$ARTIFACT"
chmod 0755 "$ARTIFACT"

(
  cd "$DIST_DIR"
  sha256sum "$(basename "$ARTIFACT")" > "$(basename "$CHECKSUMS")"
)

if [[ "${COSIGN_SIGN:-0}" == "1" ]]; then
  if ! command -v cosign >/dev/null 2>&1; then
    echo "COSIGN_SIGN=1 was set but cosign is not installed" >&2
    exit 1
  fi
  cosign sign-blob --yes --output-signature "${ARTIFACT}.sig" "$ARTIFACT"
fi

if [[ -n "${GPG_SIGNING_KEY:-}" ]]; then
  gpg --batch --yes --local-user "$GPG_SIGNING_KEY" --armor \
    --detach-sign --output "${ARTIFACT}.asc" "$ARTIFACT"
elif [[ "${GPG_SIGN:-0}" == "1" ]]; then
  gpg --batch --yes --armor --detach-sign --output "${ARTIFACT}.asc" "$ARTIFACT"
fi

echo "Wrote $ARTIFACT"
echo "Wrote $CHECKSUMS"

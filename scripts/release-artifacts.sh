#!/usr/bin/env bash
set -euo pipefail

DIST_DIR="${DIST_DIR:-dist}"
BINARY="${BUILD_ENGINE_BINARY:-${DIST_DIR}/build-engine}"
RELEASE_ARCH="${RELEASE_ARCH:-linux-amd64}"
CHECKSUMS="${DIST_DIR}/SHA256SUMS"

if [[ ! -x "$BINARY" ]]; then
  echo "release binary not found or not executable at $BINARY" >&2
  echo "Run: uv run pyinstaller packaging/pyinstaller/build-engine.spec --noconfirm" >&2
  exit 1
fi

VERSION="$("${BINARY}" --version | awk '{print $2}')"
ARTIFACT_BASENAME="build-engine-${VERSION}-${RELEASE_ARCH}"
ARTIFACT="${DIST_DIR}/${ARTIFACT_BASENAME}"

write_checksums() {
  (
    cd "$DIST_DIR"
    mapfile -t files < <(
      find . -maxdepth 1 -type f \
        \( \
          -name "${ARTIFACT_BASENAME}" \
          -o -name "${ARTIFACT_BASENAME}.sig" \
          -o -name "${ARTIFACT_BASENAME}.pem" \
          -o -name "${ARTIFACT_BASENAME}.asc" \
          -o -name "${ARTIFACT_BASENAME}.cdx.json" \
          -o -name "${ARTIFACT_BASENAME}.provenance.intoto.jsonl" \
          -o -name "${ARTIFACT_BASENAME}.sbom.intoto.jsonl" \
          -o -name "mincemeat-build-engine_${VERSION}_*.deb" \
        \) \
        -printf '%f\n' | sort
    )
    if [[ "${#files[@]}" -eq 0 ]]; then
      echo "no release files found for ${ARTIFACT_BASENAME}" >&2
      exit 1
    fi
    sha256sum "${files[@]}" > "$(basename "$CHECKSUMS")"
  )
}

if [[ "${CHECKSUM_ONLY:-0}" != "1" ]]; then
  cp "$BINARY" "$ARTIFACT"
  chmod 0755 "$ARTIFACT"

  if [[ "${COSIGN_SIGN:-0}" == "1" ]]; then
    if ! command -v cosign >/dev/null 2>&1; then
      echo "COSIGN_SIGN=1 was set but cosign is not installed" >&2
      exit 1
    fi
    cosign sign-blob --yes \
      --output-signature "${ARTIFACT}.sig" \
      --output-certificate "${ARTIFACT}.pem" \
      "$ARTIFACT"
  fi

  if [[ -n "${GPG_SIGNING_KEY:-}" ]]; then
    gpg --batch --yes --local-user "$GPG_SIGNING_KEY" --armor \
      --detach-sign --output "${ARTIFACT}.asc" "$ARTIFACT"
  elif [[ "${GPG_SIGN:-0}" == "1" ]]; then
    gpg --batch --yes --armor --detach-sign --output "${ARTIFACT}.asc" "$ARTIFACT"
  fi
fi

write_checksums

echo "Wrote $ARTIFACT"
echo "Wrote $CHECKSUMS"

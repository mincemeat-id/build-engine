#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-mincemeat-id/build-engine}"
ARCH="${ARCH:-linux-amd64}"
TAG=""
WORK_DIR=""

usage() {
  cat <<'EOF'
Usage: scripts/verify-release.sh <tag> [--dir path]

Downloads and verifies a build-engine GitHub Release. Required local tools:
gh, sha256sum, cosign, slsa-verifier, jq.

Environment:
  REPO  GitHub repository, default mincemeat-id/build-engine.
  ARCH  Artifact architecture suffix, default linux-amd64.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)
      WORK_DIR="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      if [[ -n "$TAG" ]]; then
        echo "unexpected argument: $1" >&2
        usage >&2
        exit 2
      fi
      TAG="$1"
      shift
      ;;
  esac
done

if [[ -z "$TAG" ]]; then
  usage >&2
  exit 2
fi

for tool in gh sha256sum cosign slsa-verifier jq; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "missing required tool: $tool" >&2
    exit 1
  fi
done

if [[ -z "$WORK_DIR" ]]; then
  WORK_DIR="$(mktemp -d)"
  trap 'rm -rf "$WORK_DIR"' EXIT
  gh release download "$TAG" \
    --repo "$REPO" \
    --pattern 'build-engine-*' \
    --pattern 'SHA256SUMS' \
    --dir "$WORK_DIR"
fi

cd "$WORK_DIR"

binary="$(find . -maxdepth 1 -type f -name "build-engine-*-${ARCH}" \
  ! -name '*.sig' ! -name '*.pem' ! -name '*.asc' ! -name '*.json' ! -name '*.jsonl' \
  -printf '%f\n' | sort | head -n 1)"
if [[ -z "$binary" ]]; then
  echo "release binary not found in $WORK_DIR" >&2
  exit 1
fi

required=(
  "SHA256SUMS"
  "${binary}.sig"
  "${binary}.pem"
  "${binary}.cdx.json"
  "${binary}.provenance.intoto.jsonl"
  "${binary}.sbom.intoto.jsonl"
)
for path in "${required[@]}"; do
  if [[ ! -f "$path" ]]; then
    echo "missing release verification file: $path" >&2
    exit 1
  fi
done

sha256sum --check SHA256SUMS

cosign verify-blob \
  --certificate "${binary}.pem" \
  --signature "${binary}.sig" \
  --certificate-identity-regexp "^https://github.com/${REPO}/" \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
  "$binary"

slsa-verifier verify-artifact "$binary" \
  --provenance-path "${binary}.provenance.intoto.jsonl" \
  --source-uri "github.com/${REPO}" \
  --source-tag "$TAG"

jq -e '.bomFormat == "CycloneDX"' "${binary}.cdx.json" >/dev/null

echo "PASS: $REPO $TAG verified"

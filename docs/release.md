# Build Engine Release Process

> **Status:** Release pipeline design.
> **Audience:** Maintainers, release managers, platform operators.

The build-engine release pipeline publishes a standalone Linux amd64 binary and
its verification material from GitHub Actions. Releases are cut from immutable
`vX.Y.Z` tags after the default branch has a green `make verify`.

## Inputs

Required release inputs:

- `pyproject.toml` version set to `X.Y.Z`.
- Matching `CHANGELOG.md` section `## [X.Y.Z] - YYYY-MM-DD`.
- Current OpenAPI, protocol, and image-manifest contract snapshots.
- Green CI on the `ubuntu-24.04` GitHub-hosted runner.

Optional release inputs:

- `GPG_SIGNING_KEY` for detached `.asc` signatures.
- `ENABLE_ARM64=true` when the linux arm64 matrix entry is ready to publish.
- `BUILD_ENGINE_IMAGES_MANIFEST_URL` when the release should align against a
  manifest other than the default immutable build-engine-images `v1.0.0`
  release manifest.

## Trigger

Maintainers cut a release with:

```bash
make release VERSION=X.Y.Z
```

The target updates `pyproject.toml`, refreshes `uv.lock`, adds the matching
changelog section, commits, tags `vX.Y.Z`, and pushes with `git push
--follow-tags`.

The release workflow is triggered by tags:

```yaml
on:
  push:
    tags:
      - "v*.*.*"
```

The workflow also supports manual dispatch with an explicit tag. Manual runs
publish a draft GitHub Release so maintainers can inspect assets before making
them public.

The workflow uses a tag-scoped concurrency group so re-runs for the same tag do
not publish conflicting artifacts:

```yaml
concurrency:
  group: build-engine-release-${{ github.ref_name }}
  cancel-in-progress: false
```

Minimum permissions:

```yaml
permissions:
  contents: write
  id-token: write
  attestations: write
  packages: write
  security-events: write
```

## Build Job

The build job runs on the `ubuntu-24.04` GitHub-hosted runner and uses
`astral-sh/setup-uv` so Python installation and dependency resolution match the
developer path. The job:

1. Checks out the tag.
2. Installs Python 3.14 with `uv`.
3. Runs `make verify`.
4. Builds `dist/build-engine` through the PyInstaller spec.
5. Installs the binary into a temporary `DESTDIR` and runs
   `build-engine --version` and `build-engine doctor --json`.
6. Renames the binary to `build-engine-X.Y.Z-linux-amd64`.
7. Generates a CycloneDX source SBOM.
8. Builds `mincemeat-build-engine_X.Y.Z_amd64.deb` under `packaging/deb/`.
9. Signs the binary with cosign keyless signing.
10. Generates SLSA provenance and SBOM attestations.
11. Generates `SHA256SUMS` for the binary, Debian package, SBOM, signatures, certificates, and
    attestation bundles.
12. Tags builder-image packages with `build-engine-X.Y.Z`, using the manifest
    URL configured by `BUILD_ENGINE_IMAGES_MANIFEST_URL` or the default
    immutable `build-engine-images v1.0.0` release manifest.

The build must not depend on host-global state beyond Docker, iptables support,
and the runner baseline documented in
[`operations.md`](operations.md#ci-infrastructure).

## Builder Image Alignment

The repo-root `manifest.json` is the local pinned snapshot of the published
builder-image manifest that the engine advertises in registration and session
headers. Keep it at the repository root so release tooling can validate it
without depending on a sibling checkout.

For the `0.2.x` release line, the default release workflow aligns images using:

```text
https://github.com/mincemeat-id/build-engine-images/releases/download/v1.0.0/manifest.json
```

If `BUILD_ENGINE_IMAGES_MANIFEST_URL` is set, the workflow downloads that
manifest and fails before tagging images if its `version` differs from
`DEFAULT_IMAGE_MANIFEST_VERSION`.

## Security Gates

Release jobs run the same checks as CI plus release-specific supply-chain
checks:

- `bandit -r src/`
- `pip-audit` against the exported locked Python requirements, run through
  `uv` so the audit uses the same Python 3.14 toolchain as the release build.
- Trivy filesystem vulnerability scan with `--severity HIGH,CRITICAL --exit-code 1`,
  excluding generated outputs, the local virtualenv, and static-site fixture
  dependency locks that are not shipped with the engine.
- CycloneDX SBOM generation for the built binary and dependency graph
- Dependency review on pull requests that change direct dependencies
- `actionlint` for workflow changes

All `actions/*` and third-party actions must be pinned by commit SHA in the
final release workflow.

## Signing And Attestations

The release pipeline publishes:

- Cosign keyless blob signature for the binary.
- GitHub build provenance attestation for the binary and checksum file.
- GitHub SBOM attestation for the CycloneDX document.
- Optional GPG detached signature when release secrets are configured.

Cosign and GitHub attestations use OIDC through `id-token: write`; long-lived
signing secrets are avoided where possible.

## Artifacts

The GitHub release uploads:

- `build-engine-X.Y.Z-linux-amd64`
- `mincemeat-build-engine_X.Y.Z_amd64.deb`
- `SHA256SUMS`
- `build-engine-X.Y.Z-linux-amd64.sig`
- `build-engine-X.Y.Z-linux-amd64.pem`
- `build-engine-X.Y.Z-linux-amd64.asc` when GPG signing is enabled
- `build-engine-X.Y.Z-linux-amd64.provenance.intoto.jsonl`
- `build-engine-X.Y.Z-linux-amd64.sbom.intoto.jsonl`
- `build-engine-X.Y.Z-linux-amd64.cdx.json`

Release notes are populated from the matching `CHANGELOG.md` section and then
reviewed before publication.

## Downstream Verification

Consumers verify a release by downloading the binary, checksum file, signature,
Debian package, and attestations, then checking:

1. `sha256sum --check SHA256SUMS`
2. `cosign verify-blob` with the published certificate identity
3. GitHub provenance attestation subject and tag
4. Optional GPG detached signature
5. `build-engine --version`
6. `build-engine doctor --json`

`scripts/verify-release.sh <tag>` makes this path repeatable for operators and
support staff.

## Reproducibility

The release workflow always starts by removing `build/`, `dist/`, and
release reports from the checkout before running `make verify`, then uploads
only files under `dist/build-engine-<version>-<arch>*` plus `SHA256SUMS`.
That prevents host leftovers from entering the release bundle.

The release job fixes `SOURCE_DATE_EPOCH=0`, `PYTHONHASHSEED=0`, `TZ=UTC`, and
`LC_ALL=C.UTF-8`, then performs two explicit clean PyInstaller builds and
compares the `dist/build-engine` SHA256 before any artifact is signed. The
second clean build is kept as the release binary. A mismatch fails the release
before publication.

This check was verified locally on 2026-05-24 against PyInstaller 6.20.0 and
Python 3.14.0. Without the fixed environment, the one-file executable differed
between clean builds; with the fixed environment, both builds produced
`5673873f8ca537b57297c0a6c42400d6f2a650ace20c8d7aac8d175cc9733622`.

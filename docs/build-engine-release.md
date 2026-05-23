# Build Engine Release Process

> **Status:** Release pipeline design.
> **Audience:** Maintainers, release managers, platform operators.

The build-engine release pipeline publishes a standalone Linux amd64 binary and
its verification material from GitHub Actions. Releases are cut from immutable
`vX.Y.Z` tags after `main` has a green `make verify`.

## Inputs

Required release inputs:

- `pyproject.toml` version set to `X.Y.Z`.
- Matching `CHANGELOG.md` section `## [X.Y.Z] - YYYY-MM-DD`.
- Current OpenAPI, protocol, and image-manifest contract snapshots.
- Green CI on the sanctioned self-hosted runner pool:
  `self-hosted`, `linux`, `x64`, `ubuntu-24.04`.

Optional release inputs:

- `GPG_SIGNING_KEY` for detached `.asc` signatures.
- Manual approval for linux arm64 when that matrix entry is enabled.

## Trigger

The release workflow is triggered by tags:

```yaml
on:
  push:
    tags:
      - "v*.*.*"
```

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
  packages: read
```

## Build Job

The build job runs on the self-hosted Ubuntu 24.04 pool and uses
`astral-sh/setup-uv` so Python installation and dependency resolution match the
developer path. The job:

1. Checks out the tag.
2. Installs Python 3.14 with `uv`.
3. Runs `make verify`.
4. Builds `dist/build-engine` through the PyInstaller spec.
5. Installs the binary into a temporary `DESTDIR` and runs
   `build-engine --version` and `build-engine doctor --json`.
6. Renames the binary to `build-engine-X.Y.Z-linux-amd64`.
7. Generates `SHA256SUMS`.

The build must not depend on host-global state beyond Docker, iptables support,
and the runner baseline documented in
[`build-engine-operations.md`](build-engine-operations.md#ci-infrastructure).

## Security Gates

Release jobs run the same checks as CI plus release-specific supply-chain
checks:

- `bandit -r src/`
- `pip-audit` against `uv.lock`
- Trivy filesystem scan with `--severity HIGH,CRITICAL --exit-code 1`
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
- `SHA256SUMS`
- `build-engine-X.Y.Z-linux-amd64.sig`
- `build-engine-X.Y.Z-linux-amd64.asc` when GPG signing is enabled
- `build-engine-X.Y.Z-linux-amd64.intoto.jsonl`
- `build-engine-X.Y.Z-linux-amd64.cdx.json`

Release notes are populated from the matching `CHANGELOG.md` section and then
reviewed before publication.

## Downstream Verification

Consumers verify a release by downloading the binary, checksum file, signature,
and attestations, then checking:

1. `sha256sum --check SHA256SUMS`
2. `cosign verify-blob` with the published certificate identity
3. GitHub provenance attestation subject and tag
4. Optional GPG detached signature
5. `build-engine --version`
6. `build-engine doctor --json`

The planned `scripts/verify-release.sh` helper makes this path repeatable for
operators and support staff.

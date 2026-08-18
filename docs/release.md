# Release process

The current production package is `0.3.0`. A release changes the engine,
protocol contract, image manifest compatibility, and coordinated coreapp
contract as one reviewable change.

## Branch and contract order

Keep the active coreapp checkout untouched. Use the committed coreapp HEAD in
the separate worktree and synchronize explicitly:

```bash
BUILD_ENGINE_COREAPP_ROOT=/home/nerdv2/work/Mincemeat/coreapp-build-engine-v2 \
  uv run python scripts/sync_contracts.py
```

The generated OpenAPI subset and SHA-256 snapshot must be committed with the
engine branch. Run the coreapp contract, API, worker dispatch, and worker
pipeline tests against that worktree before merging either branch.

## Required checks

From the build-engine repository:

```bash
make verify
BUILD_ENGINE_DOCKER_INTEGRATION=1 make docker-integration
```

`make verify` runs contract synchronization, compile checks, Ruff, `ty`,
Bandit, unit/contract tests, and the PyInstaller `--version` smoke. The Docker
harness uses the eight real certified fixtures and pinned manifest references;
it covers registration, JWT rotation, presigned source retrieval, transient
secret fetch, pinned builds, upload, WSS disconnect/replay, cancellation,
timeout, restart recovery, stale attempts, and cache reset. Missing integration
environment is an explicit failure when the harness is requested, not a silent
production claim.

Also run installer/package smoke on Ubuntu 24.04 amd64:

```bash
make deb
bash scripts/smoke-ubuntu-24.04.sh
```

## Version alignment

Before tagging, verify that all of the following agree:

- package and changelog version `0.3.0`;
- protocol v2 in WSS JSON and OpenAPI contracts;
- manifest version `1.0.0`, protocol compatibility `2..2`, and digest-only
  image entries;
- the eight-profile production matrix and npm/pnpm policy;
- README, protocol, design, image, operations, release, and agent docs.

## Package and publish

Build the PyInstaller binary from a clean engine worktree, inspect the included
manifest resource, run `dist/build-engine --version`, and create the Debian
artifact. Publish the engine package only after the separate coreapp branch has
passed its own checks and the Docker harness has exercised the coordinated
contract.

Do not publish a release with mutable image references, legacy v1 routes,
network-guard claims, unsupported framework claims, unbounded event retention,
or a red `make verify` result.

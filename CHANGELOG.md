# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows semantic versioning once v1 artifacts are published.

## [Unreleased]

### Added

- AGPL-3.0-or-later `LICENSE` file at the repository root.
- Documentation refresh for contributor workflow, release operations, builder
  image source-of-truth links, and the build-secret environment contract.
- Changelog/version drift guardrail in the test suite.
- Public-facing protocol reference at `docs/protocol.md` documenting the WSS
  envelope, HTTP endpoints, and build-secret contract the agent consumes.

### Changed

- Renamed design docs in `docs/` to drop the redundant `build-engine-` prefix
  (`design.md`, `images.md`, `operations.md`, `release.md`, `protocol.md`).
- Repository prepared for open-source publication: removed internal-only
  analysis material, generalized backend URL examples, and softened
  control-plane (coreapp) references.
- CI workflows now run on GitHub-hosted `ubuntu-24.04` runners instead of a
  self-hosted pool. Release matrix uses `ubuntu-24.04-arm` when `ENABLE_ARM64`
  is set.

## [0.1.0] - 2026-05-23

### Added

- Initial build-engine package, CLI, config, registration/session auth, WSS
  protocol handling, durable SQLite queue, Docker executor, cache handling,
  metrics collection, contract snapshots, and PyInstaller packaging smoke path.

[Unreleased]: https://github.com/mincemeat-id/build-engine/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/mincemeat-id/build-engine/releases/tag/v0.1.0

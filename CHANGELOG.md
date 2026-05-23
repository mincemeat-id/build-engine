# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows semantic versioning once v1 artifacts are published.

## [Unreleased]

### Added

- Documentation refresh for contributor workflow, release operations, builder
  image source-of-truth links, and the build-secret environment contract.
- Changelog/version drift guardrail in the test suite.

## [0.1.0] - 2026-05-23

### Added

- Initial build-engine package, CLI, config, registration/session auth, WSS
  protocol handling, durable SQLite queue, Docker executor, cache handling,
  metrics collection, contract snapshots, and PyInstaller packaging smoke path.

[Unreleased]: https://github.com/mincemeat-id/build-engine/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/mincemeat-id/build-engine/releases/tag/v0.1.0

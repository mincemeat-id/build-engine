# Mincemeat Build Engine

Standalone Python 3.14 agent for running Mincemeat static-site builds outside
coreapp. The engine connects outbound to coreapp over WSS, receives build
attempts, executes them in curated Docker builder images, streams status/logs,
and uploads staged artifacts back to the platform.

This repository is currently at Stage 0: contract and scaffold. Runtime
behavior is intentionally skeletal; the shape, tooling, contracts, and binary
smoke path are in place for the implementation stages that follow.

## Quick Start

```bash
uv sync
uv run build-engine --version
uv run build-engine doctor --json
make verify
```

The project targets Python 3.14 or newer and uses:

- `uv` for dependency and environment management.
- Ruff for linting and formatting.
- ty for type checking.
- pytest for tests.
- pre-commit for local guardrails.
- PyInstaller for the eventual `--onefile` binary.

Install hooks once per checkout:

```bash
uv run pre-commit install
```

## Repository Layout

```text
build-engine/
├── contracts/              # Imported Stage 0 contracts
│   ├── image-manifest/     # Builder image manifest JSON Schema
│   ├── openapi/            # Coreapp build-engine OpenAPI subset
│   └── protocol/           # WSS protocol envelope schema
├── docs/                   # Design docs and contract lock
├── packaging/pyinstaller/  # Binary smoke/build spec
├── scripts/                # Contract sync tooling
├── src/build_engine/       # Agent package skeleton and CLI
└── tests/                  # Scaffold and contract smoke tests
```

## Contracts

Stage 0 imports the locked contract surfaces from the adjacent coreapp and
design-doc sources:

- `contracts/openapi/build-engine.openapi.json` is extracted from
  `../coreapp/frontend/openapi.json` by `scripts/sync_contracts.py`.
- `contracts/protocol/wss-v1.json` captures the WSS envelope and locked message
  type names.
- `contracts/image-manifest/manifest.schema.json` captures the builder image
  manifest contract from `docs/build-engine-images-design.md`.

Refresh the OpenAPI snapshot after coreapp contract regeneration:

```bash
make contracts-sync
```

## CLI

The installed command is `build-engine`.

```bash
build-engine serve
build-engine register --backend-url https://agent.mincemeat.id --token TOKEN --name ENGINE
build-engine status
build-engine doctor --json
build-engine cache reset --site-id SITE_ID
build-engine drain
```

Only scaffold behavior exists in Stage 0. Config, registration, auth, uplink,
queueing, executor, cache, metrics, and full diagnostics land in later stages.

## Compatibility Matrix

V1 GA framework support is locked by the design and builder-image contract:

| Framework | Preferred image | Output directory |
|-----------|-----------------|------------------|
| Astro | `node:22` or `bun:1` | `dist/` |
| Vite | `node:22` or `bun:1` | `dist/` |
| Eleventy | `node:22` | `_site/` |
| Docusaurus | `node:22` | `build/` |
| VitePress | `node:22` | `.vitepress/dist/` |
| VuePress | `node:22` | `dist/` |
| Gatsby | `node:22` | `public/` |
| Hugo | `hugo:latest` | `public/` |
| Next.js static export | `node:22` | `out/` |
| Nuxt generate | `node:22` | `.output/public/` |
| SvelteKit static | `node:22` | `build/` |
| Generic | `node:22` | inferred |

V1.x candidates after fixtures, docs, images, scans, and size gates:

| Framework | Status |
|-----------|--------|
| Zola | Candidate |
| Angular static | Candidate |
| Remix SPA mode | Candidate |

## Verification

```bash
make verify
```

The verification target syncs the OpenAPI contract snapshot, compiles source
and tests, runs Ruff, ty, pytest, and builds/runs the PyInstaller smoke binary.

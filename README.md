# Mincemeat Build Engine

![Status: V1 GA implementation](https://img.shields.io/badge/status-V1%20GA%20implementation-blue)
![License: AGPL-3.0-or-later](https://img.shields.io/badge/license-AGPL--3.0--or--later-blue)

Standalone Python 3.14 agent for running Mincemeat static-site builds outside
the control plane (coreapp). The engine connects outbound over WSS, receives
build attempts, executes them in curated Docker builder images, streams
status/logs, and uploads staged artifacts back to the platform.

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
- PyInstaller for the `--onefile` distribution binary.

Install hooks once per checkout:

```bash
make hooks-install
```

## Repository Layout

```text
build-engine/
├── contracts/              # Imported contract snapshots
│   ├── image-manifest/     # Builder image manifest JSON Schema
│   ├── openapi/            # Control-plane build-engine OpenAPI subset
│   └── protocol/           # WSS protocol envelope schema
├── docs/                   # Design, protocol, operations, and release docs
├── packaging/              # Debian, PyInstaller, and systemd assets
├── scripts/                # Contract sync, install, release tooling
├── src/build_engine/       # Agent package and CLI
└── tests/                  # Unit, contract, and integration tests
```

## Contracts

The repository carries locked contract surfaces:

- `contracts/openapi/build-engine.openapi.json` is the agent's HTTP surface,
  extracted by `scripts/sync_contracts.py` from the control plane's OpenAPI
  source. The script expects the control-plane checkout at `../coreapp`.
- `contracts/protocol/wss-v1.json` captures the WSS envelope and locked
  message-type names.
- `contracts/image-manifest/manifest.schema.json` captures the builder image
  manifest contract from [`docs/images.md`](docs/images.md).

Refresh the OpenAPI snapshot after a control-plane contract regeneration:

```bash
make contracts-sync
```

## CLI

The installed command is `build-engine`.

```bash
build-engine serve
build-engine register --backend-url https://agent.example.com --token TOKEN --name ENGINE
build-engine status
build-engine doctor --json
build-engine session refresh
build-engine cache reset --site-id SITE_ID
build-engine drain
```

Registration, credential validation, session refresh, WSS uplink, and durable
queueing are implemented alongside the Docker executor, cache metrics,
packaging operations, and diagnostics described in the design docs.

## Compatibility Matrix

V1 GA framework support is locked by the design and builder-image contract.
The default builder-image set advertised at registration is `node:22`,
`bun:1`, `hugo:latest`, and `zola:latest` — the V1 GA matrix shipped in
`build-engine-images`.

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
| Zola | `zola:latest` | `public/` |
| Next.js static export | `node:22` | `out/` |
| Nuxt generate | `node:22` | `.output/public/` |
| SvelteKit static | `node:22` | `build/` |
| Angular static | `node:22` | `dist/<project>/browser/` |
| Remix SPA | `node:22` | `build/client/` |
| Generic | `node:22` | inferred |

Angular static and Remix SPA framework profiles ship as opt-in via the
`framework` override; auto-detection follows the existing dependency- and
script-marker ordering in `detect/framework.py`.

## Documentation

- [Design](docs/design.md) — agent architecture, queue, executor, cache.
- [Protocol reference](docs/protocol.md) — WSS envelope, HTTP endpoints,
  authentication, and build-secret contract.
- [Builder images](docs/images.md) — curated image matrix and manifest.
- [Operations](docs/operations.md) — install, upgrade, diagnostics, CI.
- [Release](docs/release.md) — release pipeline, signing, attestations.
- [Contributing](CONTRIBUTING.md) — verification gates, hooks, contract
  refreshes.

## Verification

```bash
make verify
```

The verification target syncs the OpenAPI contract snapshot, compiles source
and tests, runs Ruff, ty, Bandit, pytest, and builds/runs the PyInstaller
smoke binary.

## License

Distributed under the GNU Affero General Public License v3.0 or later
([LICENSE](LICENSE)).

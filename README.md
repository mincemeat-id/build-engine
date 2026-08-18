# Mincemeat Build Engine

![Status: protocol v2 production candidate](https://img.shields.io/badge/status-protocol%20v2%20production%20candidate-blue)
![License: AGPL-3.0-or-later](https://img.shields.io/badge/license-AGPL--3.0--or--later-blue)

The build engine is a standalone Python 3.14 agent for Ubuntu 24.04 amd64.
It connects outbound to coreapp over WSS, accepts build attempts, runs a
certified static-site build in Docker, and uploads the staged artifact.

The current production package is `0.3.0`. Protocol v2 is a clean break: all
control, status, log, heartbeat, artifact, and acknowledgement traffic uses
the WSS stream. The HTTP surface is limited to registration, short-lived
session refresh, attempt-scoped secret retrieval, artifact upload tickets, and
health.

## Quick start

```bash
uv sync
uv run build-engine --version
uv run build-engine doctor --json
BUILD_ENGINE_COREAPP_ROOT=../coreapp make verify
```

Install the local hooks once per checkout:

```bash
make hooks-install
```

## Runtime commands

```bash
build-engine register \
  --backend-url https://agent.example.com \
  --token ONE_TIME_TOKEN \
  --name build-engine-sfo-1 \
  --max-concurrency 2

build-engine serve
build-engine status
build-engine doctor --json
build-engine session refresh
build-engine cache reset --site-id SITE_UUID
build-engine drain
build-engine resume
```

Credentials are written atomically with mode `0600`; runtime state lives under
`/var/lib/build-engine` by default. Source archives, build outputs, caches,
and temporary files use this canonical layout:

```text
<state>/workspaces/<attempt-uuid>/{src,out,cache,tmp}
```

Only `out` is packaged.

## Certified framework matrix

The registry is intentionally small and versioned. Compatibility failures are
execution-blocking; there is no silent generic fallback.

| Framework | Image | Package manager | Output |
|-----------|-------|-----------------|--------|
| Astro | pinned Node 22 | npm or pnpm | `dist/` |
| Vite | pinned Node 22 | npm or pnpm | `dist/` |
| Eleventy | pinned Node 22 | npm or pnpm | `_site/` |
| Docusaurus | pinned Node 22 | npm or pnpm | `build/` |
| VitePress | pinned Node 22 | npm or pnpm | `docs/.vitepress/dist/` |
| VuePress | pinned Node 22 | npm or pnpm | `docs/.vuepress/dist/` |
| Gatsby | pinned Node 22 | npm or pnpm | `public/` |
| Hugo | pinned Hugo image | none | `public/` |

Bun, Yarn, Zola, Generic, Angular, Remix, Next, Nuxt, and SvelteKit are
deferred until separately certified. The shipped manifest contains only the
Node 22 and pinned Hugo entries and every Docker execution uses
`tag@sha256:digest`.

## Contracts and documentation

- [Design](docs/design.md) — boundaries, state, worker lifecycle, and security.
- [Protocol](docs/protocol.md) — WSS v2 envelope and HTTP endpoints.
- [Builder images](docs/images.md) — manifest and certification process.
- [Operations](docs/operations.md) — installation, drain, recovery, and health.
- [Release](docs/release.md) — package, signature, and smoke requirements.
- [Security policy](SECURITY.md) — vulnerability reporting.

`contracts/openapi/build-engine.openapi.json` is generated from the adjacent
control-plane checkout. Use an explicit checkout when working in an active
workspace:

```bash
BUILD_ENGINE_COREAPP_ROOT=/path/to/coreapp-build-engine-v2 make contracts-sync
```

The WSS contract is [wss-v2.json](contracts/protocol/wss-v2.json), and the
root `manifest.json` is the immutable builder-manifest snapshot.

## Verification

```bash
make verify
```

The gate synchronizes contracts, compiles source and tests, runs Ruff, ty,
Bandit, pytest, and builds/runs the PyInstaller binary smoke. The complete
Docker harness is explicit because it pulls the certified images:

```bash
BUILD_ENGINE_COREAPP_ROOT=/path/to/coreapp-build-engine-v2 \
  BUILD_ENGINE_DOCKER_INTEGRATION=1 make docker-integration
```

## License

Distributed under the GNU Affero General Public License v3.0 or later
([LICENSE](LICENSE)).

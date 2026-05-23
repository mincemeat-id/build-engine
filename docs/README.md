# Build Engine Design Documentation

![Status: V1 GA implementation](https://img.shields.io/badge/status-V1%20GA%20implementation-blue)

> **Status:** Design and decision documentation.
> **Last Updated:** 2026-05-23.

This directory contains the build-engine design documentation. The build engine
is a standalone Python 3.14 single-binary agent that connects outbound to
coreapp, accepts build attempts over WSS, executes them in Docker containers
using curated images, streams logs/status, uploads artifacts to platform staging
storage, and reports metrics.

## Documentation Index

The design is split by repository so each component has its own source of truth:

- [Build-engine design](build-engine-design.md) - standalone agent binary,
  WSS protocol, Docker executor, local queue, cache, packaging, and host
  operations.
- [Build-engine images design](build-engine-images-design.md) - builder image
  repository design, image manifest contract, framework image matrix, publication,
  scanning, and rollback.
- [Build-engine release process](build-engine-release.md) - release pipeline,
  artifact signing, attestations, publication, and consumer verification.
- [Coreapp integration design](coreapp-design.md) - coreapp integration
  including shared models, backend, worker, frontend, migrations, pipeline
  integration, and operator UI.
- [Operations runbook](build-engine-operations.md) - host sizing, install,
  upgrade, diagnostics, release artifacts, and CI runner pool requirements.
- [Contributor guide](../CONTRIBUTING.md) - verification gates, hooks,
  contract refreshes, release process, and agent-specific notes.

## Key Design Decisions

| Topic | Decision |
|-------|----------|
| Queueing when engines are busy | Compatible online engines that are saturated keep jobs queued for up to `BUILD_ENGINE_QUEUE_MAX_WAIT_SECONDS=1800`; `BUILD` reports `WAITING_FOR_ENGINE`. |
| No compatible online engine | Fail immediately with `NO_ENGINE_AVAILABLE`. No-build pipelines still skip `BUILD` and continue. |
| Artifact/log staging | Use a platform-owned staging bucket/prefix. The worker promotes build output from staging to the site's final storage target. |
| Agent authentication | Use a one-time registration token, engine secret, and short-lived bearer session tokens for agent HTTP and WSS requests. |
| Multi-replica backend routing | Store engine WSS ownership in Redis and publish commands to `build-engine:commands:{engine_id}`. |
| Retry identity | Use `BuildJobAttempt.id` on every WSS event and artifact upload URL. Stale attempts are audit-only. |
| Framework GA scope | v1 GA: Astro, Vite, Eleventy, Docusaurus, VitePress, VuePress, Gatsby, Hugo, Zola, Next.js static export, Nuxt generate, SvelteKit static, Angular static, Remix SPA, Generic. |
| Network mode | `NETWORK_FULL` allows outbound internet but blocks metadata IPs, host gateway, Docker bridge, and platform private networks. |
| Deployment source | Keep `Deployment.source = GITHUB` for GitHub-sourced builds; add `deploy_metadata.build_engine=true` and `deploy_metadata.build_job_id`. |
| Historical pipelines | Do not backfill. Frontend renders six-stage historical pipelines and seven-stage new pipelines. |

## Verification

Each repository has its own verification gate:
- **build-engine**: `make verify` runs lint, type-check, tests, and binary build smoke
- **build-engine-images**: CI runs lint, image build, Trivy scan, fixture smoke, and manifest validation
- **coreapp**: `make verify` runs Python lint/type-check, frontend lint/build, contract checks, and tests

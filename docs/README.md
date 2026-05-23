# Build Engine Documentation

![Status: V1 GA implementation](https://img.shields.io/badge/status-V1%20GA%20implementation-blue)

> **Status:** Design and decision documentation.
> **Last Updated:** 2026-05-23.

This directory contains the build-engine documentation. The build engine is a
standalone Python 3.14 single-binary agent that connects outbound to a control
plane (coreapp), accepts build attempts over WSS, executes them in Docker
containers using curated images, streams logs/status, uploads artifacts to
staging storage, and reports metrics.

## Documentation Index

- [Design](design.md) — agent binary, WSS uplink, Docker executor, local queue,
  cache, packaging, and host operations.
- [Builder images](images.md) — curated builder image repository, image
  manifest contract, framework matrix, publication, scanning, and rollback.
- [Release process](release.md) — release pipeline, artifact signing,
  attestations, publication, and consumer verification.
- [Protocol reference](protocol.md) — WSS envelope, message types, attempt
  lifecycle, and HTTP agent endpoints the engine talks to.
- [Operations](operations.md) — host sizing, install, upgrade, diagnostics,
  release artifacts, and CI infrastructure.
- [Contributor guide](../CONTRIBUTING.md) — verification gates, hooks,
  contract refreshes, release process, and agent-specific notes.

## Key Design Decisions

| Topic | Decision |
|-------|----------|
| Queueing when engines are busy | Compatible online engines that are saturated keep jobs queued for up to `BUILD_ENGINE_QUEUE_MAX_WAIT_SECONDS=1800`; `BUILD` reports `WAITING_FOR_ENGINE`. |
| No compatible online engine | Fail immediately with `NO_ENGINE_AVAILABLE`. No-build pipelines still skip `BUILD` and continue. |
| Artifact/log staging | Use a platform-owned staging bucket/prefix. The control plane promotes build output from staging to the site's final storage target. |
| Agent authentication | One-time registration token, engine secret, and short-lived bearer session tokens for agent HTTP and WSS requests. |
| Retry identity | Use `BuildJobAttempt.id` on every WSS event and artifact upload URL. Stale attempts are audit-only. |
| Framework GA scope | v1 GA: Astro, Vite, Eleventy, Docusaurus, VitePress, VuePress, Gatsby, Hugo, Zola, Next.js static export, Nuxt generate, SvelteKit static, Angular static, Remix SPA, Generic. |
| Network mode | `NETWORK_FULL` allows outbound internet but blocks metadata IPs, host gateway, Docker bridge, and operator-supplied private networks. |

## Verification

Local verification: `make verify` runs lint, type-check, security lint, tests,
and the PyInstaller binary smoke. See the [contributor guide](../CONTRIBUTING.md)
for full details.

# Build Engine Final Design

> **Status:** Final design plan for implementation kickoff.
> **Date:** 2026-05-19.
> **Scope:** Coreapp integration, standalone build engine, and curated builder
> image repositories.

This directory archives the final build-engine design for coreapp reference.
The design is split by repository so each implementation track has its own
source of truth:

- [Coreapp design](coreapp-design.md) - shared models, backend, worker,
  frontend, migrations, pipeline integration, and operator UI.
- [Build-engine design](build-engine-design.md) - standalone agent binary,
  WSS protocol, Docker executor, local queue, cache, packaging, and host
  operations.
- [Build-engine images design](build-engine-images-design.md) - builder image
  repository, image manifest contract, framework image matrix, publication,
  scanning, and rollback.
- [Static-site build-engine operations](../static-sites/build-engine-operations.md)
  - coreapp runbook for hostnames, staging storage, observability, local smoke,
  staging smoke, and failure drills.

## Final Decisions

| Topic | Decision |
|-------|----------|
| Queueing when engines are busy | Compatible online engines that are saturated keep jobs queued for up to `BUILD_ENGINE_QUEUE_MAX_WAIT_SECONDS=1800`; `BUILD` reports `WAITING_FOR_ENGINE`. |
| No compatible online engine | Fail immediately with `NO_ENGINE_AVAILABLE`. No-build pipelines still skip `BUILD` and continue. |
| Artifact/log staging | Use a platform-owned staging bucket/prefix. The worker promotes build output from staging to the site's final storage target. |
| Agent TLS topology | Use a dedicated agent hostname outside CDN proxying. Traefik/Nginx requests client certs, forwards the verified peer certificate/fingerprint to FastAPI, and FastAPI pins against `BuildEngine.fingerprint`. |
| Multi-replica backend routing | Store engine WSS ownership in Redis and publish commands to `build-engine:commands:{engine_id}`. |
| Retry identity | Use `BuildJobAttempt.id` on every WSS event and artifact upload URL. Stale attempts are audit-only. |
| Framework GA scope | v1 GA: Astro, Vite, Eleventy, Docusaurus, VitePress, VuePress, Gatsby, Hugo, Next.js static export, Nuxt generate, SvelteKit static, Generic. v1.x candidates: Zola, Angular static, Remix SPA. |
| Network mode | `NETWORK_FULL` allows outbound internet but blocks metadata IPs, host gateway, Docker bridge, and platform private networks. |
| Deployment source | Keep `Deployment.source = GITHUB` for GitHub-sourced builds; add `deploy_metadata.build_engine=true` and `deploy_metadata.build_job_id`. |
| Historical pipelines | Do not backfill. Frontend renders six-stage historical pipelines and seven-stage new pipelines. |

## Cross-Repo Milestones

| Stage | Target | Exit Criteria |
|-------|--------|---------------|
| 0. Contract lock | 2-3 days, complexity M | Shared protocol, OpenAPI, image manifest, and framework GA list are agreed. |
| 1. Scaffolding | 3-5 days, complexity M | Three repos build in CI with placeholder contracts and docs. |
| 2. Vertical slice | 2-3 weeks, complexity XL | Astro and Vite build through engine and deploy through coreapp in local/staging. |
| 3. Framework completion | 2-3 weeks, complexity L | v1 GA framework fixtures pass cold/warm acceptance. |
| 4. Operator readiness | 1-2 weeks, complexity L | Admin UI, doctor, metrics, retention, cache reset, drain, and rollback paths are tested. |
| 5. Release hardening | 1 week, complexity M | Security scan, load smoke, failure drills, docs, and compatibility matrix pass. |

## Verification Gate

Coreapp changes must pass `make verify` before completion. The build-engine
and build-engine-images repos should mirror the same discipline with their own
lint, type-check, tests, binary/image build, scan, and fixture gates.

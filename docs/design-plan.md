# Build Engine — Design Draft (v0.5-review)

> **Status:** Design review complete; implementation kickoff is blocked until
> the v0.5 open questions in §19 are answered.
> All product decisions resolved (v0.1–v0.3). v0.4 adds: sequence diagrams,
> agent wire-protocol spec, OpenAPI sketch, Alembic migration sketch, ADRs,
> frontend wireframes, framework acceptance matrix, hardware spec,
> DR runbook, GHCR publication runbook, and compatibility matrix.
> v0.5-review adds: a gap analysis against the current coreapp pipeline,
> corrections to auth/log/migration/dispatch semantics, attempt-level
> idempotency, source-artifact handoff, and new open questions that need a
> definite answer before the plan is finalized.
> **Audience:** Backend, worker, frontend, platform, and operators.
> **Goal:** Define a new standalone project (`build-engine`) that performs
> framework-aware static-site builds in a Docker executor, registered with
> and driven by the existing Mincemeat backend over a NAT-friendly channel.
> Also define **all required changes** to the existing `backend/`, `worker/`,
> and `frontend/` projects.

This draft incorporates the answers given to the v0.1–v0.3 open questions
and the additional product context (Ubuntu Server 24.04/26.04 LTS x86_64
only, our-infrastructure-only deployments, separate repository for the
build-engine project, multi-project implementation tasks). The v0.5-review
pass deliberately reopens a small number of implementation-critical questions
where the earlier draft was internally inconsistent or did not match the
current coreapp pipeline.

---

## Changelog

| Version | Date       | Notes                                                                                       |
|---------|------------|---------------------------------------------------------------------------------------------|
| v0.1    | 2026-05-19 | Initial draft. 30 open questions, gap list, iteration plan.                                  |
| v0.2    | 2026-05-19 | OQ-1…OQ-20 resolved. New §§ added for backend/worker/frontend changes, queue design without external deps, build cache model, dispatcher policy, observability/metrics, and remaining open questions trimmed and renumbered. |
| v0.3    | 2026-05-19 | OQ-A…OQ-M resolved. Auth replaced with **LXD-style self-signed-cert + fingerprint-pinning** (no internal CA). Dispatcher behavior on zero engines fixed (immediate fail; no-build path unaffected). Cache scope locked to PM-only. New `build_engine_metric` table. Artifact + log retention mirror `Pipeline` retention. `doctor` subcommand in v1. Generic post-build output inference. Manual re-dispatch deferred. |
| v0.4    | 2026-05-19 | Added §§23–34: sequence diagrams (register, dispatch, run, cancel, engine-lost, cache reset, dispatch-zero), agent wire-protocol spec, OpenAPI sketch for all new endpoints, Alembic migration sketch, ADRs (queue, auth, cache, dispatcher, fast-fail), frontend wireframes, framework acceptance matrix, hardware spec, DR runbook, GHCR publication runbook, compatibility matrix. Ready for v0.5 implementation kickoff. |
| v0.5-review | 2026-05-19 | Comprehensive design review before finalization. Fixed stale CA/mTLS references, log-storage assumptions, native-enum migration sketch, source handoff, attempt-level replay protection, dispatcher semantics for busy engines, framework scope inconsistencies, and current coreapp pipeline integration gaps. Added §3ter and refreshed §19 open questions. |

---

## 1. Problem Statement (unchanged from v0.1)

Today the static-site stack ([`docs/static-sites/`](docs/static-sites/))
supports only **no-build** deployments. We add **framework-aware build
capability** as a separate service so:

- Heavy npm/Node/Docker builds do not bloat the platform Dramatiq worker.
- Build hosts are managed like `LxdServer`/`StorageTarget` (admin-registered).
- Build engines are isolated, individually upgradable, and operate over a
  NAT-friendly outbound channel.

The solution is the **Build Engine**, inspired by GitLab self-hosted runners
with a Docker executor.

---

## 2. Goals and Non-Goals (revised)

### Goals

1. **Framework auto-detection** from `package.json` and lockfiles, plus
   non-Node manifests (Hugo in v1; Zola as a v1 candidate or fast-follow).
2. **Package-manager auto-detection** (`npm`, `pnpm`, `yarn`, `bun`) using
   lockfiles and Corepack's `packageManager` field.
3. **Async, queued execution** with **real-time status** streamed back to the
   backend and forwarded to end-user browsers via the existing pipeline
   WebSocket.
4. **Docker executor** running each build inside a clean container with
   resource limits, log streaming, and cancellable execution.
5. **Portable single binary** packaged with **PyInstaller `--onefile`**,
   runnable as a `systemd` service alongside Docker on a dedicated Ubuntu
   Server VM/container.
6. **NAT-friendly registration** — engines connect outbound to the backend,
   pull jobs, and stream progress; no inbound ports required.
7. **Admin-managed registration**, mirroring the `LxdServer`/`StorageTarget`
   ergonomics.
8. **Async-first** engine with **parallel build processing**, default
   `max_concurrency = 2` per engine.
9. **Persistent build cache** with **tenancy isolation**, max-expiry, and
   safeguards.
10. **Operator visibility** into engine queue and job history (per-engine
    and global) from the admin UI.

### Non-Goals (v1)

- Anything other than static-site output.
- Replacing or merging with the existing in-repo `worker/` service.
- Multi-tenant build hosts across organizations (we operate the hosts).
- Distributed/remote-layer build cache (only per-engine local cache).
- User-supplied custom builder images (curated images only).
- arm64 / musl / Windows / macOS distribution (Ubuntu LTS x86_64 only).
- Engine self-update (manual operator-driven upgrades only).
- Per-environment (prod/preview) build secrets — global per site in v1.

---

## 3. Decisions Resolved From v0.1 Open Questions

For traceability, the resolutions are captured here verbatim and then woven
into the relevant sections of this v0.2 document.

| ID    | Decision                                                                                                       |
|-------|----------------------------------------------------------------------------------------------------------------|
| OQ-1  | `BUILD` is **always** a distinct `PipelineStage` row for new pipelines. If unnecessary, mark `SKIPPED` (or `SUCCESS` if implementation needs a completed terminal state) for stable stage numbering. |
| OQ-2  | **Separate repository** for `build-engine`. v0.x design lives in coreapp for context; once stable, the doc + project move to `mincemeat-id/build-engine`. |
| OQ-3  | **No external broker.** Use an embedded, file-backed, durable queue (SQLite-WAL based) inside the engine. Drop Dramatiq dependency in the engine. (Backend continues to use Dramatiq + Redis.) |
| OQ-4  | v1 ships fully static-compatible frameworks plus Generic fallback. v0.5 reopens the exact GA ship list as OQ-R because image/test coverage must match framework claims. Frameworks that require config to be static-compatible (e.g. Next.js export, Nuxt generate, SvelteKit adapter-static) are surfaced with **actionable warnings**. |
| OQ-5  | **Both** — fast-fail detection in backend after FETCH; comprehensive detection in engine at BUILD start.       |
| OQ-6  | **Corepack-on** in v1.                                                                                          |
| OQ-7  | Logs are **persistent**. Engine streams logs to the backend, backend writes the full BUILD log to object storage with a live tail in Redis. v0.5 corrected the earlier "matches current PipelineStage log behavior" wording because current stage logs are DB-capped. |
| OQ-8  | **Reuse** existing pipeline WebSocket (`/api/v1/sites/{site_id}/pipelines/{pipeline_id}/ws`). Add `BUILD` stage events to the same stream. |
| OQ-9  | Images published to **GHCR public** under `ghcr.io/mincemeat-id/build-engine-*`. No secrets baked in; SBOM and provenance attached for auditability. |
| OQ-10 | **Persistent per-site cache** with tenancy isolation: cache key namespaced by `site_id`, mounted read-write only into builds for that site, max age 30 days, max size 5 GiB per site (configurable). |
| OQ-11 | Resource ceilings: **max 10 min wallclock**, **1 CPU**, **2 GiB RAM**, **500 MiB output artifact**.            |
| OQ-12 | **`NETWORK_FULL`** is the v1 default. `NETWORK_RESTRICTED` deferred.                                          |
| OQ-13 | **Re-dispatchable** jobs. v0.5 refines idempotency from `job_id` alone to `(build_job_id, attempt_id)` so stale attempts cannot win races. |
| OQ-14 | **Superseded by OQ-H.** Earlier CA-based mTLS + JWT design was replaced in v0.3 by LXD-style self-signed client certificates plus fingerprint pinning and short-lived JWTs. |
| OQ-15 | **WSS required.** No long-poll fallback in v1.                                                                |
| OQ-16 | **Global per-site secrets** in v1.                                                                            |
| OQ-17 | PyInstaller **`--onefile`**.                                                                                  |
| OQ-18 | **Ubuntu LTS x86_64 only.** No arm64, no musl, no Windows, no macOS.                                          |
| OQ-19 | **No self-update** in v1. Operators upgrade manually (or via our ops automation).                              |
| OQ-20 | `BuildJob` is a **sibling** of `Pipeline`, linked via FK.                                                     |

Additional product-level decisions baked into v0.2:

- Default per-engine concurrency: **`max_concurrency = 2`**.
- Dispatch policy v1: **round-robin** over `ONLINE` engines whose
  capabilities match.
- Engine heartbeat interval: **15 s**; backend marks engine `OFFLINE` after
  **3 missed beats (45 s)** and **fails in-flight jobs immediately** with
  `ENGINE_LOST` so the user sees fast failure rather than long stalls.
- Engine pushes **metrics** to the backend, both **real-time** (per-event
  inside the WSS stream) and on a **15 s rollup interval**.
- **Post-build validation** runs **on the engine first** (fail-fast) and
  **again in the backend** after artifact upload (defense in depth).

### 3bis. Decisions Resolved From v0.2 Open Questions

| ID    | Decision                                                                                                       |
|-------|----------------------------------------------------------------------------------------------------------------|
| OQ-A  | **PM cache only** in v1 (npm/_cacache, pnpm store, yarn cache, bun install cache). No `node_modules` shadow.    |
| OQ-B  | **New `build_engine_metric` table**. Independent of `instance_metric`.                                          |
| OQ-C  | When **all engines are offline**, fail the pipeline **immediately** at the `BUILD` stage with `error_code = NO_ENGINE_AVAILABLE`. **No-build pipelines are unaffected** — they skip BUILD as `SKIPPED` and proceed to UPLOAD/ACTIVATE/FINALIZE even when zero engines are registered. |
| OQ-D  | Build-artifact retention in the chosen staging store **mirrors `Pipeline` retention** (`PIPELINE_RETENTION_PER_SITE` plus active deployment's pipeline). |
| OQ-E  | Build-log retention in the chosen staging store **aligns with `Pipeline` retention** (same lifecycle as the owning pipeline). |
| OQ-F  | Re-enabling cache after disabling **wipes and starts fresh**.                                                  |
| OQ-G  | **No** manual re-dispatch of a failed `BuildJob` in v1. User must re-run the pipeline (manual redeploy or new push). |
| OQ-H  | **Drop the internal CA.** Auth uses the **same model as LXD servers**: engine generates a **self-signed cert at registration**, posts it to the backend, backend stores the **encrypted PEM + fingerprint** in `BuildEngine`. Backend pins the fingerprint per session; engine pins the backend TLS leaf fingerprint. No CRL, no PKI, no rotation infra for v1. |
| OQ-I  | Engine audit-log retention = platform default.                                                                |
| OQ-J  | **Wire-protocol negotiation as proposed**: engine sends `proto_version`; backend rejects unsupported ranges. Engine refuses jobs that request images outside its pinned `image_manifest_version` range. |
| OQ-K  | **Ship `build-engine doctor` in v1.** End-to-end diagnostics for operators.                                    |
| OQ-L  | **Fixed 10 s** SIGTERM→SIGKILL grace across all engines. Not configurable in v1.                              |
| OQ-M  | **Generic framework gets intelligent post-build output detection.** After running the user's build command, the engine scans for `out/`, `dist/`, `build/`, `public/`, `_site/`, `.output/public/` (in that order) and picks the first one that contains `index.html`. If none is found, the job fails with `USER_OUTPUT_INVALID` and a structured guidance payload. The detected output path is reported back and persisted on `SiteBuildConfig.detected_output_dir` so subsequent builds skip the inference. |

### 3ter. v0.5 Design Review Findings

The v0.4 draft was directionally strong but too optimistic about readiness.
This review found several places where the design either contradicted itself,
assumed behavior that coreapp does not currently have, or skipped a boundary
that will matter during implementation. The concrete fixes are woven into the
later sections; unresolved decisions are tracked in §19.

| ID     | Finding                                                                 | Action in this draft                                                                 |
|--------|-------------------------------------------------------------------------|--------------------------------------------------------------------------------------|
| DR-1   | The current pipeline `VALIDATE` stage validates the publish directory and materializes files into memory. A buildable source needs a different path: validate the source archive, classify build mode, then let `BUILD` produce the deployable file set. | Clarified split validation and upload inputs in §§4.2, 10.4, and 11. |
| DR-2   | Dispatcher logic conflated "no compatible engine exists" with "engines exist but are busy." Fast-failing every busy period would violate the async queued goal. | Added queue-vs-fail decision as OQ-N and adjusted dispatcher semantics to distinguish unavailable from saturated capacity. |
| DR-3   | Re-dispatching by `job_id` alone can accept stale events/artifacts from a previous engine attempt after a heartbeat false-positive or reconnect race. | Added `BuildJobAttempt` and `attempt_id`; WSS events and artifact uploads are attempt-scoped (§§10, 24, 25, 26). |
| DR-4   | The draft said build logs mirror current `PipelineStage` log behavior, but current coreapp stores stage logs in capped DB text, not R2. | Made external build-log storage an explicit new capability with optional stage log pointers (§§7.4, 10.1, 10.5, 26). |
| DR-5   | Stale CA/mTLS references survived after the v0.3 self-signed fingerprint decision. Registration also cannot require client-cert validation before the cert is registered. | Fixed auth wording: registration is HTTPS + one-time token; steady-state uses client cert fingerprint + JWT. TLS termination remains OQ-P. |
| DR-6   | Current SQLAlchemy enums use `native_enum=False`; the Alembic sketch's raw MySQL `ALTER ... ENUM(...)` would not match the existing schema. | Replaced raw enum alteration with model-code enum update plus dialect-aware column alteration guidance (§26). |
| DR-7   | Build root, no-build publish directory, and post-build output directory were blurred together. Monorepos need a first-class build root. | Added `root_directory` to build config and job payloads (§§10, 12, 24, 25, 26). |
| DR-8   | Artifact and log staging assumes R2, while static-site storage is already multi-provider through `StorageTarget`. | Added OQ-O to decide whether build staging is a platform bucket or the site's storage target. |
| DR-9   | The backend WSS hub design assumes one backend process. Multiple API replicas need a routing story for engine sockets. | Added OQ-Q for sticky routing vs Redis-command fanout. |
| DR-10  | Framework scope drifted: Zola appears in detection but not images/tests; Angular appears in the registry but not acceptance fixtures; Remix was marked impossible despite current SPA-mode support. | Updated framework table/matrix and added OQ-R to lock the v1 ship list against official docs and smoke fixtures. |
| DR-11  | `NETWORK_FULL` was underspecified for untrusted build code. Even "full internet" should not imply access to metadata services, Docker bridge, or internal platform networks. | Added baseline egress blocks and OQ-S for abuse controls (§§8.5, 13). |
| DR-12  | Build-time secrets lacked validation limits and redaction caveats. | Added key/value size limits, reserved-name rules, and "redaction is best effort" language (§13.2). |

---

## 4. High-Level Architecture

```diagram
                                          ╭──────────────────────────╮
                                          │  Browser (Vue SPA)       │
                                          ╰─────────────┬────────────╯
                                                        │ existing pipeline WS
                                                        ▼
╭────────────────────────╮   REST + internal events    ╭─────────────────────────────╮
│  Backend (FastAPI)     │◀───────────────────────────▶│  Mincemeat platform         │
│  - BuildEngine model   │                              │  (R2, KV, Cloudflare Worker)│
│  - BuildJob dispatcher │                              ╰─────────────────────────────╯
│  - Round-robin policy  │
│  - Heartbeat watcher   │
╰─────────┬──────────────╯
          │  outbound-only client-cert + JWT WSS, initiated by engines
          ▼
╭──────────────────────────────────────────────────────────────────────╮
│  Build Engine host (Ubuntu LTS 24.04 / 26.04, amd64)                 │
│  systemd: build-engine.service (PyInstaller --onefile binary)        │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  build-engine                                                  │  │
│  │  ┌──────────────┐  ┌────────────────┐  ┌─────────────────────┐ │  │
│  │  │ Uplink       │  │ Durable queue  │  │ Docker executor      │ │  │
│  │  │ cert+JWT WSS │──▶ (SQLite WAL)   │──▶ (curated GHCR image) │ │  │
│  │  │ pulls jobs   │  │ persists state │  │ stdout/stderr stream │ │  │
│  │  └──────────────┘  └────────────────┘  │ artifact + sha256    │ │  │
│  │                                         ╰─────────────────────╯ │  │
│  │                                                                 │  │
│  │  ┌────────────────────────────────────────────────────────────┐ │  │
│  │  │  Async runtime: asyncio + httpx + websockets + aiosqlite   │ │  │
│  │  │  Parallel workers = max_concurrency (default 2)            │ │  │
│  │  └────────────────────────────────────────────────────────────┘ │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                          │                                           │
│                          ▼                                           │
│        Local Docker daemon (UNIX socket, exclusive)                  │
╰──────────────────────────────────────────────────────────────────────╯
```

### 4.1 Engine vs platform Worker — division of labor

| Concern                  | Existing `worker/` (Dramatiq)                       | New `build-engine/`                                |
|--------------------------|-----------------------------------------------------|----------------------------------------------------|
| Co-location              | Platform VPC                                        | Our build-host fleet (still our infra)             |
| Workload                 | LXD ops, polling, domain checks, deploys             | Heavy npm/Node/Docker builds                       |
| Runtime                  | Python 3.14 + uv venv + Dramatiq + Redis             | Python 3.14 PyInstaller `--onefile`, no Dramatiq, embedded SQLite queue |
| Dependencies             | Backend-aligned                                     | Minimal — only what the engine + executor need     |
| Networking               | Inside backend network                              | Outbound-only client-cert + JWT WSS                 |
| Failure blast radius     | Affects platform                                    | Local to engine; platform stays healthy            |

### 4.2 Stage placement

Current GitHub pipeline stages
(`PREPARE → FETCH → VALIDATE → UPLOAD → ACTIVATE → FINALIZE`,
from [github-deployments.md](docs/static-sites/github-deployments.md))
become:

```
PREPARE → FETCH → VALIDATE → BUILD → UPLOAD → ACTIVATE → FINALIZE
```

`BUILD` is **always** present for **new** pipelines created after the build
engine ships (resolution OQ-1). Historical pipelines keep their existing
six-stage skeleton and the frontend must tolerate both shapes:

- For **no-build** sources (today's UPLOAD-from-zip and direct GitHub-no-build
  links), `BUILD.status = SKIPPED` with `skip_reason = NO_BUILD_REQUIRED`.
- For **buildable** sources, `BUILD` runs on a registered engine, produces a
  build-output tarball, and writes its artifact reference into the
  `BuildJob`/`BuildJobAttempt` rows. The `UPLOAD` stage then validates that
  artifact and promotes the resulting static files into the deployment prefix.

`VALIDATE` is broadened to also classify the project mode
(`NO_BUILD` | `BUILD_REQUIRED` | `BUILD_INCOMPATIBLE`) using fast detection
(see §6). Implementation detail from the v0.5 review: the existing
`VALIDATE` stage currently reads the publish directory into memory for the
no-build path. Build-required sources must split validation into:

1. **Archive/source validation**: path traversal, blocked file types,
   extracted byte/file limits, source root resolution.
2. **Project classification**: framework/PM detection and compatibility
   warnings.
3. **No-build publish validation** only when the project is classified as
   `NO_BUILD`. For `BUILD_REQUIRED`, defer deployable-file validation until
   the engine artifact returns.

---

## 5. Components and Repository Layout

### 5.1 New repository: `mincemeat-id/build-engine`

The build-engine lives in its own repo (resolution OQ-2). Proposed layout:

```
build-engine/
├── pyproject.toml          # Python 3.14, uv, PyInstaller config
├── src/build_engine/
│   ├── __init__.py
│   ├── main.py             # CLI entrypoint: serve | register | doctor | status
│   ├── config.py           # pydantic-settings, layered config
│   ├── agent/
│   │   ├── uplink.py       # client-cert + JWT WSS client to backend
│   │   ├── heartbeat.py
│   │   ├── job_loop.py
│   │   └── auth.py         # JWT lifecycle, cert pinning
│   ├── detect/
│   │   ├── package_json.py
│   │   ├── lockfiles.py
│   │   ├── framework.py    # FrameworkProfile registry
│   │   └── compatibility.py# is-static-buildable
│   ├── executor/
│   │   ├── docker_runner.py  # docker SDK wrapper
│   │   ├── images.py
│   │   ├── workspace.py
│   │   ├── stream.py
│   │   ├── artifact.py
│   │   ├── validate.py     # post-build validation
│   │   └── cache.py        # per-site cache mount lifecycle
│   ├── queue/
│   │   ├── store.py        # aiosqlite-backed durable queue
│   │   ├── leases.py       # at-most-once handoff w/ visibility timeout
│   │   └── dlq.py          # poison-job parking
│   ├── ipc/
│   │   └── status_bus.py   # in-process pub/sub
│   ├── metrics/
│   │   ├── collector.py    # gauges/counters/timers
│   │   └── reporter.py     # 15s rollup push via uplink
│   └── cli/
│       └── commands.py
├── tests/
├── images/                 # Dockerfiles (also published via separate repo, see §8)
│   └── README.md           # points to ghcr.io/mincemeat-id/build-engine-images
├── packaging/
│   ├── systemd/build-engine.service
│   ├── pyinstaller/        # specs, hidden-imports, datas
│   └── deb/                # later: .deb packaging
└── scripts/
    ├── build_binary.sh
    └── ci/
```

### 5.2 Repository for builder images: `mincemeat-id/build-engine-images`

Separate public repo (resolution OQ-9) so images are auditable and lifecycle
is independent of the engine binary:

```
build-engine-images/
├── node20/Dockerfile
├── node22/Dockerfile
├── bun/Dockerfile
├── hugo/Dockerfile
├── manifest.json           # supported image -> tag mapping per build-engine version
└── .github/workflows/
    ├── build-and-publish.yml   # builds + SBOM + provenance via cosign
    └── trivy-scan.yml
```

All images published to `ghcr.io/mincemeat-id/build-engine-images/<name>:<tag>`.

### 5.3 Coreapp (`mincemeat-id/coreapp`) changes

Each is detailed in §10 (backend), §11 (worker), §12 (frontend).

---

## 6. Framework And Package-Manager Detection

### 6.1 Two-stage detection (resolution OQ-5)

| Stage           | Where    | Inputs                                              | Outcome                                  |
|-----------------|----------|-----------------------------------------------------|------------------------------------------|
| **Fast-fail**   | Backend  | Fetched tarball (already on platform after `FETCH`) | Decides `NO_BUILD` / `BUILD_REQUIRED` / `BUILD_INCOMPATIBLE`; sets the `BUILD` stage's `skip_reason` or dispatches job |
| **Comprehensive** | Engine | Extracted source on the engine host                 | Re-detects, picks final image/cmd/output, may downgrade with structured error before launching container |

If the engine detects something inconsistent with the backend pre-detection
(e.g. user replaced `next.config.js` between FETCH and BUILD — which
shouldn't happen because we use the fetched tarball — or the backend used a
stale heuristic), the engine wins and emits a `BUILD_INCOMPATIBLE` error
with the precise mismatch reason. The job is marked `FAILED` with class
`USER_CONFIG_INVALID`.

### 6.2 Detection inputs (priority)

1. `SiteBuildConfig.root_directory` — optional path inside the repository
   where build detection and commands run. Defaults to repository root.
2. `package.json` — `dependencies`, `devDependencies`, `scripts`,
   `packageManager`, `engines.node`.
3. Lockfiles — `bun.lockb`, `pnpm-lock.yaml`, `yarn.lock`, `package-lock.json`.
4. Framework config files — `next.config.*`, `astro.config.*`,
   `nuxt.config.*`, `vite.config.*`, `gatsby-config.*`, `svelte.config.*`,
   `angular.json`, `eleventy.config.*`, `hugo.toml`, `config.toml`, etc.
5. Output-directory hints — `out/`, `dist/`, `build/`, `public/`, `_site/`,
   `.output/public/`.

### 6.3 Framework registry (v1 candidate ship list — resolution OQ-4, reviewed)

Per OQ-4, the intent is to ship a broad static-compatible set plus the
**Generic** fallback. v0.5 review adds a stricter release rule: a framework is
GA in v1 only if it has a curated image, positive smoke fixture, negative
fixture where relevant, and user-facing docs. Frameworks that require special
config are shipped with **warning + actionable instructions** rather than
being silently rejected. The exact GA list is OQ-R.

| Framework  | Static-compatible | Default build               | Default output       | v1 notes                                              |
|------------|-------------------|-----------------------------|----------------------|-------------------------------------------------------|
| Astro      | ✅                | `astro build`               | `dist`               | Default static.                                       |
| Vite       | ✅                | `vite build`                | `dist`               | Default static.                                       |
| Eleventy   | ✅                | `eleventy`                  | `_site`              |                                                        |
| Docusaurus | ✅                | `docusaurus build`          | `build`              |                                                        |
| VitePress  | ✅                | `vitepress build`           | `.vitepress/dist`    |                                                        |
| VuePress   | ✅                | `vuepress build`            | `dist`               |                                                        |
| Gatsby     | ✅                | `gatsby build`              | `public`             |                                                        |
| Hugo       | ✅ (non-Node)     | `hugo`                      | `public`             | Uses `hugo` image, not Node.                          |
| Zola       | ⚠ v1 candidate    | `zola build`                | `public`             | Requires a `zola` image and smoke fixture before GA; see OQ-R. |
| Next.js    | ⚠ requires `output: 'export'` | `next build`        | `out`                | If missing flag → `BUILD_INCOMPATIBLE` with guide.    |
| Nuxt       | ⚠ requires `nuxi generate` | `nuxi generate`        | `.output/public`     | If repo uses `nuxi build` (SSR) → guide to switch.    |
| SvelteKit  | ⚠ requires `@sveltejs/adapter-static` | `vite build` | `build`              | If wrong adapter → guide.                              |
| Angular    | ⚠ requires static output config | `ng build`     | `dist/<project>`     | Guide for `outputMode: "static"` / prerendering and output path. |
| Remix SPA  | ⚠ requires Vite SPA mode | `remix vite:build` | `build/client`       | If SSR/server loaders/actions are present → guide.    |
| Remix SSR  | ❌                | n/a                         | n/a                  | `BUILD_INCOMPATIBLE` with link to SPA-mode/static alternatives.|
| Generic    | ✅ (fallback)     | `<pm> run build`            | **post-build inferred** (resolution OQ-M) | When `package.json` has a `build` script but no recognized framework. After build, engine scans `out/`, `dist/`, `build/`, `public/`, `_site/`, `.output/public/` in order, picks the first containing `index.html`, and persists the result to `SiteBuildConfig.detected_output_dir` for subsequent builds. User-overridable. If none match, fails with `USER_OUTPUT_INVALID` + guidance. |

**User-actionable warnings** are produced by `detect/compatibility.py` and
surfaced both in the backend pre-detection response and in the engine's
`BUILD_INCOMPATIBLE` error payload. Each warning carries:

```json
{
  "code": "NEXTJS_REQUIRES_EXPORT",
  "title": "Next.js project is not configured for static export",
  "what_we_saw": "next.config.js does not set output: 'export'",
  "how_to_fix": "Add `output: 'export'` to next.config.js and ensure no API routes are used.",
  "docs_url": "https://docs.mincemeat.id/static-sites/frameworks/nextjs"
}
```

### 6.4 Package-manager detection (resolution OQ-6)

Priority:

1. `packageManager` field in `package.json` (Corepack honors this exactly).
2. Lockfile presence: `bun.lockb` → bun, `pnpm-lock.yaml` → pnpm,
   `yarn.lock` → yarn, `package-lock.json` → npm.
3. Fallback to npm.

Per OQ-6, **Corepack is enabled inside images** so PM versions are honored
faithfully. We measure pull/install time during v1 and may pin in v1.x if
the cost is unacceptable.

Standardized invocations:

| PM    | Install                                                                 | Build              |
|-------|-------------------------------------------------------------------------|--------------------|
| npm   | `npm ci` (fallback `npm install` if no lockfile)                        | `npm run build`    |
| pnpm  | `pnpm install --frozen-lockfile`                                         | `pnpm run build`   |
| yarn  | berry: `yarn install --immutable`; classic: `yarn install --frozen-lockfile` | `yarn build` |
| bun   | `bun install --frozen-lockfile`                                          | `bun run build`    |

Node image selection:

1. If `SiteBuildConfig.node_version` is set, use it if it maps to a supported
   image (`20` or `22` in v1).
2. Else inspect `package.json.engines.node`; choose the newest supported image
   satisfying the range.
3. Else default to Node 22.

If the requested range excludes every supported image, fail during
comprehensive detection with `USER_CONFIG_INVALID` and a guidance payload
that names the supported Node majors.

---

## 7. Async, Queue, and Real-Time Status

### 7.1 No external broker in the engine (resolution OQ-3)

The engine does **not** use Dramatiq or Redis. Instead it ships an embedded,
file-backed durable queue:

```
/var/lib/build-engine/queue.sqlite        (aiosqlite, journal_mode=WAL, synchronous=NORMAL)
```

A single `jobs` table holds queue state. Outline:

```sql
CREATE TABLE jobs (
    job_id          TEXT PRIMARY KEY,
    pipeline_id     TEXT NOT NULL,
    site_id         TEXT NOT NULL,
    payload_json    TEXT NOT NULL,        -- backend job spec
    state           TEXT NOT NULL,        -- QUEUED|LEASED|RUNNING|DONE|FAILED|CANCELLED
    lease_owner     TEXT,                  -- engine worker slot id
    lease_expires_at INTEGER,              -- epoch sec; visibility timeout
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_seq        INTEGER NOT NULL DEFAULT 0,
    enqueued_at     INTEGER NOT NULL,
    started_at      INTEGER,
    finished_at     INTEGER,
    error_code      TEXT,
    error_message   TEXT
);
CREATE INDEX jobs_state_lease ON jobs(state, lease_expires_at);
```

Workers acquire a job via `UPDATE ... WHERE state='QUEUED' RETURNING ...`
inside a transaction (SQLite supports `RETURNING` since 3.35; Ubuntu 24.04
ships 3.45+). Lease/heartbeat semantics guarantee at-most-once execution
per attempt; if a worker crashes the lease expires and the job is reclaimable.

Why this satisfies the requirements:

- **No external dependencies** beyond the Python stdlib + `aiosqlite`.
- **Persistent**: state survives restart and crash.
- **Async-first**: `aiosqlite` integrates cleanly with `asyncio`.
- **Simple ops**: nothing to install, nothing to scale separately, single
  file you can `cp` to debug.

Limits and trade-offs (acknowledged):

- Single-host only — which is what we want. Each engine has its own queue.
- Concurrency is process-internal — fine for `max_concurrency = 2`.
- A poison job that crashes the executor is parked into a DLQ table after
  `MAX_ATTEMPTS = 3` and surfaced via metrics and the admin UI.

### 7.2 Two-queue topology end-to-end

| Queue                              | Where             | Purpose                                                                                             |
|------------------------------------|-------------------|-----------------------------------------------------------------------------------------------------|
| Backend Dramatiq queue (Redis)     | Platform          | Runs the pipeline orchestrator. The `BUILD` stage's "actor" enqueues a `BuildJob` row and waits on events. |
| Backend dispatcher                 | Platform          | Round-robin selects an `ONLINE` engine, marks job `ASSIGNED`, pushes to the engine over WSS.        |
| Engine local queue (SQLite WAL)    | Inside engine     | Accepts pushed jobs; workers (default 2) lease + execute; persistence survives restart.             |

Rationale: the backend never blocks waiting on a remote engine, and the
engine survives short backend outages — pending jobs drain when the uplink
reconnects.

### 7.3 Job lifecycle

```diagram
[Pipeline reaches BUILD stage actor]
      │
      ▼
[Dispatcher: persist BuildJob(QUEUED), create BuildJobAttempt(attempt=1),
 select engine if immediately available]
      │
      ▼ (push over WSS with build_job_id + attempt_id)
[Engine receives attempt → enqueue local SQLite (QUEUED)]
      │
      ▼ (worker leases)
[Engine ACK → BuildJob.status=ASSIGNED → RUNNING]
      │
      ▼
[Engine runs container; streams events]
      │   statuses: PREPARING → INSTALLING → BUILDING → PACKAGING → VALIDATING → UPLOADING_ARTIFACT
      ▼
[Engine post-build validation passes locally]
      │
      ▼
[Engine PUTs artifact to backend-issued presigned URL]
      │
      ▼
[Engine ACK SUCCEEDED + attempt_id + artifact ref + sha256 + size]
      │
      ▼
[Backend accepts only the current attempt_id, re-validates artifact
 (size, sha256, archive safety, presence of index.html)]
      │
      ▼
[BUILD stage SUCCESS → UPLOAD stage proceeds with build artifact as input]
```

If the engine crashes or the heartbeat goes stale:
- Backend marks engine `OFFLINE` after **3 missed beats = 45 s**.
- Backend marks the **current `BuildJobAttempt`** `FAILED` with
  `error_code = ENGINE_LOST` immediately (no long stall).
- If retry policy allows another attempt, the parent `BuildJob` returns to
  `QUEUED`, a new `BuildJobAttempt` is created, and the dispatcher tries a
  different compatible engine. The final `BuildJob` becomes `FAILED` only
  after attempts are exhausted.
- Stale events/artifacts from earlier attempts are ignored because every
  event and presigned upload URL is bound to `attempt_id`.

### 7.4 Real-time status pipe (resolution OQ-7 & OQ-8)

```diagram
Engine executor ──in-proc bus──▶ Uplink ──cert+JWT WSS──▶ Backend uplink WS endpoint
       │                                                          │
       │                                                          ▼
       │                                            Redis Pub/Sub (existing fanout)
       │                                                          │
       ▼                                                          ▼
local rotating log file                          Existing pipeline WS to browser
                                                  AND object-storage persistence for full log
```

Events are append-only and monotonically sequenced per `BuildJobAttempt`:

```json
{
  "build_job_id": "uuid",
  "attempt_id": "uuid",
  "seq": 142,
  "ts": "2026-05-19T07:00:01Z",
  "type": "LOG",
  "stream": "stdout",
  "data": "npm warn deprecated ..."
}
```

Event `type`s: `STATUS`, `LOG`, `METRIC`, `WARNING`, `ARTIFACT_READY`,
`ERROR`, `HEARTBEAT`, `CACHE_HIT`, `CACHE_MISS`.

Persistence (resolution OQ-7, corrected in v0.5 review):

- Current coreapp stage logs are stored in `pipeline_stages.log_text` and
  capped by `PIPELINE_STAGE_LOG_MAX_BYTES`; there is no existing R2-backed
  stage log store.
- **Live tail**: last N KB held in Redis under the pipeline event channel
  and mirrored into `PipelineStage.log_text` as a capped tail so the current
  API shape remains useful.
- **Full build log**: backend appends to
  `sites/{site_id}/build-logs/{build_job_id}/{attempt_id}.log` in the
  chosen build-log staging store (see OQ-O). On job finalize, log is gzipped
  to `.log.gz`; `PipelineStage.log_storage_key` points at it.
- Frontend reuses the existing
  `GET /api/v1/sites/{site_id}/pipelines/{pipeline_id}/stages/{stage_id}/logs`
  endpoint. The endpoint returns `PipelineStage.log_text` for ordinary stages
  and transparently serves the external full BUILD log when
  `log_storage_key` is present.

WebSocket reuse (resolution OQ-8): the build stage's events flow into the
existing pipeline WebSocket
(`/api/v1/sites/{site_id}/pipelines/{pipeline_id}/ws`) with a `stage_id`
matching the `BUILD` stage row. Frontend does **not** need a new socket.

---

## 8. Docker Executor

### 8.1 Curated builder images (resolution OQ-9)

Images live in the dedicated public repo
`mincemeat-id/build-engine-images` and are published to
`ghcr.io/mincemeat-id/`. v1 set:

- `ghcr.io/mincemeat-id/build-engine-images/node:20`
- `ghcr.io/mincemeat-id/build-engine-images/node:22`
- `ghcr.io/mincemeat-id/build-engine-images/bun:latest`
- `ghcr.io/mincemeat-id/build-engine-images/hugo:latest`

Each image contains: runtime, common build tools (git, tar, curl,
ca-certificates), Corepack-enabled PMs, a thin `/build-entrypoint.sh`
that reads a JSON manifest from `/build/manifest.json`.

Auditability requirements (per OQ-9):

- **No secrets** baked into images, ever.
- **SBOM** (CycloneDX) attached at publish via `cosign attach sbom`.
- **Provenance** (SLSA v1) attached via cosign keyless.
- **Trivy** scan in CI; failing CVE budget blocks publication.
- A `manifest.json` at repo root pins which engine versions accept which
  image tags so we can roll forward/back safely.

### 8.2 Per-job container model

For each job, the engine:

1. Creates a workspace `/var/lib/build-engine/jobs/{job_id}/`.
2. Downloads the fetched source tarball from the backend-issued
   `source_download_url`, verifies `source_sha256`, and extracts it into
   `workspace/src/`.
3. Resolves `root_directory` inside `workspace/src/` and rejects any path
   traversal, symlink escape, or missing directory.
4. Writes `manifest.json` with build command, output dir, env vars, secret
   references, framework profile.
5. Mounts the **per-site cache directory** (see §8.3) read-write at the
   expected PM cache path inside the container.
6. Runs the resolved image with:
   - `--rm`
   - `--memory=2g --memory-swap=2g`
   - `--cpus=1.0`
   - `--pids-limit=1024`
   - `--read-only` root FS, tmpfs at `/tmp` (size 256 MiB)
   - `--user 1000:1000`
   - `--cap-drop=ALL`
   - `--security-opt=no-new-privileges`
   - `--security-opt seccomp=<default>` + AppArmor profile
   - **No** `/var/run/docker.sock` mount, ever.
   - Mounts: `workspace/src` (rw), `workspace/out` (rw),
     `cache/{site_id}` (rw, scoped).
7. Captures stdout/stderr line-by-line; forwards through the event bus.
8. Enforces **10 min wallclock** (OQ-11). On timeout: SIGTERM, 10 s grace,
   SIGKILL.
9. Runs **post-build validation** (size, count, sha256, archive safety,
   `index.html` presence, presence of expected output dir).
10. On success: tars `workspace/out/` into an artifact, computes sha256, size,
   uploads via a backend-issued presigned URL bound to
   `(build_job_id, attempt_id, engine_id)` in the chosen staging store
   (see OQ-O).
11. Cleans up workspace (subject to retention policy for debugging — keep
    last 5 by default).

### 8.3 Build cache (resolution OQ-10)

Goals: massively speed up incremental builds; never let one site read or
mutate another site's cache.

| Property                | Value                                                              |
|-------------------------|--------------------------------------------------------------------|
| Cache scope             | Per `site_id`. **Never** cross-site.                              |
| Location on engine      | `/var/lib/build-engine/cache/{site_id}/...`                       |
| Contents (v1)           | **PM caches only** (resolution OQ-A): `npm/_cacache`, `pnpm/store/v3`, `yarn/cache`, `bun/install/cache`. **No `node_modules` shadow** in v1. |
| Per-site size cap       | **5 GiB** (configurable per engine)                                |
| Total engine cap        | `5 GiB × min(active sites, cache_max_sites)`; LRU eviction across sites |
| TTL                     | **30 days** since last access; pruned on a background timer       |
| Integrity safeguard     | Pre-flight sha256 of lockfile snapshot recorded with the cache; on mismatch the cache is invalidated for that site rather than reused |
| Poisoning safeguards    | Cache only mounted into containers running the **same site_id**; cache prune triggered after `FAILED` build with `error_code in {EXEC_OOM, EXEC_TIMEOUT, INTEGRITY_FAILED}` |
| Disable switch          | Per-site flag `build_cache_enabled` (default true); per-engine kill-switch in admin UI |
| Re-enable behavior      | (Resolution OQ-F.) If `build_cache_enabled` is flipped `false → true`, the engine **wipes and rebuilds** the per-site cache on the next job. Stale data from before the disable is never reused. |

A `cache reset` admin action invalidates a site's cache by `rm -rf`'ing
`/var/lib/build-engine/cache/{site_id}/` on every engine, dispatched as a
control message over the engine WSS uplink.

`node_modules` shadow caching is **future scope** (revisit in v1.x once we
measure real-world install times with PM-cache-only).

### 8.4 Concurrency, parallelism, and scheduling

- Each engine declares `max_concurrency` at registration; default **2**
  (per product requirement).
- Engine spawns N worker tasks; each leases one job at a time from the
  SQLite queue.
- Backend dispatcher uses **round-robin** (resolution; v1) across
  `ONLINE` engines with `capacity_remaining > 0` and matching capabilities
  (image set, engine version range).
- The backend never dispatches more jobs to an engine than
  `max_concurrency - currently_running` (tracked via heartbeats + ACKs).

### 8.5 Network policy

`NETWORK_FULL` is the default product mode in v1 (resolution OQ-12), but it
must still include platform safety blocks. "Full" means outbound internet for
package installs and framework fetches; it does **not** mean access to host or
platform internals.

Baseline v1 blocks:

- Cloud metadata IP ranges, especially `169.254.169.254`.
- Docker bridge, host gateway, and engine host private addresses.
- RFC1918 / RFC4193 ranges that belong to the platform control plane.
- Backend, MariaDB, Redis, MinIO, and Nomad private service networks unless
  explicitly routed through the public API endpoint.

Documented as a v2 candidate: per-site/package-registry allowlists and an
egress proxy with accounting. Abuse controls remain an open question in OQ-S.

### 8.6 Cancellation (resolution: SIGTERM-then-KILL)

- Backend sends a cancel message over WSS with the `job_id`.
- Engine flips job state to `CANCELLING`, sends SIGTERM to the container,
  waits **10 s grace**, then SIGKILL.
- Job is marked `CANCELLED`. Artifact (if partial) is deleted.
- Pipeline cancel is best-effort and respects existing cooperative
  cancellation semantics in `github-deployments.md`.

---

## 9. NAT-Friendly Backend ↔ Engine Communication

### 9.1 Channels

| Channel              | Direction          | Transport              | Purpose                                          |
|----------------------|--------------------|------------------------|--------------------------------------------------|
| Registration         | Engine → Backend   | HTTPS POST + one-time token | Exchange one-time registration token and self-signed cert for `engine_id` + session JWT. Client-cert validation starts **after** this call. |
| Heartbeat            | Engine → Backend   | HTTPS POST every **15 s** | Liveness + capacity advertisement              |
| Job push / status    | Engine ↔ Backend   | **WSS** (client cert fingerprint + JWT) | Backend pushes assigned attempts; engine streams status/log events |
| Artifact upload      | Engine → Storage    | HTTPS PUT to backend-issued presigned URL keyed by `(build_job_id, attempt_id)` | Deliver build output |
| Token rotation       | Engine → Backend   | HTTPS POST             | Rotate short-lived session JWT                  |
| Control messages     | Backend → Engine   | over the same WSS      | Cancel, cache-reset, drain                      |

### 9.2 Identity and auth (resolution OQ-14 + OQ-H)

Mirrors the existing **LXD server** auth pattern in coreapp
([`shared/src/shared/models/lxd_server.py`](shared/src/shared/models/lxd_server.py),
[`backend/src/app/services/server/certificates.py`](backend/src/app/services/server/certificates.py)).
No internal CA, no PKI, no CRL.

**Registration handshake:**

1. Admin generates a one-time registration token in the backend UI.
2. Operator runs `build-engine register --token <token>`.
3. The engine generates a fresh **self-signed RSA-3072 (or ed25519)
   certificate + private key** locally:
   - Common Name: `build-engine/{engine_name}`
   - Subject Alternative Name: the engine's hostname
   - Validity: 20 years (we operate the hosts; rotation is manual)
4. The engine POSTs the registration token + its self-signed cert PEM to
   `/api/v1/build-engines/agent/register`.
5. Backend stores the **cert PEM encrypted at rest** (same AES-256-GCM +
   HKDF-SHA256 scheme as LXD certs, using `CERT_MASTER_KEY`) plus the
   **fingerprint** (sha256 hex of the DER form) on the `BuildEngine` row.
6. Backend returns the `engine_id`, the **backend's TLS leaf certificate
   fingerprint** (for pinning), and an initial **session JWT**.

**Steady-state auth on every uplink call:**

- TLS connection from engine to backend uses the engine's self-signed cert
  as the **client certificate**. Backend's TLS terminator or trusted edge
  component must expose the verified peer certificate to FastAPI so the app
  can validate by **fingerprint match against the stored
  `BuildEngine.fingerprint`** rather than by walking a chain (exactly what
  LXD's TLS trust does). The exact production termination topology is OQ-P.
- The engine pins the backend's leaf certificate fingerprint it received at
  registration; refuses connections that present a different leaf cert.
- Application-level auth: short-lived **session JWT** (e.g. 1 h) issued via
  `POST /api/v1/build-engines/agent/sessions` (engine secret + cert →
  JWT). JWT carries `engine_id`, `proto_version`, and capability digest.
- An attacker who steals only the JWT cannot connect — the TLS handshake
  fails without the private key. An attacker who steals only the cert
  cannot mint a JWT — the engine secret is required.

**Why this is simpler than the v0.2 CA-based design:**

| Concern              | CA-based (v0.2 proposal) | Self-signed + fingerprint (v0.3, this) |
|----------------------|--------------------------|-----------------------------------------|
| CA hosting           | Vault / AWS PCA / offline CA | None — backend just stores a fingerprint |
| Cert issuance        | Backend mints, signs, delivers | Engine self-mints at registration       |
| Revocation           | CRL or OCSP              | Mark `BuildEngine.status = DISABLED`; fingerprint no longer matches in DB |
| Rotation             | CA lifecycle             | Re-run `register` with a fresh token    |
| Operator burden      | High                     | Low — same as adding an LXD server      |

**Encryption at rest:** the engine's cert PEM is stored using the existing
`encrypt_certificate` helper from `shared/crypto.py`; same fields
(`cert_encrypted`, `cert_iv`, `cert_tag`, `fingerprint`) as `LxdServer`.
The engine's **private key never leaves the engine host**.

**Disabling / revoking:** admin sets `BuildEngine.status = DISABLED`. The
backend's TLS handler refuses connections whose presented cert fingerprint
maps to a non-`ONLINE`/non-`DRAINING` `BuildEngine` row. The fingerprint
remains in the DB so that revocation can be audited and undone if needed.

### 9.3 No long-poll fallback (resolution OQ-15)

WSS is required. We control the network. If a host cannot establish WSS the
engine refuses to start and prints an actionable error.

### 9.4 Idempotency and replay

- Every uplink request carries an `Idempotency-Key` header equal to
  `(engine_id, build_job_id, attempt_id, seq)` or
  `(engine_id, request_uuid)` for non-job calls.
- Status events have monotonically increasing `seq` per `attempt_id`.
- Backend records `BuildJobAttempt.last_seq` and drops anything not strictly
  greater. Events for stale attempts are audit-only and cannot transition the
  parent job.

### 9.5 Wire-protocol versioning

- Engine sends `X-Build-Engine-Proto: v1` on every request.
- Backend rejects unsupported protocol versions during the handshake with a
  structured error telling the operator to upgrade the engine.
- Each engine release pins a `proto_min`/`proto_max` range.

---

## 10. Backend (`coreapp/backend/`) Changes

### 10.1 New SQLAlchemy models in `shared/src/shared/models/`

| Model                | Purpose                                                                   |
|----------------------|---------------------------------------------------------------------------|
| `BuildEngine`        | Registered engines. Mirrors `LxdServer` cert fields: `cert_encrypted`, `cert_iv`, `cert_tag`, `fingerprint` (sha256 hex, unique). Plus: `id`, `name`, `status` (`PENDING`/`ONLINE`/`OFFLINE`/`DISABLED`/`DRAINING`/`QUARANTINED`), `version`, `proto_version`, `image_manifest_version`, `max_concurrency`, `labels` (JSON), `capabilities` (JSON), `metrics_json` (last-known snapshot), `last_seen_at`, `created_by_user_id`, `created_at`, `updated_at`. |
| `BuildEngineToken`   | One-time registration tokens (creator, expiry, consumed_at, token_hash).   |
| `BuildJob`           | Sibling of `Pipeline` (resolution OQ-20). FK to `pipeline_id`, `pipeline_stage_id`, `site_id`, plus nullable `current_engine_id` and `current_attempt_id`. Fields: `source_storage_key`, `source_sha256`, `root_directory`, framework_id, package_manager, image, build_command, output_dir, detected_output_dir, env_summary (keys only), artifact_storage_key, artifact_size, artifact_sha256, started_at, finished_at, error_code, error_message, error_class, attempts, cache_hit (bool). Parent job status reflects the overall user-visible build. |
| `BuildJobAttempt`    | One execution attempt for a `BuildJob`, bound to one engine. Fields: `id`, `build_job_id`, `attempt_number`, `engine_id`, `status`, `last_seq`, `assigned_at`, `started_at`, `finished_at`, `artifact_storage_key`, `artifact_size`, `artifact_sha256`, `error_code`, `error_class`, `error_message`. All WSS events and artifact upload URLs carry `attempt_id` so stale attempts cannot win races. |
| `BuildJobEvent`      | Thin audit table — status-changing events only, keyed by `build_job_id` and optional `attempt_id`. Full build logs go to external storage through `PipelineStage.log_storage_key`. |
| `BuildEngineMetric`  | (Resolution OQ-B.) New per-engine time-series rows pushed every 15 s. Columns: `id`, `engine_id` FK, `recorded_at`, `workers_busy`, `workers_total`, `queue_depth`, `cache_size_bytes`, `cache_hit_ratio`, `jobs_running`, `jobs_completed_total`, `docker_errors_total`, `uplink_reconnects_total`. Indexed on `(engine_id, recorded_at desc)`. Old rows pruned by an APScheduler job (default: keep 7 days). |
| `SiteBuildSecret`    | Encrypted env vars per site. Same AES-256-GCM + HKDF-SHA256 scheme as `StorageTarget`. HKDF context label: `build-engine-secret`. |
| `SiteBuildConfig`    | Per-site overrides: `root_directory`, `framework_override`, `build_command`, `output_dir`, `detected_output_dir` (resolution OQ-M; persisted after first successful Generic build), `node_version`, `build_cache_enabled`. |
| `PipelineStage` additions | `PipelineStageName` gains `BUILD`. `PipelineStage` gains nullable `log_storage_key`, `log_storage_bytes`, and `log_storage_compressed` so BUILD can expose full external logs while ordinary stages keep `log_text`. |

`Deployment.source` likely remains `GITHUB` for GitHub-sourced builds; the
fact that content passed through the build engine is better represented by
`PipelineStage.BUILD`, `BuildJob`, and `Deployment.deploy_metadata`. If product
analytics need a separate content-origin enum value, add `GITHUB_BUILD` rather
than overloading `BUILD`; see OQ-O/OQ-T.

Alembic migration: one revision that creates the new tables, updates the
Python/SQLAlchemy enum values for `PipelineStageName.BUILD`, adds optional
BUILD log pointer columns to `pipeline_stages`, and seeds none. Because the
current models use `native_enum=False`, enum changes are primarily model-code
changes plus dialect-aware `VARCHAR`/constraint updates where Alembic detects
them; do not use raw MySQL native `ENUM` DDL for these existing columns.

### 10.2 New service modules under `backend/src/app/services/`

- `build_engines.py` — register, list, disable, rotate token, drain, soft-delete.
- `build_dispatcher.py` — round-robin scheduler; called by the pipeline
  `BUILD` actor.
- `build_job_events.py` — apply incoming events from engines; enforce
  monotonic `seq`; fan out to existing pipeline pub/sub.
- `build_secrets.py` — CRUD for `SiteBuildSecret` with crypto helpers.
- `heartbeat_watcher.py` — APScheduler job that scans `BuildEngine`
  `last_seen_at`; marks `OFFLINE` after 45 s; cascades `ENGINE_LOST` to
  in-flight jobs and re-dispatches.

### 10.3 New HTTP/WS endpoints under `backend/src/app/api/v1/`

**Admin surface** (RBAC: admin-only):

| Method | Path                                                          | Purpose                                  |
|--------|---------------------------------------------------------------|------------------------------------------|
| `GET`    | `/api/v1/admin/build-engines`                                 | List engines with health/status          |
| `POST`   | `/api/v1/admin/build-engines/registration-tokens`             | Issue a one-time token                   |
| `GET`    | `/api/v1/admin/build-engines/{engine_id}`                     | Detail                                    |
| `PATCH`  | `/api/v1/admin/build-engines/{engine_id}`                     | Update name, labels, max_concurrency     |
| `POST`   | `/api/v1/admin/build-engines/{engine_id}/disable`             | Disable (drains in-flight)               |
| `POST`   | `/api/v1/admin/build-engines/{engine_id}/drain`               | Stop accepting new jobs                  |
| `POST`   | `/api/v1/admin/build-engines/{engine_id}/cache/reset`         | Reset cache (optionally per-site)         |
| `DELETE` | `/api/v1/admin/build-engines/{engine_id}`                     | Remove                                    |
| `GET`    | `/api/v1/admin/build-jobs`                                    | Global job history (filters: engine, site, status, time) |
| `GET`    | `/api/v1/admin/build-jobs/{job_id}`                           | Detail incl. logs link                   |
| `GET`    | `/api/v1/admin/build-engines/{engine_id}/jobs`                | Per-engine job list / queue              |

**Site-owner surface**:

| Method | Path                                                          | Purpose                                  |
|--------|---------------------------------------------------------------|------------------------------------------|
| `GET`    | `/api/v1/sites/{site_id}/build-config`                       | Read framework override + build settings  |
| `PUT`    | `/api/v1/sites/{site_id}/build-config`                       | Update                                    |
| `GET`    | `/api/v1/sites/{site_id}/build-secrets`                      | List secret keys (values redacted)        |
| `PUT`    | `/api/v1/sites/{site_id}/build-secrets/{key}`                | Upsert                                    |
| `DELETE` | `/api/v1/sites/{site_id}/build-secrets/{key}`                | Remove                                    |
| `POST`   | `/api/v1/sites/{site_id}/build-cache/reset`                  | Reset this site's cache on all engines   |

**Engine-facing uplink** (client cert + JWT, **not** subject to user RBAC):

| Method | Path                                                          | Purpose                                  |
|--------|---------------------------------------------------------------|------------------------------------------|
| `POST` | `/api/v1/build-engines/agent/register`                        | Bootstrap via registration token         |
| `POST` | `/api/v1/build-engines/agent/sessions`                        | Mint a session JWT (engine secret → JWT) |
| `POST` | `/api/v1/build-engines/agent/heartbeats`                       | Liveness + capacity advertisement        |
| `WS`   | `/api/v1/build-engines/agent/ws`                              | Job push + status/log/metric stream + control messages |
| `POST` | `/api/v1/build-engines/agent/jobs/{job_id}/attempts/{attempt_id}/artifact-upload-url` | Issue presigned object-storage PUT URL |
| `POST` | `/api/v1/build-engines/agent/jobs/{job_id}/attempts/{attempt_id}/ack` | Ack assigned, started, finished states |
| `POST` | `/api/v1/build-engines/agent/metrics`                         | 15s rollup metrics push                  |

### 10.4 Pipeline integration

- Pipeline executor adds a `BUILD` stage row to every new pipeline.
- The stage actor:
  1. Loads project mode set by `VALIDATE`.
  2. If `NO_BUILD_REQUIRED`, marks stage `SKIPPED` with `skip_reason` and
     proceeds. **No-build pipelines never touch the dispatcher and are
     unaffected by the build-engine fleet state** (resolution OQ-C). A site
     that only uses no-build deployments continues to function even if
     zero engines are registered or all engines are offline.
  3. If `BUILD_INCOMPATIBLE`, marks stage `FAILED` with the structured
     warning payload and aborts pipeline.
  4. Otherwise calls `build_dispatcher.dispatch(pipeline_id, stage_id)`.
- The `VALIDATE`/`UPLOAD` stages become input-aware:
  - No-build path: current behavior continues — validate `publish_directory`
    and populate `ctx.files` for upload.
  - Build path: validate source archive and classify project, but do **not**
    read the eventual deployable files into `ctx.files`; `BUILD` produces a
    staged artifact, and `UPLOAD` downloads and validates that artifact before
    promotion.

**Dispatcher behavior on unavailable vs saturated capacity (resolution OQ-C,
clarified in v0.5 review):**

The dispatcher computes the set of candidate engines as
`status == ONLINE AND proto matches AND image_manifest_version matches`.

If this compatible-online set is empty, the build cannot run anywhere now:

```
compatible-online set empty
       │
       ▼
mark BuildJob.status=FAILED
error_code=NO_ENGINE_AVAILABLE
error_class=PLATFORM_ERROR
       │
       ▼
mark BUILD stage FAILED with actionable message:
"No build engine is currently available to run this build.
 Contact your administrator."
       │
       ▼
abort pipeline (FAILED), do not retry
```

This is intentionally **fast-fail** to avoid silently stalled deploys.
Operators are alerted via the existing alerting that watches `BuildEngine`
heartbeats (every engine going `OFFLINE` should already be paging them).

If compatible engines exist but all are busy (`capacity_remaining == 0`), the
dispatcher must **not** fail immediately. That is a saturation queue, not a
fleet outage. The recommended behavior is:

```
compatible-online set non-empty
capacity_remaining == 0
       │
       ▼
keep BuildJob.status=QUEUED
BUILD stage remains RUNNING with phase=WAITING_FOR_ENGINE
       │
       ▼
dispatch on next heartbeat / job completion that opens capacity
```

The only missing product decision is the maximum time a BUILD stage may wait
for saturated capacity before failing; see OQ-N.

- Pipeline cancellation propagates a control message to the engine.

### 10.5 Retention (resolutions OQ-D and OQ-E)

- **Build artifacts** in the chosen staging store (R2 or site storage target;
  see OQ-O) follow the same retention as `Pipeline`
  records: a per-site retention worker keeps the `PIPELINE_RETENTION_PER_SITE`
  most recent pipelines plus the active deployment's pipeline. When a
  `Pipeline` row is pruned, its associated build artifact in
  `sites/{site_id}/build-artifacts/{job_id}.tar.gz` is deleted in the
  same transaction.
- **Build logs** in the chosen staging store
  (`sites/{site_id}/build-logs/{build_job_id}/{attempt_id}.log.gz`)
  follow the same lifecycle. The existing pipeline-retention worker is
  extended to also unlink build artifacts and gzipped logs when their
  owning pipeline is pruned.
- No separate TTL is introduced; lifecycle is entirely owned by pipeline
  retention.

### 10.6 Config additions (backend `.env.example`)

| Variable                              | Purpose                                                |
|---------------------------------------|--------------------------------------------------------|
| `BUILD_ENGINE_AGENT_JWT_TTL_SECONDS`  | Session JWT lifetime (default 3600)                    |
| `BUILD_ENGINE_HEARTBEAT_TIMEOUT_SECONDS` | Default 45                                         |
| `BUILD_ENGINE_LOG_STORAGE_PREFIX`     | Object-storage prefix for build logs                  |
| `BUILD_ENGINE_ARTIFACT_STORAGE_PREFIX` | Object-storage prefix for build artifacts             |
| `BUILD_ENGINE_MAX_ARTIFACT_BYTES`     | Default 524288000 (500 MiB)                           |
| `BUILD_ENGINE_MAX_BUILD_SECONDS`      | Default 600 (10 min)                                  |
| `BUILD_ENGINE_METRIC_RETENTION_DAYS`  | Default 7                                              |

> `CERT_MASTER_KEY` (already present) is reused to encrypt the engine's
> self-signed cert PEM at rest; no new key material is introduced.
> `BUILD_ENGINE_QUEUE_MAX_WAIT_SECONDS` is pending OQ-N. Dispatcher fails
> immediately only when no compatible engine is online.

---

## 11. Worker (`coreapp/worker/`) Changes

The platform worker still owns the pipeline lifecycle. v1 changes:

1. **New `BUILD` stage handler** in `worker/src/worker/pipeline/` that
   requests dispatch from the backend-owned build dispatcher and waits on
   the `BuildJobEvent` stream. The dispatcher must live with, or be able to
   route commands to, the backend process that owns engine WSS connections.
   The recommended v1 shape is an authenticated internal backend endpoint
   called by the worker; multi-replica routing is OQ-Q.
2. **Pre-build detection** added to the existing `VALIDATE` stage:
   `worker/src/worker/pipeline/validate.py` gains a
   `classify_project_mode(extracted_tarball_meta, root_directory)` function
   that returns `NO_BUILD | BUILD_REQUIRED | BUILD_INCOMPATIBLE` plus
   warnings. For build-required sources, it records source artifact metadata
   and does not populate `ctx.files`.
3. **Post-build re-validation** added: after the engine reports
   `ARTIFACT_READY`, the existing UPLOAD stage downloads the build artifact
   tarball, re-runs the current static-site validation routines (size,
   count, path traversal, presence of `index.html`), then proceeds to
   upload into `sites/{site_id}/deployments/{deployment_id}/` exactly as
   today.
4. **Heartbeat watcher**: an APScheduler job is added to the worker (since
   the worker already hosts APScheduler jobs) to drive
   `heartbeat_watcher.py`.
5. **Cancellation propagation**: when a pipeline is cancelled, the worker
   sends a cancel control message to the dispatcher, which fans it out
   over WSS.

No new external dependencies on the worker beyond what the backend pulls in.

---

## 12. Frontend (`coreapp/frontend/`) Changes

### 12.1 New admin pages

- **`/admin/build-engines`** — list view: name, status (ONLINE/OFFLINE/
  DISABLED/DRAINING/QUARANTINED), version, capacity used/max, labels,
  last seen, running jobs.
- **`/admin/build-engines/new`** — register flow: name + labels →
  one-time token → copy-to-clipboard + CLI snippet.
- **`/admin/build-engines/{id}`** — detail: capabilities, recent jobs,
  cache stats, audit log, disable/drain/reset cache actions.
- **`/admin/build-engines/{id}/jobs`** — per-engine queue + history with
  filters and a live "currently running" panel.
- **`/admin/build-jobs`** — global build-job history across all engines.

### 12.2 Site-owner pages

- **Site settings → Build**: framework override (with detection result
  shown), root directory, build command, output directory, Node version,
  "enable build cache" toggle, "reset build cache" button.
- **Site settings → Environment variables (build-time)**: key/value editor,
  values write-only after creation, with `is_secret` flag (always true in
  v1 since all stored encrypted).
- **Pipeline detail**: add the new `BUILD` stage to the existing timeline.
  Display:
  - Detected framework + PM + cache hit/miss.
  - Live log stream (reuses existing pipeline WS).
  - Engine name + version.
  - Duration breakdown (install vs build vs upload).

### 12.3 Components and contracts

- Generate TypeScript types via the existing `contracts:check` workflow
  from the new OpenAPI surface in §10.3.
- Reka UI primitives for the table / form patterns to match the rest of the
  admin surface.
- Pinia store: `useBuildEnginesStore`, `useBuildJobsStore`.

---

## 13. Security

### 13.1 Threat model summary (revised)

| Threat                                       | Mitigation                                                                          |
|----------------------------------------------|--------------------------------------------------------------------------------------|
| Malicious user code escapes container        | Non-root, read-only FS, no docker.sock, seccomp/apparmor, drop caps, pids-limit     |
| Token theft on engine host                   | Short-lived session JWTs; client certificate required even with valid JWT           |
| Compromised engine submits forged artifacts  | Artifact upload uses presigned URL bound to `(engine_id, build_job_id, attempt_id)`; backend re-verifies size + sha256 + archive safety + index.html |
| Replay of stale status events                | Monotonic `seq` per attempt; backend drops `seq <= last_seq` and rejects stale `attempt_id`s |
| Cache poisoning across sites                 | Cache strictly per `site_id`; not mounted into containers for other sites           |
| Cache poisoning within a site                | sha256 integrity check on cache metadata; auto-invalidate on integrity failure or OOM/KILL |
| Secrets leak to logs                         | Engine has a secret-scrubber over outbound log streams (token list shipped with the job) |
| Engine host compromise                       | Docker socket is only privileged access; engine runs as a dedicated UNIX user; AppArmor profile; client-cert fingerprint rejected on disable |
| Long-lived cert misuse                       | No CRL/PKI. Admin disables the `BuildEngine` row; backend rejects future sessions and WSS handshakes for that fingerprint. |
| Build code probes internal services          | `NETWORK_FULL` still blocks metadata, Docker bridge, host gateway, and platform private networks. |
| Build code abuses outbound internet          | Per-job CPU/RAM/time limits plus audit logs in v1; stronger egress accounting/allowlists are OQ-S. |

### 13.2 Build-time secrets (resolution OQ-16)

- Stored encrypted in `site_build_secret` (AES-256-GCM + HKDF-SHA256,
  context label `build-engine-secret`).
- Global per site in v1; no env separation.
- Backend ships decrypted values to the engine **only** as part of the job
  push, over client-cert/JWT protected WSS, and they are kept only in memory by the
  engine for the lifetime of the job. Never persisted on disk.
- Engine writes them into the container as env vars; never to disk inside
  the container either (the workspace mount itself is rw but the secrets
  are passed via `--env`).
- Engine log scrubber replaces any verbatim occurrences of secret values
  before forwarding logs. This is best-effort redaction, not a guarantee:
  transformed, encoded, truncated, or derived secrets may still leak if user
  code prints them.
- Secret keys must match `[A-Z_][A-Z0-9_]{0,127}` and cannot use reserved
  prefixes: `MINCEMEAT_`, `BUILD_ENGINE_`, `GITHUB_`, `AWS_`, `S3_`, `CF_`,
  `CLOUDFLARE_`.
- Secret values are capped at 16 KiB each, with a 128 KiB total env payload
  cap per build job.
- Public build-time variables such as `NEXT_PUBLIC_*` are still stored through
  `SiteBuildSecret` in v1 for implementation simplicity, but the UI must make
  clear that frameworks may embed them into the shipped client bundle.

### 13.3 Audit log

New `audit_log` entries:

- `build_engine.registered` / `disabled` / `drained` / `cache_reset` / `removed`
- `build_engine.token_issued`
- `build_job.dispatched` / `succeeded` / `failed` / `cancelled` / `engine_lost`
- `site_build_secret.created` / `updated` / `deleted`
- `site_build_config.updated`

---

## 14. Portability and Packaging (PyInstaller)

### 14.1 Distribution target (resolution OQ-18)

- **Ubuntu LTS 24.04 and 26.04**, **amd64 only**.
- Build the binary against the **oldest supported glibc** to maximize
  portability across 24.04/26.04.
- Output: `build-engine` single-file binary (`--onefile`, resolution OQ-17).
- No arm64, no musl, no Windows, no macOS.

### 14.2 PyInstaller spec notes

- Explicit `hiddenimports` for `aiosqlite`, `httpx`, `websockets`,
  `pydantic`, `pydantic_settings`, `structlog`, `cryptography`.
- Bundle `certifi` + `tzdata`.
- Strip + UPX **disabled** (UPX can produce false-positive AV hits and
  slows debugging).

### 14.3 Configuration layering (unchanged from v0.1)

| Layer                  | Path                                                  | Purpose                  |
|------------------------|-------------------------------------------------------|--------------------------|
| Defaults (compiled in) | n/a                                                   | Sensible defaults        |
| System config          | `/etc/mincemeat/build-engine/config.toml`             | Operator settings        |
| Credentials            | `/etc/mincemeat/build-engine/credentials.toml` (`0600`) | Engine id + client-cert paths |
| Environment            | `BUILD_ENGINE_*`                                      | CI / overrides           |
| CLI flags              | `build-engine serve --backend-url …`                  | Ad-hoc overrides         |

### 14.4 CI build matrix

GitHub Actions: single matrix entry `ubuntu-24.04` (amd64). Signed
artifacts attached to releases; SHA256 + cosign signatures published.

---

## 15. Systemd Deployment

### 15.1 Files installed

- `/usr/local/bin/build-engine`
- `/etc/mincemeat/build-engine/config.toml`
- `/etc/mincemeat/build-engine/credentials.toml` (`0600`) — `engine_id`,
  `engine_secret`, backend leaf-cert fingerprint, agent base URL.
- `/etc/mincemeat/build-engine/engine.crt` (`0444`) — self-signed cert.
- `/etc/mincemeat/build-engine/engine.key` (`0400`) — private key (never
  leaves the host; not posted to backend).
- `/var/lib/build-engine/` (workspaces, caches, `queue.sqlite`)
- `/var/log/build-engine/` (also journald)
- `/etc/systemd/system/build-engine.service`

### 15.2 systemd unit (sketch)

```ini
[Unit]
Description=Mincemeat Build Engine
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=notify
User=build-engine
Group=docker
ExecStart=/usr/local/bin/build-engine serve --config /etc/mincemeat/build-engine/config.toml
Restart=on-failure
RestartSec=5s
LimitNOFILE=65535
StateDirectory=build-engine
LogsDirectory=build-engine

# Hardening
ProtectSystem=strict
ReadWritePaths=/var/lib/build-engine /var/log/build-engine
NoNewPrivileges=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
```

### 15.3 First-boot UX

1. Admin generates a token in the UI.
2. Operator runs:
   ```bash
   build-engine register \
     --backend-url https://api.mincemeat.id \
     --token <one-time-token> \
     --name "build-engine-sfo-1" \
     --max-concurrency 2
   ```
   The CLI:
   - generates a fresh `engine.key` + self-signed `engine.crt`,
   - POSTs the cert + token to the backend,
   - persists `engine_id`, `engine_secret`, and the backend's leaf-cert
     fingerprint into `credentials.toml`.
3. `systemctl enable --now build-engine`.
4. Engine shows `ONLINE` on the admin UI within a heartbeat.

### 15.4 `build-engine doctor` (resolution OQ-K)

A bundled diagnostic subcommand shipped in v1 because this is a complex
multi-component subsystem. Runs and reports on:

| Check                                         | Pass criterion                                            |
|-----------------------------------------------|-----------------------------------------------------------|
| Binary version + protocol version             | Matches a backend-supported range                         |
| `docker version` reachable on UNIX socket     | Daemon responds                                            |
| Docker daemon cgroup driver / cgroup v2       | cgroup v2 present                                          |
| Disk space on `/var/lib/build-engine`         | ≥ 20 GiB free                                              |
| Workspace + cache directory writability       | Pass                                                       |
| `engine.crt` + `engine.key` readable, parseable | Pass                                                     |
| Backend reachability (HTTPS handshake)        | Pass + leaf-cert fingerprint matches pinned value          |
| Agent endpoint reachability                   | `GET /api/v1/build-engines/agent/health` returns 200      |
| WSS handshake                                 | Connects, receives `welcome` frame                        |
| Image registry pull (`ghcr.io/mincemeat-id/build-engine-images/node:20`) | Pull within 60 s |
| SQLite queue file integrity                   | `PRAGMA integrity_check` returns `ok`                     |
| Time skew vs backend                          | Within ±60 s                                              |

Output is human-readable by default and `--json` for machine consumption.
Exit code is non-zero if any check fails.

### 15.5 Upgrades (resolution OQ-19)

No self-update in v1. Upgrade flow:

```bash
systemctl stop build-engine
cp build-engine.new /usr/local/bin/build-engine
systemctl start build-engine
```

Backend tolerates one minor protocol version skew. Operators can drain an
engine first via `POST /api/v1/admin/build-engines/{id}/drain` to avoid
interrupting in-flight jobs.

---

## 16. Observability and Metrics

### 16.1 Engine-side metrics (push model)

The engine pushes metrics rollups to the backend every **15 s**, plus
real-time events inline with the status stream (per requirement).

Counters/gauges/timers:

| Metric                         | Type      | Notes                                       |
|--------------------------------|-----------|---------------------------------------------|
| `engine.workers.total`         | gauge     | `max_concurrency`                           |
| `engine.workers.busy`          | gauge     | currently running                           |
| `engine.queue.depth`           | gauge     | SQLite queue `QUEUED` count                 |
| `engine.jobs.completed_total`  | counter   | success/failed split via label              |
| `engine.jobs.duration_seconds` | timer     | per-job                                     |
| `engine.docker.image_pull_seconds` | timer | per pull                                    |
| `engine.cache.hit_ratio`       | gauge     | per site, rolled up engine-wide             |
| `engine.cache.size_bytes`      | gauge     | per site                                    |
| `engine.artifact.bytes`        | histogram |                                             |
| `engine.docker.errors_total`   | counter   |                                             |
| `engine.uplink.reconnects_total`| counter  |                                             |

### 16.2 Backend-side aggregation

- `BuildEngine.metrics_json` stores the last-known snapshot, updated by
  heartbeat/metrics pushes for cheap list views.
- Time-series storage uses the dedicated `build_engine_metric` table
  resolved in OQ-B. Do not extend `instance_metric`; build-engine retention,
  labels, and UI filters are different enough to keep the series separate.
- Surface on admin UI: per-engine sparkline of `queue.depth`,
  `workers.busy`, `cache.hit_ratio`, `jobs.duration_seconds` p50/p95.

### 16.3 Failure classification

Every `BuildJob.error_code` falls into one of:

| Class                  | Examples                                                           |
|------------------------|--------------------------------------------------------------------|
| `USER_BUILD_FAILED`    | exit code != 0 from build command                                  |
| `USER_CONFIG_INVALID`  | `BUILD_INCOMPATIBLE` from comprehensive detection                  |
| `USER_OUTPUT_INVALID`  | post-build validation rejected the output                          |
| `EXEC_OOM`             | Docker OOMKilled                                                   |
| `EXEC_TIMEOUT`         | 10 min wallclock exceeded                                          |
| `EXEC_INFRA`           | image pull failure, docker daemon error                            |
| `ENGINE_LOST`          | heartbeat timeout while job was assigned                            |
| `PLATFORM_ERROR`       | backend dispatcher / storage error                                  |
| `CANCELLED`            | cancelled by user                                                   |

Retry policy:

- `USER_*` → no retry.
- `EXEC_INFRA`, `ENGINE_LOST` → create a new `BuildJobAttempt` and
  re-dispatch up to **2 extra times**.
- `EXEC_OOM`, `EXEC_TIMEOUT` → no retry (must change project, raise limit,
  or contact us).
- `CANCELLED` → no retry.

---

## 17. Operator and Developer Workflows

### 17.1 Local development

Because the build engine lives in a separate repo:

- `build-engine` developer:
  ```bash
  cd build-engine
  uv sync
  uv run build-engine serve --dev   # uses ./dev.config.toml, embedded queue under ./.local/queue.sqlite
  ```
  Dev mode supports pointing at a local backend (`http://localhost:8000`)
  and skips production client-cert enforcement in favor of a dev cert pair
  (gated behind `--dev`).

- `coreapp` developer:
  ```bash
  cd coreapp && ./dev.sh
  ```
  Backend exposes the agent endpoints. The dev config accepts a dev client-cert
  cert pair and `BUILD_ENGINE_DEV_TRUST_ANY=1` to ease local testing.

### 17.2 CI

- `build-engine` repo CI: lint, type-check, tests, PyInstaller build,
  integration test that runs the binary against the backend's test
  harness using Docker-in-Docker.
- `coreapp` repo CI: contract tests for the new agent endpoints (no
  engine binary needed, mock uplink).
- `build-engine-images` repo CI: build each image, run Trivy scan,
  publish to GHCR with SBOM + provenance.

### 17.3 Release coordination

Three repos, three release cycles:

- `build-engine-images` releases bump tags in their `manifest.json`.
- `build-engine` releases pin a known-good image manifest version range.
- `coreapp` releases pin a `proto_min`/`proto_max` range.

Compatibility matrix is published in the build-engine repo's README.

---

## 18. Implementation Task Breakdown (target repos)

Per product direction, separate task lists per repository.

### 18.1 `build-engine` (new repo)

1. Project scaffolding (pyproject, uv, ruff, ty, pytest, pre-commit).
2. CLI skeleton (`serve`, `register`, `status`, `doctor`).
3. Config layering + `pydantic-settings`.
4. Client-cert + JWT uplink client.
5. Heartbeat loop.
6. SQLite WAL durable queue + lease/visibility-timeout semantics.
7. WSS receive loop + control-message handling.
8. Docker executor (image pull, container run, log stream, artifact tar).
9. Per-site cache mount + lifecycle.
10. Detection module (package_json, lockfiles, framework registry).
11. Post-build validator.
12. Metrics collector + 15s reporter.
13. PyInstaller spec + CI build.
14. Systemd unit + install script.
15. Tests (unit + integration with DinD).

### 18.2 `build-engine-images` (new repo)

1. Repo scaffolding + GitHub Actions.
2. `node:20`, `node:22`, `bun:latest`, `hugo:latest` Dockerfiles.
3. Build entrypoint script + manifest contract.
4. SBOM + cosign provenance pipeline.
5. Trivy gating workflow.
6. Image-version manifest + release process.

### 18.3 `coreapp` (this repo)

**Shared (`shared/`):**
1. SQLAlchemy models: `BuildEngine`, `BuildEngineToken`, `BuildJob`,
   `BuildJobAttempt`, `SiteBuildSecret`, `SiteBuildConfig`. Add
   `PipelineStageName.BUILD` and BUILD log pointer columns. Decide OQ-T
   before adding any new `Deployment.source` value.

**Backend (`backend/`):**
2. Alembic migration.
3. Services: `build_engines`, `build_dispatcher`, `build_job_events`,
   `build_secrets`, `heartbeat_watcher`.
4. Admin REST endpoints (engines, tokens, jobs, cache reset).
5. Site-owner REST endpoints (build-config, build-secrets, cache reset).
6. Agent endpoints (`register`, `sessions`, `heartbeats`, `ws`,
   `artifact-upload-url`, `jobs/{id}/ack`, `metrics`).
7. Client-cert fingerprint validation / TLS termination config (no CA pin;
   see OQ-P).
8. Pipeline integration (`BUILD` stage actor in pipeline executor).
9. Object-storage build-log persistence.
10. Pipeline WS fanout for `BUILD` events.
11. Audit-log entries.
12. OpenAPI contracts.
13. Tests.

**Worker (`worker/`):**
14. `BUILD` stage handler that bridges to dispatcher + waits on
    `build_job_events`.
15. `VALIDATE` extension: `classify_project_mode`.
16. Post-build re-validation in `UPLOAD`.
17. APScheduler heartbeat watcher.
18. Cancellation propagation.

**Frontend (`frontend/`):**
19. TypeScript types via `contracts:check`.
20. Admin pages (list, register, detail, jobs, global jobs).
21. Site settings: Build tab + Env vars tab + cache reset.
22. Pipeline detail: `BUILD` stage card + framework/cache info +
    live logs.
23. Pinia stores + Reka UI components.
24. Tests.

**Docs (`docs/static-sites/`):**
25. New `docs/static-sites/build-engine.md` (operator runbook).
26. Update `docs/static-sites/overview.md` to remove "Optional build or
    transform stages" from Future Scope.
27. Update `github-deployments.md` pipeline-stage table to include `BUILD`.

---

## 19. Open Questions — v0.5 Review

All v0.1 (OQ-1…OQ-20) and v0.2/v0.3 (OQ-A…OQ-M) questions remain resolved
except where explicitly superseded below. These v0.5 questions are blockers
for finalizing the design because they affect data model, operator behavior,
or production networking.

| ID    | Question | Recommendation / default if accepted |
|-------|----------|---------------------------------------|
| OQ-N  | When compatible engines are online but all are busy, how long may a BUILD stage wait before failing? | Queue instead of fast-failing. Add `BUILD_ENGINE_QUEUE_MAX_WAIT_SECONDS` default 1800. Status detail: `WAITING_FOR_ENGINE`. Fast-fail only when no compatible online engine exists. |
| OQ-O  | Where do build artifacts and full build logs live before promotion: a platform-owned staging bucket, or each site's configured `StorageTarget`? | Prefer a platform-owned staging bucket/prefix for simpler presigned URL issuance, retention, and operator access. The worker then promotes from staging to the site's final storage target. |
| OQ-P  | What is the exact TLS termination topology for engine client certificates in production? | Use a dedicated agent hostname that terminates TLS at Traefik/Nginx under our control, requires/request-validates client certs, forwards the verified peer cert/fingerprint to FastAPI, and bypasses any CDN layer that cannot pass arbitrary self-signed client certs. |
| OQ-Q  | If the backend API runs more than one replica, how does the dispatcher push to the replica that owns a given engine WSS? | Either make agent WSS sticky to one backend replica for v1, or store engine connection ownership in Redis and publish `job.assign`/control commands to `build-engine:commands:{engine_id}`. Prefer Redis command fanout if multi-replica is already required. |
| OQ-R  | Which framework profiles are truly in v1 GA versus v1.x fast-follow? | Lock v1 to frameworks with an image, positive fixture, negative fixture where relevant, and docs guide. Current recommendation: Astro, Vite, Eleventy, Docusaurus, VitePress, VuePress, Gatsby, Hugo, Next.js static export, Nuxt generate, SvelteKit static, Generic. Treat Zola, Angular static, and Remix SPA as v1 candidates that must earn inclusion via fixtures before kickoff. |
| OQ-S  | What abuse controls are required for outbound build traffic under `NETWORK_FULL`? | For v1: block metadata/private platform networks, cap job time/resources, log destination summaries when possible, and rate-limit jobs per site/user. Defer per-registry allowlists/egress proxy to v2 unless product wants stricter launch controls. |
| OQ-T  | Should `Deployment.source` gain a new enum for built GitHub deployments? | Prefer no new `BUILD` source. Keep `Deployment.source = GITHUB` and add `deploy_metadata.build_job_id` / `build_engine = true`. If analytics needs enum distinction, add `GITHUB_BUILD`, not bare `BUILD`. |
| OQ-U  | Do we backfill a `BUILD` stage into historical pipelines? | No. Historical six-stage pipelines remain immutable. Frontend renders either six-stage or seven-stage timelines. New pipelines only receive `BUILD`. |

---

## 20. Spec Deliverables Status

The v0.4 spec deliverables below are now present in §§23–34. They no longer
block implementation skeleton work. The remaining blockers are the v0.5 open
questions in §19 and the concrete model/protocol corrections called out in
§3ter.

1. **Sequence diagrams** for: register, dispatch (success), dispatch (no
   compatible engine), run, cancel, engine-lost recovery, cache reset.
2. **Wire-protocol spec** for the agent WSS:
   - Envelope shape (`type`, `seq`, `ts`, `payload`).
   - Message taxonomy: `welcome`, `job.assign`, `job.ack`, `status`,
     `log`, `metric`, `artifact.ready`, `cancel`, `cache.reset`, `drain`,
     `error`, `heartbeat`.
   - Error code catalog.
   - `proto_version` negotiation rules (resolution OQ-J).
3. **OpenAPI schemas** for every endpoint in §10.3 (admin, site-owner,
   agent).
4. **Alembic migration** sketch: column types, indexes, FKs, stage enum
   additions, attempt table, and log pointer columns.
5. **ADRs** under `docs/adr/` for the major decisions:
   - Embedded SQLite WAL queue vs broker.
   - Self-signed-cert + fingerprint auth (mirrors LXD ADR pattern).
   - Per-site PM-only cache.
   - Round-robin dispatcher v1.
   - Fast-fail on zero engines.
6. **Frontend wireframes** for the new admin (engine list, register,
   detail, jobs) and site-settings (Build, Env vars) pages, plus the
   pipeline-detail BUILD card.
7. **Framework acceptance matrix**: for each v1 framework, a sample repo,
   expected build invocation, expected output dir, expected artifact
   size band, expected log breadcrumbs.
8. **Disaster-recovery runbook**: engine host crashes mid-build, GHCR
   outage during image pull, staging-storage outage during artifact upload, backend
   outage during status stream, SQLite queue corruption.
9. **Performance budget**: cold and warm-cache target build times per
   framework on the chosen hardware spec.
10. **Hardware spec** for the build-engine hosts (vCPU, RAM, disk for
    `max_concurrency = 2` + cache + workspace + queue).
11. **GHCR publication runbook**: image rebuild cadence, CVE-budget
    policy, manifest version bump procedure, rollback.
12. **Compatibility matrix** between `build-engine`, `coreapp`, and
    `build-engine-images` (resolution OQ-J implementation detail).

---

## 21. Iteration Plan (revised)

| Pass | Focus                                                                                                                     |
|------|---------------------------------------------------------------------------------------------------------------------------|
| v0.1 | Establish shape, identify open questions. ✅                                                                              |
| v0.2 | Resolve v0.1 OQs, define coreapp changes per project, durable queue model, cache model, observability. ✅                 |
| v0.3 | Resolve v0.2 OQs (OQ-A…OQ-M), lock auth model to LXD pattern, finalize retention, finalize cache scope. ✅ (this pass)    |
| v0.4 | Sequence diagrams, wire-protocol spec, OpenAPI schemas, ADRs, Alembic migration sketch, frontend wireframes, framework-acceptance matrix, hardware spec, DR runbook. ✅ |
| v0.5-review | Validate the design against current coreapp, correct contradictions, add attempt-level protocol/data model, and reopen implementation-critical questions. ✅ (this pass) |
| v0.6 | Resolve §19, then move the design doc into `mincemeat-id/build-engine`; spin up `mincemeat-id/build-engine-images`; cut implementation tasks per §18 across all three repos. End-to-end target: Astro + Vite + Next.js static export first. |

---

## 22. Status Summary

| Area                                         | Status                                |
|----------------------------------------------|---------------------------------------|
| Product decisions (v0.1–v0.3 OQs)            | **Resolved**                          |
| v0.5 review open questions (§19)             | **Blocking finalization**             |
| High-level architecture                      | **Locked**                            |
| New repo layout (`build-engine`, `images`)   | **Locked**                            |
| Coreapp changes (backend / worker / frontend / shared) | **Specified per project**         |
| Auth model                                   | **LXD-style self-signed + fingerprint** |
| Queue model                                  | **Embedded aiosqlite WAL**            |
| Cache model                                  | **PM-only, per-site, 5 GiB / 30 d**   |
| Dispatcher                                   | **Round-robin; fast-fail only when no compatible online engine exists; saturation wait is OQ-N** |
| Retention                                    | **Mirrors `Pipeline` retention**      |
| Packaging                                    | **PyInstaller `--onefile`, Ubuntu LTS x86_64** |
| Diagnostics                                  | **`build-engine doctor` in v1**       |
| Sequence diagrams                            | **Complete (§23)**                    |
| Agent wire-protocol spec                     | **Complete (§24)**                    |
| OpenAPI schemas                              | **Complete sketch (§25)**             |
| Alembic migration                            | **Complete sketch (§26)**             |
| ADRs (queue, auth, cache, dispatcher, fast-fail) | **Complete (§27)**                |
| Frontend wireframes                          | **Complete (§28)**                    |
| Framework acceptance matrix                  | **Draft (§29); v1 scope still OQ-R**  |
| DR runbook                                   | **Complete (§30)**                    |
| Performance budget & hardware spec           | **Complete (§§31–32)**                |
| GHCR publication runbook                     | **Complete (§33)**                    |
| Compatibility matrix                         | **Complete (§34)**                    |
| Implementation                               | **Pending §19 answers, then v0.6 kickoff** |

The design is **not yet ready for implementation kickoff**. Next step:
answer §19, then relocate this document into `mincemeat-id/build-engine`,
spin up `mincemeat-id/build-engine-images`, and cut the per-repo task
breakdown from §18 into GitHub issues.

---

## 23. Sequence Diagrams

> All diagrams use the box-drawing style. Arrows: `─▶` request, `◀─` response,
> `──▶` async/fire-and-forget, `╌▶` over-WSS. Persistent state writes are
> shown in `[brackets]`.

### 23.1 Engine registration

```diagram
Operator      build-engine CLI       Backend (FastAPI)         DB (MariaDB)
   │                │                      │                        │
   │ generate token │                      │                        │
   ├───────────────▶│  (admin UI)          │                        │
   │                │                      │ POST /admin/build-     │
   │                │                      │   engines/registration-│
   │                │                      │   tokens               │
   │                │                      ├───────────────────────▶│
   │                │                      │       [insert BET]     │
   │                │                      │◀───────────────────────┤
   │  copy token    │                      │ {token, expires_at}    │
   │◀───────────────┤                      │                        │
   │                │                      │                        │
   │ register --token T --name "..."       │                        │
   ├───────────────▶│                      │                        │
   │                │ generate engine.key  │                        │
   │                │ + engine.crt         │                        │
   │                │ (self-signed, 20y)   │                        │
   │                │                      │                        │
   │                │ POST /agent/register │                        │
   │                │  {token, cert_pem,   │                        │
   │                │   name, capabilities,│                        │
   │                │   proto_version,     │                        │
   │                │   image_manifest_v}  │                        │
   │                ├─────────────────────▶│                        │
   │                │                      │ validate token,        │
   │                │                      │ compute fingerprint    │
   │                │                      │ encrypt cert_pem       │
   │                │                      │  with CERT_MASTER_KEY  │
   │                │                      ├───────────────────────▶│
   │                │                      │   [insert BuildEngine] │
   │                │                      │   [mark BET consumed]  │
   │                │                      │◀───────────────────────┤
   │                │ {engine_id,          │                        │
   │                │  engine_secret,      │                        │
   │                │  backend_cert_fp,    │                        │
   │                │  session_jwt}        │                        │
   │                │◀─────────────────────┤                        │
   │                │                      │                        │
   │                │ write credentials.toml                        │
   │                │ (chmod 0600)         │                        │
   │  "done. enable │                      │                        │
   │   the service" │                      │                        │
   │◀───────────────┤                      │                        │
   │                │                      │                        │
   │ systemctl enable --now build-engine   │                        │
   ├───────────────▶│                      │                        │
   │                │ (start serve)        │                        │
   │                │ TLS connect (client  │                        │
   │                │   cert = engine.crt) │                        │
   │                ├─────────────────────▶│                        │
   │                │                      │ fingerprint lookup     │
   │                │                      ├───────────────────────▶│
   │                │                      │◀───────────────────────┤
   │                │                      │ {engine matches}       │
   │                │ POST /agent/sessions │                        │
   │                ├─────────────────────▶│                        │
   │                │ {session_jwt}        │                        │
   │                │◀─────────────────────┤                        │
   │                │ WSS /agent/ws upgrade│                        │
   │                ├──╌─────────────────╌▶│                        │
   │                │ welcome frame        │                        │
   │                │◀╌─────────────────╌──┤                        │
   │                │ heartbeats every 15s │                        │
   │                ├──╌─────────────────╌▶│ [update last_seen_at]  │
   │                │                      │ [status=ONLINE]        │
```

### 23.2 Dispatch — success path

```diagram
Pipeline runner   build_dispatcher    Backend WSS hub     Engine
      │                  │                  │                │
      │ BUILD stage      │                  │                │
      │ reached          │                  │                │
      ├─────────────────▶│                  │                │
      │                  │ select engine    │                │
      │                  │ (round-robin     │                │
      │                  │  over ONLINE     │                │
      │                  │  w/ capacity)    │                │
      │                  │ [insert BuildJob]│                │
      │                  │  status=QUEUED   │                │
      │                  │ [insert Attempt] │                │
      │                  │                  │                │
      │                  │ push job.assign  │                │
      │                  │ {attempt_id}     │                │
      │                  ├─────────────────▶│ job.assign     │
      │                  │                  ├──╌───────────╌▶│
      │                  │                  │                │ enqueue
      │                  │                  │                │ to SQLite
      │                  │                  │ job.ack ASSIGNED {attempt_id}
      │                  │                  │◀╌───────────╌──┤
      │                  │ [status=ASSIGNED]│                │
      │                  │                  │                │
      │                  │                  │ status RUNNING │
      │                  │                  │◀╌───────────╌──┤
      │                  │                  │ log... log...  │
      │                  │                  │◀╌───────────╌──┤
      │                  │ fanout via Redis │                │
      │                  │ pub/sub          │                │
      │                  ├──▶ pipeline WS   │                │
      │                  │                  │ artifact.ready │
      │                  │                  │◀╌───────────╌──┤
      │                  │                  │                │ PUT artifact
      │                  │                  │                │  to presigned URL
      │                  │                  │ job.ack DONE {attempt_id}
      │                  │                  │◀╌───────────╌──┤
      │                  │ [status=SUCCEEDED]                │
      │ BUILD success    │                  │                │
      │◀─────────────────┤                  │                │
      │ UPLOAD stage     │                  │                │
      │ re-validates +   │                  │                │
      │ promotes artifact│                  │                │
```

### 23.3 Dispatch — no compatible online engine (resolution OQ-C)

```diagram
Pipeline runner   build_dispatcher              DB
      │                  │                       │
      │ BUILD reached    │                       │
      ├─────────────────▶│                       │
      │                  │ SELECT engine         │
      │                  │   WHERE status=ONLINE │
      │                  │     AND proto matches │
      │                  │     AND image matches │
      │                  ├──────────────────────▶│
      │                  │◀──────────────────────┤
      │                  │ (empty set)           │
      │                  │ [BuildJob FAILED      │
      │                  │  error_code=          │
      │                  │  NO_ENGINE_AVAILABLE  │
      │                  │  error_class=         │
      │                  │  PLATFORM_ERROR]      │
      │                  ├──────────────────────▶│
      │ BUILD FAILED     │                       │
      │◀─────────────────┤                       │
      │                  │                       │
      │ mark pipeline FAILED, surface message:   │
      │ "No build engine is currently available  │
      │  to run this build. Contact your admin." │
      │                                          │
      │ (no retry; user must redeploy or push)   │
```

> Engines that are online but busy are **not** this path. Saturated capacity
> keeps the `BuildJob` queued until capacity opens or OQ-N's max wait is hit.
>
> No-build pipelines short-circuit before reaching the dispatcher; they
> mark BUILD stage `SKIPPED` and proceed to UPLOAD/ACTIVATE/FINALIZE
> regardless of engine fleet state.

### 23.4 In-container build run

```diagram
Engine worker        Docker daemon        Container (image)        Status bus
      │                   │                       │                       │
      │ pull image        │                       │                       │
      ├──────────────────▶│ (skip if cached)      │                       │
      │ STATUS PREPARING  │                       │                       │
      ├───────────────────┼───────────────────────┼──────────────────────▶│
      │ extract tarball   │                       │                       │
      │ write manifest.json                       │                       │
      │ mount cache/{site_id}                     │                       │
      │                   │                       │                       │
      │ run --rm \        │                       │                       │
      │   --memory 2g \   │                       │                       │
      │   --cpus 1 \      │                       │                       │
      │   --read-only \   │                       │                       │
      │   --user 1000 \   │                       │                       │
      │   image           │                       │                       │
      ├──────────────────▶│ start                 │                       │
      │                   ├──────────────────────▶│ /build-entrypoint.sh  │
      │                   │                       │ → corepack PM install │
      │                   │                       │ → build cmd           │
      │ STATUS INSTALLING │                       │                       │
      ├───────────────────┼───────────────────────┼──────────────────────▶│
      │ stdout/stderr     │                       │                       │
      │◀──────────────────┤◀──────────────────────┤                       │
      │ LOG events ──────────────────────────────────────────────────────▶│
      │ STATUS BUILDING ──────────────────────────────────────────────────▶│
      │ (10 min watchdog)│                        │                       │
      │ exit code         │                       │                       │
      │◀──────────────────┤◀──────────────────────┤                       │
      │                   │                       │                       │
      │ post-build validate (size, count, sha256, │                       │
      │   index.html present, archive safety)     │                       │
      │ STATUS PACKAGING ────────────────────────────────────────────────▶│
      │ tar workspace/out → artifact.tar.gz        │                       │
      │ compute sha256, size                       │                       │
      │ ARTIFACT_READY ──────────────────────────────────────────────────▶│
      │ STATUS UPLOADING_ARTIFACT ───────────────────────────────────────▶│
      │ request presigned URL via uplink           │                       │
      │ PUT to presigned staging URL               │                       │
      │ STATUS SUCCEEDED ────────────────────────────────────────────────▶│
      │ rm -rf workspace/                          │                       │
```

### 23.5 Cancellation

```diagram
User      Backend          WSS hub        Engine worker      Container
 │           │                │                │                  │
 │ POST cancel               │                │                  │
 ├──────────▶│                │                │                  │
 │           │ control msg    │                │                  │
 │           │ {type:cancel}  │                │                  │
 │           ├───────────────▶│ ╌╌╌▶ job worker│                  │
 │           │                │                │ flip state=      │
 │           │                │                │   CANCELLING     │
 │           │                │                │ docker kill      │
 │           │                │                │  --signal TERM   │
 │           │                │                ├─────────────────▶│
 │           │                │                │ wait 10s         │
 │           │                │                │                  │ (drain)
 │           │                │                │ docker kill      │
 │           │                │                │  --signal KILL   │
 │           │                │                ├─────────────────▶│ (dead)
 │           │                │ status         │                  │
 │           │                │ CANCELLED      │                  │
 │           │                │◀╌╌╌╌╌╌╌╌╌╌╌╌╌╌─┤                  │
 │           │ pipeline       │                │                  │
 │           │ CANCELLED      │                │                  │
 │           │                │                │ delete partial   │
 │           │                │                │  artifact, rm    │
 │           │                │                │  workspace       │
```

### 23.6 Engine-lost recovery

```diagram
Engine            heartbeat_watcher        build_dispatcher       DB
   │ (host crash; no more heartbeats)              │              │
   X                  │                            │              │
                      │ (15s scheduler tick)       │              │
                      │ SELECT engines             │              │
                      │  WHERE last_seen_at        │              │
                      │   < now - 45s              │              │
                      ├───────────────────────────────────────────▶│
                      │◀───────────────────────────────────────────┤
                      │  [status=OFFLINE]                          │
                      │ for each current attempt:                  │
                      │  [BuildJobAttempt FAILED                   │
                      │   error_code=ENGINE_LOST                   │
                      │   error_class=ENGINE_LOST]                 │
                      ├───────────────────────────────────────────▶│
                      │                            │               │
                      │ if attempts remain:        │               │
                      │  [BuildJob QUEUED]         │               │
                      │  [new BuildJobAttempt]     │               │
                      │ re-dispatch eligible jobs  │               │
                      │ (attempts < 3)             │               │
                      ├───────────────────────────▶│               │
                      │                            │ same flow as  │
                      │                            │ §23.2 dispatch│
                      │ publish pipeline event     │               │
                      │ to existing WS fanout      │               │
```

### 23.7 Cache reset

```diagram
Admin       Backend          WSS hub          Engine A    Engine B    ...
  │            │                │                │           │
  │ POST .../cache/reset?site=X │                │           │
  ├───────────▶│                │                │           │
  │            │ control msg    │                │           │
  │            │ {type:         │                │           │
  │            │  cache.reset,  │                │           │
  │            │  site_id:X}    │                │           │
  │            ├───────────────▶│╌╌╌▶ engine A  ─┤           │
  │            │                │╌╌╌▶ engine B ──────────────┤
  │            │                │                │ rm -rf    │ rm -rf
  │            │                │                │ /var/lib/ │ /var/lib/
  │            │                │                │  build-   │  build-
  │            │                │                │  engine/  │  engine/
  │            │                │                │  cache/X  │  cache/X
  │            │                │                │ ack       │ ack
  │            │                │◀╌╌╌╌╌╌╌╌╌╌╌╌╌╌─┤◀──────────┤
  │            │ aggregate acks │                │           │
  │ 200 OK ◀───┤                │                │           │
  │ {engines: [X, Y], success: true}             │           │
  │ audit_log: site_build_cache.reset            │           │
```

---

## 24. Wire-Protocol Spec — Agent WSS

### 24.1 Connection lifecycle

1. **TLS handshake** — engine presents its self-signed client cert; backend
   validates by `BuildEngine.fingerprint` match (resolution OQ-H).
2. **HTTP upgrade** — `GET /api/v1/build-engines/agent/ws` with headers:
   - `Authorization: Bearer <session_jwt>`
   - `X-Build-Engine-Proto: 1`
   - `X-Build-Engine-Version: <semver>`
   - `X-Image-Manifest-Version: <semver>`
3. Backend sends a single **`welcome`** frame containing negotiated proto,
   server time (for skew check), and the session-bound `engine_id`.
4. Both sides exchange typed messages until either tears down (clean close
   code `1000` on drain; `4xxx` on protocol error).
5. Engine reconnects with exponential backoff (1, 2, 5, 10, 30s cap) on
   any disconnect and resumes from `last_seq` per in-flight attempt.

### 24.2 Envelope

All WSS messages are JSON, one message per frame. Every message carries:

```json
{
  "v": 1,
  "id": "u1f2b3c4...",     // ULID, message id, unique per direction
  "type": "<type>",
  "ts": "2026-05-19T07:00:01.123Z",
  "payload": { ... }
}
```

Job-scoped messages additionally carry:

```json
{
  "build_job_id": "uuid",
  "attempt_id": "uuid",
  "seq": 42                 // monotonic per (attempt_id, direction)
}
```

### 24.3 Message taxonomy

| Direction | Type                | Payload keys                                                                                  |
|-----------|---------------------|-----------------------------------------------------------------------------------------------|
| **B → E** | `welcome`           | `engine_id`, `server_time`, `proto_negotiated`, `heartbeat_interval_seconds`                  |
| **B → E** | `job.assign`        | `build_job_id`, `attempt_id`, `pipeline_id`, `site_id`, `source_download_url`, `source_sha256`, `source_archive_format`, `root_directory`, `framework_id`, `package_manager`, `image`, `build_command`, `output_dir` (optional), `env` (k/v map of decrypted build-time secrets), `cache_enabled`, `resource_limits`, `timeout_seconds` |
| **B → E** | `cancel`            | `build_job_id`, `reason`                                                                      |
| **B → E** | `cache.reset`       | `site_id` (null = all sites on this engine)                                                  |
| **B → E** | `drain`             | (none) — engine stops accepting new assigns, finishes in-flight                              |
| **B → E** | `ping`              | (none) — application-level liveness probe                                                    |
| **E → B** | `hello`             | `version`, `proto_version`, `image_manifest_version`, `capabilities`, `max_concurrency`      |
| **E → B** | `job.ack`           | `build_job_id`, `attempt_id`, `state` (`ASSIGNED`/`RUNNING`/`SUCCEEDED`/`FAILED`/`CANCELLED`) |
| **E → B** | `status`            | `build_job_id`, `phase` (`PREPARING`/`INSTALLING`/`BUILDING`/`PACKAGING`/`VALIDATING`/`UPLOADING_ARTIFACT`), `detail` (optional) |
| **E → B** | `log`               | `build_job_id`, `stream` (`stdout`/`stderr`), `data` (UTF-8 string, ≤ 64 KiB per frame)      |
| **E → B** | `metric`            | `build_job_id` (nullable), `name`, `value`, `unit`                                            |
| **E → B** | `artifact.ready`    | `build_job_id`, `sha256`, `size_bytes`                                                       |
| **E → B** | `cache.event`       | `build_job_id`, `event` (`HIT`/`MISS`/`POISONED`/`WIPED`)                                     |
| **E → B** | `error`             | `build_job_id` (nullable), `code`, `message`, `recoverable` (bool)                            |
| **E → B** | `heartbeat`         | `workers_busy`, `workers_total`, `queue_depth`, `cache_size_bytes`, `disk_free_bytes`         |
| **E → B** | `pong`              | (none)                                                                                        |

### 24.4 Error code catalog

Engine error codes (mirror `BuildJob.error_code`):

| Code                       | Class                  | Recoverable | Notes                                            |
|----------------------------|------------------------|-------------|--------------------------------------------------|
| `USER_BUILD_FAILED`        | `USER_BUILD_FAILED`    | no          | Build command exit code ≠ 0                      |
| `USER_CONFIG_INVALID`      | `USER_CONFIG_INVALID`  | no          | Detection rejected config (e.g. Next.js no export) |
| `USER_OUTPUT_INVALID`      | `USER_OUTPUT_INVALID`  | no          | Post-build validation rejected output             |
| `EXEC_OOM`                 | `EXEC_OOM`             | no          | Docker reported OOMKilled                         |
| `EXEC_TIMEOUT`             | `EXEC_TIMEOUT`         | no          | 10 min wallclock exceeded                         |
| `EXEC_INFRA_IMAGE_PULL`    | `EXEC_INFRA`           | yes         | Registry transient failure                        |
| `EXEC_INFRA_DOCKER`        | `EXEC_INFRA`           | yes         | Daemon error                                      |
| `EXEC_INFRA_DISK_FULL`     | `EXEC_INFRA`           | no          | No space left                                     |
| `INTEGRITY_FAILED`         | `EXEC_INFRA`           | no          | Cache integrity check failed; cache wiped         |
| `CANCELLED`                | `CANCELLED`            | no          | User-initiated                                    |
| `ENGINE_LOST`              | `ENGINE_LOST`          | yes         | Set by backend, never by engine                   |
| `NO_ENGINE_AVAILABLE`      | `PLATFORM_ERROR`       | no          | No compatible online engine                       |

Protocol error close codes (RFC 6455 application range):

| Code   | Meaning                                                  |
|--------|----------------------------------------------------------|
| `4001` | Bad envelope / unparseable JSON                          |
| `4002` | Unknown message type                                     |
| `4003` | Out-of-order `seq` for a known `attempt_id`              |
| `4004` | Unknown `build_job_id` in a job-scoped message           |
| `4010` | Unsupported `proto_version`                              |
| `4011` | Engine `status` ≠ `ONLINE`/`DRAINING` (revoked / disabled) |
| `4012` | Session JWT expired or invalid                           |
| `4013` | Cert fingerprint mismatch                                 |
| `4029` | Rate-limit / message-size cap exceeded                   |
| `4090` | Engine forced to drain by admin                          |

### 24.5 `proto_version` negotiation (resolution OQ-J)

- Engine declares `proto_version` in the upgrade handshake and `hello`.
- Backend has a compiled-in `[proto_min, proto_max]` range. v1 → `[1, 1]`.
- If engine's proto is outside the range, backend closes with `4010` and a
  human-readable reason in the close payload.
- Engine refuses any `job.assign` whose required `image` tag is outside its
  `image_manifest_version` pinned manifest, replying with
  `error{code: EXEC_INFRA_IMAGE_PULL, recoverable: false}`.

### 24.6 Idempotency, ordering, replay

- `seq` is monotonic strictly increasing per `(attempt_id, direction)`.
- Backend stores `BuildJobAttempt.last_seq` and drops any inbound event with
  `seq <= last_seq`.
- On reconnect, the engine re-sends any unacknowledged events starting at
  `last_seq + 1`; backend acks via per-event `job.ack` for status changes
  or implicit acceptance for logs.
- Outbound `job.assign` from backend is idempotent on
  `(build_job_id, attempt_id)`; if the engine already has the attempt in its
  queue, it returns `job.ack ASSIGNED` with the existing state instead of
  re-enqueueing.
- Any event or artifact upload for a non-current `attempt_id` is recorded as
  stale/audit-only and cannot transition the parent `BuildJob` or `Pipeline`
  stage.

### 24.7 Frame size limits

| Limit                       | Value          |
|-----------------------------|----------------|
| Max frame size              | 1 MiB           |
| Max `log.data` per frame    | 64 KiB          |
| Max in-flight events / sec  | 200 (rate-limited per engine) |
| Heartbeat interval          | 15 s            |
| Heartbeat timeout (3 missed)| 45 s            |

---

## 25. OpenAPI Schemas (Sketch)

> The full OpenAPI doc lives in `backend/openapi/build_engine.yaml` once the
> implementation lands. The shape below is sufficient for v0.5 implementation.

### 25.1 Common schemas

```yaml
components:
  schemas:
    BuildEngineStatus:
      type: string
      enum: [PENDING, ONLINE, OFFLINE, DISABLED, DRAINING, QUARANTINED]
    BuildJobStatus:
      type: string
      enum: [QUEUED, ASSIGNED, RUNNING, SUCCEEDED, FAILED, CANCELLED, TIMED_OUT]
    BuildErrorClass:
      type: string
      enum:
        - USER_BUILD_FAILED
        - USER_CONFIG_INVALID
        - USER_OUTPUT_INVALID
        - EXEC_OOM
        - EXEC_TIMEOUT
        - EXEC_INFRA
        - ENGINE_LOST
        - PLATFORM_ERROR
        - CANCELLED
    BuildEngineCapabilities:
      type: object
      required: [os, arch, max_concurrency, images, proto_version, image_manifest_version]
      properties:
        os: { type: string, example: "linux" }
        arch: { type: string, example: "amd64" }
        max_concurrency: { type: integer, minimum: 1, maximum: 16 }
        images:
          type: array
          items: { type: string }
          example: ["ghcr.io/mincemeat-id/build-engine-images/node:20"]
        proto_version: { type: integer, example: 1 }
        image_manifest_version: { type: string, example: "1.0.0" }
    BuildEngine:
      type: object
      properties:
        id: { type: string, format: uuid }
        name: { type: string }
        status: { $ref: "#/components/schemas/BuildEngineStatus" }
        version: { type: string }
        proto_version: { type: integer }
        image_manifest_version: { type: string }
        max_concurrency: { type: integer }
        labels: { type: object, additionalProperties: { type: string } }
        capabilities: { $ref: "#/components/schemas/BuildEngineCapabilities" }
        fingerprint: { type: string, description: "sha256 hex" }
        last_seen_at: { type: string, format: date-time, nullable: true }
        created_at: { type: string, format: date-time }
        updated_at: { type: string, format: date-time }
    BuildJob:
      type: object
      properties:
        id: { type: string, format: uuid }
        pipeline_id: { type: string, format: uuid }
        pipeline_stage_id: { type: string, format: uuid }
        site_id: { type: string, format: uuid }
        current_engine_id: { type: string, format: uuid, nullable: true }
        current_attempt_id: { type: string, format: uuid, nullable: true }
        status: { $ref: "#/components/schemas/BuildJobStatus" }
        source_storage_key: { type: string }
        source_sha256: { type: string }
        root_directory: { type: string, default: "." }
        framework_id: { type: string }
        package_manager: { type: string, enum: [npm, pnpm, yarn, bun] }
        image: { type: string }
        build_command: { type: string }
        output_dir: { type: string, nullable: true }
        detected_output_dir: { type: string, nullable: true }
        cache_hit: { type: boolean }
        artifact_storage_key: { type: string, nullable: true }
        artifact_size: { type: integer, nullable: true }
        artifact_sha256: { type: string, nullable: true }
        attempts: { type: integer }
        error_code: { type: string, nullable: true }
        error_class: { $ref: "#/components/schemas/BuildErrorClass", nullable: true }
        error_message: { type: string, nullable: true }
        started_at: { type: string, format: date-time, nullable: true }
        finished_at: { type: string, format: date-time, nullable: true }
    BuildJobAttempt:
      type: object
      properties:
        id: { type: string, format: uuid }
        build_job_id: { type: string, format: uuid }
        attempt_number: { type: integer }
        engine_id: { type: string, format: uuid }
        status: { $ref: "#/components/schemas/BuildJobStatus" }
        last_seq: { type: integer }
        artifact_storage_key: { type: string, nullable: true }
        artifact_size: { type: integer, nullable: true }
        artifact_sha256: { type: string, nullable: true }
        error_code: { type: string, nullable: true }
        error_class: { $ref: "#/components/schemas/BuildErrorClass", nullable: true }
        error_message: { type: string, nullable: true }
        assigned_at: { type: string, format: date-time, nullable: true }
        started_at: { type: string, format: date-time, nullable: true }
        finished_at: { type: string, format: date-time, nullable: true }
```

### 25.2 Admin endpoints

```yaml
/api/v1/admin/build-engines:
  get:
    summary: List build engines
    parameters:
      - { name: status, in: query, schema: { $ref: "#/components/schemas/BuildEngineStatus" } }
    responses:
      "200":
        content:
          application/json:
            schema:
              type: object
              properties:
                items: { type: array, items: { $ref: "#/components/schemas/BuildEngine" } }

/api/v1/admin/build-engines/registration-tokens:
  post:
    summary: Issue a one-time registration token
    requestBody:
      required: true
      content:
        application/json:
          schema:
            type: object
            required: [name]
            properties:
              name: { type: string }
              labels: { type: object, additionalProperties: { type: string } }
              expires_in_seconds: { type: integer, default: 900, minimum: 60, maximum: 86400 }
    responses:
      "201":
        content:
          application/json:
            schema:
              type: object
              required: [token, expires_at]
              properties:
                token: { type: string, description: "One-time-use plaintext" }
                expires_at: { type: string, format: date-time }

/api/v1/admin/build-engines/{engine_id}:
  get: { ... }
  patch:
    summary: Update name/labels/max_concurrency
  delete:
    summary: Remove (hard delete; cascades to BuildJob soft-mark)

/api/v1/admin/build-engines/{engine_id}/disable:
  post: { responses: { "200": { ... } } }

/api/v1/admin/build-engines/{engine_id}/drain:
  post: { responses: { "200": { ... } } }

/api/v1/admin/build-engines/{engine_id}/cache/reset:
  post:
    requestBody:
      content:
        application/json:
          schema:
            type: object
            properties:
              site_id: { type: string, format: uuid, nullable: true }
              # null = reset all per-site caches on this engine

/api/v1/admin/build-jobs:
  get:
    parameters:
      - { name: engine_id, in: query, schema: { type: string, format: uuid } }
      - { name: site_id, in: query, schema: { type: string, format: uuid } }
      - { name: status, in: query, schema: { $ref: "#/components/schemas/BuildJobStatus" } }
      - { name: since, in: query, schema: { type: string, format: date-time } }
      - { name: limit, in: query, schema: { type: integer, default: 50, maximum: 200 } }

/api/v1/admin/build-jobs/{job_id}:
  get: { ... }

/api/v1/admin/build-engines/{engine_id}/jobs:
  get: { ... }
```

### 25.3 Site-owner endpoints

```yaml
/api/v1/sites/{site_id}/build-config:
  get: { ... }
  put:
    requestBody:
      content:
        application/json:
          schema:
            type: object
            properties:
              root_directory: { type: string, nullable: true }
              framework_override: { type: string, nullable: true }
              build_command: { type: string, nullable: true }
              output_dir: { type: string, nullable: true }
              node_version: { type: string, nullable: true }
              build_cache_enabled: { type: boolean }

/api/v1/sites/{site_id}/build-secrets:
  get:
    description: Returns only keys; values are write-only.
  put:
    summary: Upsert a key (treated as PUT /key for idempotency in the impl)

/api/v1/sites/{site_id}/build-secrets/{key}:
  put:
    requestBody:
      content:
        application/json:
          schema:
            type: object
            required: [value]
            properties:
              value: { type: string }
  delete: { ... }

/api/v1/sites/{site_id}/build-cache/reset:
  post: { responses: { "202": { ... } } }
```

### 25.4 Agent endpoints (client cert + Bearer JWT)

```yaml
/api/v1/build-engines/agent/register:
  post:
    security: []  # uses one-time token in body, no JWT yet
    requestBody:
      content:
        application/json:
          schema:
            type: object
            required: [registration_token, cert_pem, name, capabilities]
            properties:
              registration_token: { type: string }
              cert_pem: { type: string, description: "PEM-encoded self-signed cert" }
              name: { type: string }
              capabilities: { $ref: "#/components/schemas/BuildEngineCapabilities" }
    responses:
      "201":
        content:
          application/json:
            schema:
              type: object
              required: [engine_id, engine_secret, backend_cert_fingerprint, session_jwt]
              properties:
                engine_id: { type: string, format: uuid }
                engine_secret: { type: string }
                backend_cert_fingerprint: { type: string }
                session_jwt: { type: string }
                session_jwt_expires_at: { type: string, format: date-time }

/api/v1/build-engines/agent/sessions:
  post:
    description: Mint a fresh session JWT given the engine secret.
    requestBody:
      content:
        application/json:
          schema:
            type: object
            required: [engine_id, engine_secret]
            properties:
              engine_id: { type: string, format: uuid }
              engine_secret: { type: string }

/api/v1/build-engines/agent/heartbeats:
  post:
    requestBody:
      content:
        application/json:
          schema:
            type: object
            required: [workers_busy, workers_total, queue_depth]
            properties:
              workers_busy: { type: integer }
              workers_total: { type: integer }
              queue_depth: { type: integer }
              cache_size_bytes: { type: integer }
              disk_free_bytes: { type: integer }

/api/v1/build-engines/agent/ws:
  get:
    description: Long-lived WSS as specified in §24.

/api/v1/build-engines/agent/jobs/{job_id}/attempts/{attempt_id}/artifact-upload-url:
  post:
    requestBody:
      content:
        application/json:
          schema:
            type: object
            required: [sha256, size_bytes]
            properties:
              sha256: { type: string }
              size_bytes: { type: integer }
    responses:
      "200":
        content:
          application/json:
            schema:
              type: object
              required: [upload_url, expires_at, storage_key]
              properties:
                upload_url: { type: string, format: uri }
                expires_at: { type: string, format: date-time }
                storage_key: { type: string }

/api/v1/build-engines/agent/jobs/{job_id}/attempts/{attempt_id}/ack:
  post:
    requestBody:
      content:
        application/json:
          schema:
            type: object
            required: [state]
            properties:
              state: { $ref: "#/components/schemas/BuildJobStatus" }
              error_code: { type: string, nullable: true }
              error_class: { $ref: "#/components/schemas/BuildErrorClass", nullable: true }

/api/v1/build-engines/agent/metrics:
  post:
    requestBody:
      content:
        application/json:
          schema:
            type: object
            properties:
              recorded_at: { type: string, format: date-time }
              workers_busy: { type: integer }
              workers_total: { type: integer }
              queue_depth: { type: integer }
              cache_size_bytes: { type: integer }
              cache_hit_ratio: { type: number }
              jobs_running: { type: integer }
              jobs_completed_total: { type: integer }
              docker_errors_total: { type: integer }
              uplink_reconnects_total: { type: integer }

/api/v1/build-engines/agent/health:
  get:
    security: []
    responses: { "200": { ... } }
```

---

## 26. Alembic Migration Sketch

One revision `20260520_0001_build_engine.py`.

Important v0.5 review corrections:

- Current coreapp enums use `native_enum=False`; adding
  `PipelineStageName.BUILD` is a model-code enum change plus dialect-aware
  column/constraint handling, not a raw MySQL native `ENUM` alteration.
- Current table names are plural (`users`, `pipelines`, `pipeline_stages`,
  `static_sites`, `deployments`).
- `Deployment.source = BUILD` is intentionally **not** included unless OQ-T
  resolves that way.

```python
"""build engine: tables, attempts, BUILD stage log pointers

Revision ID: 20260520_0001
Revises: <previous head>
Create Date: 2026-05-20 00:00:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "20260520_0001"
down_revision = "<previous head>"


def upgrade() -> None:
    # Model-code change: add PipelineStageName.BUILD.
    # Existing native_enum=False columns are VARCHAR-like in this project.
    op.add_column("pipeline_stages", sa.Column("log_storage_key", sa.String(512), nullable=True))
    op.add_column("pipeline_stages", sa.Column("log_storage_bytes", sa.BigInteger(), nullable=True))
    op.add_column(
        "pipeline_stages",
        sa.Column("log_storage_compressed", sa.Boolean(), nullable=True),
    )

    op.create_table(
        "build_engine",
        sa.Column("id", mysql.CHAR(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PENDING", "ONLINE", "OFFLINE", "DISABLED", "DRAINING", "QUARANTINED",
                    name="build_engine_status"),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("proto_version", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("image_manifest_version", sa.String(32), nullable=False),
        sa.Column("max_concurrency", sa.SmallInteger(), nullable=False, server_default="2"),
        sa.Column("labels", sa.JSON(), nullable=False, server_default=sa.text("(JSON_OBJECT())")),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=True),
        # Mirrors LxdServer cert fields exactly
        sa.Column("cert_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("cert_iv", sa.LargeBinary(12), nullable=False),
        sa.Column("cert_tag", sa.LargeBinary(16), nullable=False),
        sa.Column("fingerprint", sa.String(100), nullable=False, unique=True),
        sa.Column("engine_secret_hash", sa.String(200), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("created_by_user_id", mysql.CHAR(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_index("ix_build_engine_status_last_seen",
                    "build_engine", ["status", "last_seen_at"])

    op.create_table(
        "build_engine_token",
        sa.Column("id", mysql.CHAR(36), primary_key=True),
        sa.Column("token_hash", sa.String(200), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False, server_default=sa.text("(JSON_OBJECT())")),
        sa.Column("created_by_user_id", mysql.CHAR(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("consumed_by_engine_id", mysql.CHAR(36),
                  sa.ForeignKey("build_engine.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "build_job",
        sa.Column("id", mysql.CHAR(36), primary_key=True),
        sa.Column("pipeline_id", mysql.CHAR(36), sa.ForeignKey("pipelines.id"), nullable=False),
        sa.Column("pipeline_stage_id", mysql.CHAR(36),
                  sa.ForeignKey("pipeline_stages.id"), nullable=False),
        sa.Column("site_id", mysql.CHAR(36), sa.ForeignKey("static_sites.id"), nullable=False),
        sa.Column("current_engine_id", mysql.CHAR(36),
                  sa.ForeignKey("build_engine.id", ondelete="SET NULL"), nullable=True),
        sa.Column("current_attempt_id", mysql.CHAR(36), nullable=True),
        sa.Column(
            "status",
            sa.Enum("QUEUED", "ASSIGNED", "RUNNING", "SUCCEEDED",
                    "FAILED", "CANCELLED", "TIMED_OUT",
                    name="build_job_status"),
            nullable=False,
            server_default="QUEUED",
        ),
        sa.Column("source_storage_key", sa.String(512), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("root_directory", sa.String(255), nullable=False, server_default="."),
        sa.Column("framework_id", sa.String(64), nullable=False),
        sa.Column("package_manager",
                  sa.Enum("npm", "pnpm", "yarn", "bun", name="build_job_pm"),
                  nullable=False),
        sa.Column("image", sa.String(255), nullable=False),
        sa.Column("build_command", sa.String(1024), nullable=False),
        sa.Column("output_dir", sa.String(255), nullable=True),
        sa.Column("detected_output_dir", sa.String(255), nullable=True),
        sa.Column("env_summary", sa.JSON(), nullable=False, server_default=sa.text("(JSON_ARRAY())")),
        sa.Column("cache_hit", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("artifact_storage_key", sa.String(512), nullable=True),
        sa.Column("artifact_size", sa.BigInteger(), nullable=True),
        sa.Column("artifact_sha256", sa.String(64), nullable=True),
        sa.Column("attempts", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_class", sa.String(32), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_index("ix_build_job_engine_status", "build_job", ["current_engine_id", "status"])
    op.create_index("ix_build_job_site_created", "build_job", ["site_id", "created_at"])
    op.create_index("ix_build_job_pipeline", "build_job", ["pipeline_id"])

    op.create_table(
        "build_job_attempt",
        sa.Column("id", mysql.CHAR(36), primary_key=True),
        sa.Column("build_job_id", mysql.CHAR(36),
                  sa.ForeignKey("build_job.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_number", sa.SmallInteger(), nullable=False),
        sa.Column("engine_id", mysql.CHAR(36),
                  sa.ForeignKey("build_engine.id", ondelete="SET NULL"), nullable=True),
        sa.Column(
            "status",
            sa.Enum("QUEUED", "ASSIGNED", "RUNNING", "SUCCEEDED",
                    "FAILED", "CANCELLED", "TIMED_OUT",
                    name="build_job_status"),
            nullable=False,
            server_default="QUEUED",
        ),
        sa.Column("last_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("artifact_storage_key", sa.String(512), nullable=True),
        sa.Column("artifact_size", sa.BigInteger(), nullable=True),
        sa.Column("artifact_sha256", sa.String(64), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_class", sa.String(32), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("assigned_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_build_job_attempt_job_number",
                    "build_job_attempt", ["build_job_id", "attempt_number"], unique=True)
    op.create_index("ix_build_job_attempt_engine_status",
                    "build_job_attempt", ["engine_id", "status"])

    op.create_table(
        "build_job_event",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("build_job_id", mysql.CHAR(36), sa.ForeignKey("build_job.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("attempt_id", mysql.CHAR(36),
                  sa.ForeignKey("build_job_attempt.id", ondelete="CASCADE"), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_unique_constraint(
        "uq_build_job_event_attempt_seq",
        "build_job_event",
        ["attempt_id", "seq"],
    )

    op.create_table(
        "build_engine_metric",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("engine_id", mysql.CHAR(36), sa.ForeignKey("build_engine.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column("workers_busy", sa.SmallInteger(), nullable=False),
        sa.Column("workers_total", sa.SmallInteger(), nullable=False),
        sa.Column("queue_depth", sa.Integer(), nullable=False),
        sa.Column("cache_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("cache_hit_ratio", sa.Float(), nullable=False),
        sa.Column("jobs_running", sa.SmallInteger(), nullable=False),
        sa.Column("jobs_completed_total", sa.BigInteger(), nullable=False),
        sa.Column("docker_errors_total", sa.Integer(), nullable=False),
        sa.Column("uplink_reconnects_total", sa.Integer(), nullable=False),
    )
    op.create_index("ix_build_engine_metric_engine_time",
                    "build_engine_metric", ["engine_id", "recorded_at"])

    op.create_table(
        "site_build_config",
        sa.Column("site_id", mysql.CHAR(36), sa.ForeignKey("static_sites.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("root_directory", sa.String(255), nullable=True),
        sa.Column("framework_override", sa.String(64), nullable=True),
        sa.Column("build_command", sa.String(1024), nullable=True),
        sa.Column("output_dir", sa.String(255), nullable=True),
        sa.Column("detected_output_dir", sa.String(255), nullable=True),
        sa.Column("node_version", sa.String(32), nullable=True),
        sa.Column("build_cache_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now(), onupdate=sa.func.now()),
    )

    op.create_table(
        "site_build_secret",
        sa.Column("id", mysql.CHAR(36), primary_key=True),
        sa.Column("site_id", mysql.CHAR(36), sa.ForeignKey("static_sites.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("secret_key", sa.String(128), nullable=False),
        sa.Column("value_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("value_iv", sa.LargeBinary(12), nullable=False),
        sa.Column("value_tag", sa.LargeBinary(16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_unique_constraint(
        "uq_site_build_secret_key",
        "site_build_secret",
        ["site_id", "secret_key"],
    )


def downgrade() -> None:
    op.drop_table("site_build_secret")
    op.drop_table("site_build_config")
    op.drop_index("ix_build_engine_metric_engine_time", table_name="build_engine_metric")
    op.drop_table("build_engine_metric")
    op.drop_constraint("uq_build_job_event_attempt_seq", "build_job_event", type_="unique")
    op.drop_table("build_job_event")
    op.drop_index("ix_build_job_attempt_engine_status", table_name="build_job_attempt")
    op.drop_index("ix_build_job_attempt_job_number", table_name="build_job_attempt")
    op.drop_table("build_job_attempt")
    op.drop_index("ix_build_job_pipeline", table_name="build_job")
    op.drop_index("ix_build_job_site_created", table_name="build_job")
    op.drop_index("ix_build_job_engine_status", table_name="build_job")
    op.drop_table("build_job")
    op.drop_table("build_engine_token")
    op.drop_index("ix_build_engine_status_last_seen", table_name="build_engine")
    op.drop_table("build_engine")
    op.drop_column("pipeline_stages", "log_storage_compressed")
    op.drop_column("pipeline_stages", "log_storage_bytes")
    op.drop_column("pipeline_stages", "log_storage_key")
```

> v0.5 review note: use `secret_key`, not `key`, to avoid reserved-word
> ambiguity and to better communicate that values are write-only secrets.

---

## 27. Architecture Decision Records (ADRs)

Five ADRs are introduced. They will land under
[`docs/adr/`](docs/adr/) using the template at
[`docs/adr/0000-template.md`](docs/adr/0000-template.md) once the design
file moves to the build-engine repo. Sketches below.

### ADR-100 — Embedded SQLite WAL queue in the engine

- **Status:** Accepted (v0.2 OQ-3, reconfirmed v0.4).
- **Context:** Engine needs a durable async queue. Backend already uses
  Dramatiq + Redis. Replicating that on every engine host doubles
  infrastructure burden and complicates packaging into a single binary.
- **Decision:** Use aiosqlite WAL with lease + visibility-timeout
  semantics. Single-file persistence; survives restart and crash; ships
  inside PyInstaller bundle with zero external services.
- **Consequences:** Single-host queue only (intended). Poison jobs go to a
  DLQ row. SQLite locks limit concurrency to in-process only — fine for
  `max_concurrency = 2`.
- **Alternatives rejected:** Embedded Redis (extra dep, native), in-memory
  asyncio.Queue (not durable), file-spool dirs (race-prone).

### ADR-101 — Self-signed cert + fingerprint auth (LXD pattern)

- **Status:** Accepted (v0.3 OQ-H).
- **Context:** Engines connect outbound from our infra. Standing up a
  private PKI (CA, CRL, OCSP, rotation) is overkill for the v1 scale.
- **Decision:** Mirror the existing
  [`LxdServer`](shared/src/shared/models/lxd_server.py) pattern. Engine
  self-signs at registration, posts the cert, backend stores encrypted PEM
  + sha256 fingerprint, validates by fingerprint match per TLS handshake.
- **Consequences:** Zero CA infrastructure. Revocation is a DB row
  update. Operator burden equals "add an LXD server today." Manual
  rotation by re-running `register` with a fresh token.
- **Alternatives rejected:** Internal CA + cert issuance (excess
  complexity); JWT-only with no client cert (one stolen token grants
  full agent access); ACME (overkill for internal infra).

### ADR-102 — Per-site PM-only cache, 5 GiB / 30 d

- **Status:** Accepted (v0.3 OQ-A, OQ-F).
- **Context:** Cache speeds builds; node_modules shadow caches multiply
  cache size and create fragility (binary deps, mtime sensitivity).
- **Decision:** v1 caches PM caches only (`npm/_cacache`, pnpm store,
  yarn cache, bun install cache). Per-site directory, 5 GiB cap, 30 d
  TTL. Reset wipes; re-enable wipes.
- **Consequences:** Slightly slower than node_modules shadow; far simpler
  to reason about and reset.
- **Alternatives rejected:** node_modules shadow (future scope); shared
  cross-site cache (cross-tenant risk).

### ADR-103 — Round-robin dispatcher v1

- **Status:** Accepted (v0.2).
- **Context:** Fleet is tiny (1–N engines) operated by us. Sophisticated
  scheduling adds complexity and surfaces bugs that don't matter at this
  scale.
- **Decision:** Pure round-robin over `ONLINE AND capacity_remaining > 0`
  engines that match `proto` and `image_manifest_version`.
- **Consequences:** Easy to reason about; trivially correct; perfectly
  serviceable for v1. Capability labels in the model leave room for
  label-based routing in v2.
- **Alternatives rejected:** Least-loaded (needs reliable in-flight
  counts), priority lanes (no product need yet).

### ADR-104 — Fast-fail only when no compatible online engine exists

- **Status:** Accepted with v0.5 clarification (v0.3 OQ-C, OQ-N pending).
- **Context:** When no engine can run a build, the user must learn
  immediately. Silent queueing leads to surprise multi-hour stalls and
  hides operator alarms.
- **Decision:** Dispatcher fails the pipeline at the BUILD stage
  immediately with `error_code = NO_ENGINE_AVAILABLE` only when no
  compatible engine is online. If compatible engines are online but busy,
  the job remains queued and the BUILD stage reports `WAITING_FOR_ENGINE`
  until capacity opens or OQ-N's max wait is reached. No-build pipelines
  bypass the dispatcher entirely and are unaffected.
- **Consequences:** Failures are visible. Re-run by user (manual redeploy
  or push) once capacity is restored. Operator alerting is driven by
  engine heartbeat OFFLINE transitions, not by BuildJob FAILED counts.
- **Alternatives rejected:** Failing during ordinary saturation (bad UX and
  violates queued execution), retry-with-backoff in the pipeline (couples
  user latency to operator response time).

---

## 28. Frontend Wireframes (ASCII)

> These are intentionally low-fidelity — they specify layout and
> interaction, not visual polish. Reka UI primitives + Tailwind for the
> implementation.

### 28.1 Admin — Build Engines list (`/admin/build-engines`)

```diagram
╭──────────────────────────────────────────────────────────────────────────────╮
│  Build Engines                                            [ + Register ]     │
├──────────────────────────────────────────────────────────────────────────────┤
│  Filter:  [ Status ▾ ]  [ Label ▾ ]                  Search: [___________ ]  │
├──────────────────────────────────────────────────────────────────────────────┤
│  Name              Status     Capacity   Version  Last seen   Actions        │
│  ───────────────── ────────── ────────── ──────── ─────────── ─────────────  │
│  build-sfo-1       ● ONLINE   1/2        1.0.0    3s ago      [ View ]       │
│  build-sfo-2       ● ONLINE   0/2        1.0.0    7s ago      [ View ]       │
│  build-fra-1       ◌ DRAINING 1/2        1.0.0    11s ago     [ View ]       │
│  build-syd-1       ◯ OFFLINE  -/2        1.0.0    14m ago     [ View ]       │
│  build-old-1       ⊘ DISABLED -/-        0.9.3    2d ago      [ View ]       │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### 28.2 Admin — Register engine (`/admin/build-engines/new`)

```diagram
╭──────────────────────────────────────────────────────────────────────────────╮
│  Register a new build engine                                                 │
├──────────────────────────────────────────────────────────────────────────────┤
│  Name:           [ build-engine-sfo-1________________ ]                      │
│  Labels:         [ region=sfo, tier=prod ]            [ + add ]              │
│  Token TTL:      [ 15 minutes ▾ ]                                            │
│                                                                              │
│              [ Cancel ]                  [ Generate token ]                  │
╰──────────────────────────────────────────────────────────────────────────────╯

  After generation:
╭──────────────────────────────────────────────────────────────────────────────╮
│  Registration token (one-time use, expires 15 min)                           │
│  ┌──────────────────────────────────────────────────────────────────────┐    │
│  │ bet_01HKVZ8Y3X7M…                                                    │    │
│  └──────────────────────────────────────────────────────────────────────┘    │
│  [ Copy ]                                                                    │
│                                                                              │
│  Run on the engine host:                                                     │
│                                                                              │
│    build-engine register \                                                   │
│      --backend-url https://api.mincemeat.id \                                │
│      --token bet_01HKVZ8Y3X7M… \                                             │
│      --name "build-engine-sfo-1" \                                           │
│      --max-concurrency 2                                                     │
│                                                                              │
│  Then: systemctl enable --now build-engine                                   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### 28.3 Admin — Engine detail (`/admin/build-engines/{id}`)

```diagram
╭──────────────────────────────────────────────────────────────────────────────╮
│  build-sfo-1   ● ONLINE   v1.0.0   proto 1   img-manifest 1.0.0              │
│  Region=sfo Tier=prod                  [ Drain ] [ Disable ] [ Cache reset ] │
├──────────────────────────────────────────────────────────────────────────────┤
│  Concurrency:  1 / 2 running                                                 │
│  Queue depth:  0                                                             │
│  Cache size:   3.1 GiB across 4 sites                                        │
│  Disk free:    42 GiB                                                        │
│                                                                              │
│  ┌── Last 24h sparklines ─────────────────────────────────────────────────┐  │
│  │  workers_busy   ▁▁▂▃▅▄▂▁▁▁▂▃▄▅▆▆▅▄▂▁▁                                │  │
│  │  queue_depth    ▁▁▁▁▁▁▁▂▁▁▁▁▁▁▁▁▁▁▁▁▁                                │  │
│  │  cache_hit %    ▅▆▇█████▇▇▆▇█████▇▆▇▇▇                                │  │
│  │  p95 duration   ▃▄▄▅▄▃▃▄▅▆▅▄▃▃▄▄▅▅▄▃▃                                │  │
│  └─────────────────────────────────────────────────────────────────────────┘ │
├──────────────────────────────────────────────────────────────────────────────┤
│  Recent jobs                                                       [ See all]│
│  Site            Status      Started    Duration   Cache  Framework          │
│  blog            SUCCEEDED   2m ago     38s        HIT    astro              │
│  docs            SUCCEEDED   6m ago     1m12s      MISS   docusaurus         │
│  landing         FAILED      11m ago    2m04s      —      next.js (export)   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### 28.4 Site settings — Build tab (`/sites/{id}/settings/build`)

```diagram
╭──────────────────────────────────────────────────────────────────────────────╮
│  Settings  > Build                                                           │
├──────────────────────────────────────────────────────────────────────────────┤
│  Detected framework:   Astro 5.0.2   ✓ static-compatible                     │
│  Detected PM:          pnpm (from pnpm-lock.yaml)                            │
│                                                                              │
│  ⓘ Override only if auto-detection is wrong.                                 │
│  Root directory:        [ (repo root) _____________________________ ]        │
│  Framework override:   [ (auto) ▾ ]                                          │
│  Build command:        [ (auto: pnpm run build) ___________________ ]        │
│  Output directory:     [ (auto: dist) _____________________________ ]        │
│  Node version:         [ (auto: 22) ▾ ]                                      │
│                                                                              │
│  Build cache:          [✓] Enabled                                           │
│                        Per-site cache of package-manager downloads.          │
│                        [ Reset cache on all engines ]                        │
│                                                                              │
│              [ Cancel ]                  [ Save ]                            │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### 28.5 Site settings — Build secrets (`/sites/{id}/settings/env`)

```diagram
╭──────────────────────────────────────────────────────────────────────────────╮
│  Settings  > Environment variables (build-time)                              │
├──────────────────────────────────────────────────────────────────────────────╯
│  Build secrets are passed to your build container as environment variables.  │
│  Values are encrypted at rest and never shown after creation.                │
│                                                                              │
│  Key                                Value           Last updated  Actions    │
│  ────────────────────────────────── ──────────── ───────────── ─────────────  │
│  NEXT_PUBLIC_API_URL                ●●●●●●●●●●●●  2d ago        [ Edit ] [X] │
│  SANITY_TOKEN                       ●●●●●●●●●●●●  5d ago        [ Edit ] [X] │
│                                                                              │
│              [ + Add variable ]                                              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### 28.6 Pipeline detail — BUILD stage card

```diagram
╭──────────────────────────────────────────────────────────────────────────────╮
│  Pipeline #1284  ● RUNNING  ·  main@a1b2c3d  ·  triggered by push            │
├──────────────────────────────────────────────────────────────────────────────┤
│  ● PREPARE   ✓  120ms                                                        │
│  ● FETCH     ✓  3.4s                                                         │
│  ● VALIDATE  ✓  90ms     project mode: BUILD_REQUIRED (astro)                │
│  ● BUILD     ◉ RUNNING   12s    ┐                                            │
│              engine: build-sfo-1 │                                           │
│              framework: astro    │                                           │
│              PM: pnpm  cache: HIT│                                           │
│              ┌──────────────────────────────────────────────────────────┐    │
│              │ $ pnpm install --frozen-lockfile                          │    │
│              │ Lockfile is up to date, resolution step is skipped        │    │
│              │ Packages: +423                                            │    │
│              │ ...                                                       │    │
│              │ $ astro build                                             │    │
│              │ 14:23:01 [build] output target: static                    │    │
│              │ ▌ (live log cursor)                                       │    │
│              └──────────────────────────────────────────────────────────┘    │
│  ○ UPLOAD    pending                                                         │
│  ○ ACTIVATE  pending                                                         │
│  ○ FINALIZE  pending                                                         │
│                                                                              │
│  [ Cancel pipeline ]                                                         │
╰──────────────────────────────────────────────────────────────────────────────╯
```

---

## 29. Framework Acceptance Matrix

Per framework, v1 ships with a smoke project (in `tests/fixtures/sites/`)
and the following expectations.

| Framework      | Smoke fixture path                                | Expected build invocation             | Expected output dir | Artifact size band | Required log breadcrumbs                              |
|----------------|---------------------------------------------------|---------------------------------------|---------------------|--------------------|--------------------------------------------------------|
| Astro          | `tests/fixtures/sites/astro-blog/`                | `pnpm run build` → `astro build`      | `dist/`             | 50 KiB – 5 MiB     | "astro build", "complete!"                            |
| Vite           | `tests/fixtures/sites/vite-vanilla/`              | `pnpm run build` → `vite build`       | `dist/`             | 20 KiB – 2 MiB     | "vite vX.Y.Z", "build for production"                 |
| Eleventy       | `tests/fixtures/sites/eleventy-blog/`             | `npm run build` → `eleventy`          | `_site/`            | 20 KiB – 5 MiB     | "Wrote N files"                                       |
| Docusaurus     | `tests/fixtures/sites/docusaurus-docs/`           | `npm run build` → `docusaurus build`  | `build/`            | 1 MiB – 20 MiB     | "Generated static files"                              |
| VitePress      | `tests/fixtures/sites/vitepress-docs/`            | `pnpm run docs:build`                 | `.vitepress/dist/`  | 200 KiB – 10 MiB   | "build complete"                                      |
| VuePress       | `tests/fixtures/sites/vuepress-docs/`             | `pnpm run docs:build`                 | `dist/`             | 200 KiB – 10 MiB   | "build successful"                                    |
| Gatsby         | `tests/fixtures/sites/gatsby-blog/`               | `npm run build` → `gatsby build`      | `public/`           | 500 KiB – 20 MiB   | "Done building in"                                    |
| Hugo           | `tests/fixtures/sites/hugo-quickstart/`           | `hugo`                                | `public/`           | 50 KiB – 5 MiB     | "Pages | N", "Total in"                               |
| Zola candidate | `tests/fixtures/sites/zola-quickstart/`           | `zola build`                          | `public/`           | 50 KiB – 5 MiB     | "Done in"                                             |
| Next.js export | `tests/fixtures/sites/nextjs-export/`             | `pnpm run build` → `next build`       | `out/`              | 500 KiB – 20 MiB   | "Compiled successfully", "Exporting"                  |
| Nuxt generate  | `tests/fixtures/sites/nuxt-generate/`             | `pnpm run generate`                   | `.output/public/`   | 500 KiB – 20 MiB   | "Generated route"                                      |
| SvelteKit static | `tests/fixtures/sites/sveltekit-static/`        | `pnpm run build`                      | `build/`            | 200 KiB – 5 MiB    | "adapter-static"                                       |
| Angular static candidate | `tests/fixtures/sites/angular-static/` | `npm run build` → `ng build`          | `dist/<project>/`   | 500 KiB – 20 MiB   | "Prerendered", "Application bundle generation complete" |
| Remix SPA candidate | `tests/fixtures/sites/remix-spa/`          | `npm run build` → `remix vite:build`  | `build/client/`     | 200 KiB – 10 MiB   | "vite:build"                                          |
| Generic        | `tests/fixtures/sites/generic-static/`            | user-defined `npm run build`          | inferred            | n/a                | n/a                                                    |
| **Negative: Remix SSR** | `tests/fixtures/sites/remix-ssr/`        | n/a                                   | n/a                 | n/a                | BUILD_INCOMPATIBLE with code `REMIX_REQUIRES_SPA_MODE` |
| **Negative: Next.js no-export** | `tests/fixtures/sites/nextjs-noexport/` | n/a                          | n/a                 | n/a                | BUILD_INCOMPATIBLE with code `NEXTJS_REQUIRES_EXPORT`  |

Acceptance criteria for each row:

1. Cold build (cache miss) completes within 5 min on the spec hardware (§32).
2. Warm build (cache hit) shows `cache.event HIT` and finishes within
   `max(cold * 0.5, 30 s)`.
3. Detection picks the framework without manual override.
4. Output contains `index.html`.
5. Post-build validation passes on both engine and backend.
6. The negative cases produce a structured `BUILD_INCOMPATIBLE` payload
   with a `docs_url` pointing at the per-framework guide.

---

## 30. Disaster-Recovery Runbook

| Failure scenario                                | Detection                                            | Automatic mitigation                                                                  | Operator action                                                                                                                                       |
|-------------------------------------------------|------------------------------------------------------|---------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------|
| Engine host crashes mid-build                   | Backend heartbeat watcher (45 s)                     | Mark engine OFFLINE; mark current attempts `ENGINE_LOST`; re-dispatch jobs with attempts remaining | Investigate host; if hardware, drain neighbor engines as needed; restart service when host returns.                                                    |
| Engine process crashes, host alive              | systemd `Restart=on-failure`                         | systemd restarts within 5 s; engine resumes from SQLite queue; reconnects WSS         | None unless restarts loop (`systemctl status build-engine`, `journalctl -u build-engine -e`).                                                          |
| SQLite queue file corrupted                     | `doctor` `PRAGMA integrity_check` fails              | Engine refuses to start                                                                | Stop service, move `queue.sqlite` aside, restart. In-flight jobs (if any) will be marked `ENGINE_LOST` by backend and re-dispatched.                  |
| Docker daemon unreachable                       | `doctor` and `EXEC_INFRA_DOCKER` events              | Job fails with `EXEC_INFRA_DOCKER`; eligible for re-dispatch                          | `systemctl status docker`; restart; if persistent, drain the engine.                                                                                   |
| Disk full on engine                             | Heartbeat reports `disk_free_bytes` low; `EXEC_INFRA_DISK_FULL` events | Job fails fast                                                                        | `build-engine cache reset --all-sites` to reclaim; expand volume; consider lowering per-site cache cap.                                                |
| GHCR outage during image pull                   | `EXEC_INFRA_IMAGE_PULL` events                       | Job fails; eligible for re-dispatch                                                    | Pre-pull images on engines (`docker pull ghcr.io/mincemeat-id/...`) ahead of expected outages; consider a mirror registry in v2.                       |
| Build staging storage outage during artifact upload | Presigned PUT fails with 5xx                      | Engine retries up to 3 times with backoff; then fails with `EXEC_INFRA`                | Check the selected staging provider from OQ-O; user can re-run pipeline once storage recovers.                                                          |
| Backend outage during status stream             | WSS disconnect on engine                             | Engine buffers events in-memory + SQLite; exponential reconnect (1, 2, 5, 10, 30 s)    | None — events drain on reconnect using `last_seq` resumption.                                                                                          |
| No compatible engines online                    | Dispatcher compatible-online set empty               | Each new build fails immediately with `NO_ENGINE_AVAILABLE`                            | Bring up or enable at least one compatible engine; existing no-build pipelines continue unaffected; user must re-run failed pipelines.                  |
| Engine cert / fingerprint lost or compromised   | Operator notices                                     | n/a                                                                                    | `POST /admin/build-engines/{id}/disable`; on the host, rerun `build-engine register` with a fresh token (writes a new cert and creates a new BuildEngine row).|
| backend cert rotates                            | Engine pin mismatch → TLS close                      | Engine refuses to connect, surfaces actionable error                                   | Re-run `build-engine register` to learn the new fingerprint (or distribute updated `credentials.toml` via ops automation).                              |
| Pipeline cancellation timeout                   | `cancel` sent but container ignores SIGTERM > 10 s   | Engine SIGKILLs and marks `CANCELLED`                                                  | None — recorded as `CANCELLED` with note in event log.                                                                                                  |
| Cache poisoning suspected                       | Repeated `INTEGRITY_FAILED` for one site             | Cache auto-wiped on first failure                                                      | If repeats, disable build cache on site, investigate user lockfile, then re-enable (wipe-on-reenable applies).                                          |

---

## 31. Performance Budget (v1 targets)

> Targets assume the hardware in §32. Measured at the engine and reported
> via `build_engine_metric` rollups.

| Workload                                  | Cold (cache miss)     | Warm (cache hit)      |
|-------------------------------------------|-----------------------|-----------------------|
| Astro blog smoke fixture                  | ≤ 90 s                | ≤ 40 s                |
| Vite vanilla smoke fixture                | ≤ 60 s                | ≤ 30 s                |
| Eleventy blog smoke fixture               | ≤ 60 s                | ≤ 30 s                |
| Docusaurus docs smoke fixture             | ≤ 180 s               | ≤ 90 s                |
| Next.js export smoke fixture              | ≤ 180 s               | ≤ 90 s                |
| Hugo quickstart smoke fixture             | ≤ 30 s                | ≤ 20 s                |
| Image-pull on cold engine (`node:20`)     | ≤ 60 s on first use; cached thereafter |               |
| Dispatch latency (job.assign → job.ack)   | p50 ≤ 200 ms, p99 ≤ 1 s |                       |
| Status event latency (engine → user WS)   | p50 ≤ 300 ms, p99 ≤ 2 s |                       |
| Heartbeat-to-OFFLINE detection            | ≤ 60 s (15 s interval + 45 s timeout window) |       |
| Engine cold boot (systemd start → ONLINE) | ≤ 10 s                |                       |
| Artifact upload (10 MiB) to staging storage | ≤ 5 s p95           |                       |

Hard ceilings (already in §8.2 and §3):

- Build wallclock: 10 min.
- Container memory: 2 GiB.
- Container CPU: 1.0.
- Output artifact: 500 MiB.

---

## 32. Hardware Spec — Build Engine Host

Minimum profile for one engine running `max_concurrency = 2` plus the
cache, workspace, and queue:

| Resource     | Minimum               | Recommended           | Notes                                                                                       |
|--------------|-----------------------|-----------------------|---------------------------------------------------------------------------------------------|
| vCPU         | 4 (2 reserved for builds, 2 for engine/docker/system) | 6                     | x86_64; needs cgroup v2.                                                                    |
| RAM          | 6 GiB (2 × 2 GiB containers + 2 GiB OS/engine/docker)  | 8 GiB                  | Headroom for npm install spikes.                                                            |
| Disk (root)  | 20 GiB                | 40 GiB                 | OS + docker overlay2 base images.                                                           |
| Disk (`/var/lib/build-engine`) | 50 GiB           | 100 GiB                | Workspaces + per-site cache (5 GiB × ~10 sites) + queue.                                    |
| Network      | 100 Mbps              | 1 Gbps                 | Image pulls and artifact uploads dominate.                                                  |
| OS           | Ubuntu Server 24.04 LTS or 26.04 LTS                  | 26.04 LTS              | x86_64 only.                                                                                |
| Docker       | 27.x or newer         | latest stable          | cgroup v2 driver; UNIX socket only.                                                         |
| systemd      | 255+                  | bundled with OS         | Required for the unit file directives in §15.2.                                              |

Pre-installed packages: `docker.io` (or Docker CE), `ca-certificates`,
`tzdata`, `jq` (optional, for `doctor --json` consumption).

Dedicated UNIX user `build-engine` in group `docker`. No other workloads
should share `/var/lib/build-engine`.

---

## 33. GHCR Publication Runbook (`mincemeat-id/build-engine-images`)

### 33.1 Repository structure

```
build-engine-images/
├── node/
│   ├── 20.Dockerfile
│   └── 22.Dockerfile
├── bun/
│   └── latest.Dockerfile
├── hugo/
│   └── latest.Dockerfile
├── entrypoint/
│   └── build-entrypoint.sh
├── manifest.json
└── .github/workflows/
    ├── build-and-publish.yml
    ├── trivy-scan.yml
    └── manifest-publish.yml
```

### 33.2 `manifest.json` shape

```json
{
  "version": "1.0.0",
  "images": {
    "node:20":  { "tag": "ghcr.io/mincemeat-id/build-engine-images/node:20-1.0.0",  "digest": "sha256:..." },
    "node:22":  { "tag": "ghcr.io/mincemeat-id/build-engine-images/node:22-1.0.0",  "digest": "sha256:..." },
    "bun:latest":  { "tag": "ghcr.io/mincemeat-id/build-engine-images/bun:1.0.0",   "digest": "sha256:..." },
    "hugo:latest": { "tag": "ghcr.io/mincemeat-id/build-engine-images/hugo:1.0.0",  "digest": "sha256:..." }
  },
  "engine_compat": { "proto_min": 1, "proto_max": 1 }
}
```

### 33.3 Publication pipeline

For each Dockerfile change:

1. PR opened → `build-and-publish.yml` builds the image with reproducible
   build args, runs `trivy-scan.yml` (must pass CVE budget), generates
   SBOM (CycloneDX), and uploads as PR artifact.
2. PR merged → workflow rebuilds, pushes to `ghcr.io/...:<tag>-rc<n>` and
   signs with cosign keyless.
3. Maintainer cuts a release → `manifest-publish.yml` re-tags
   `:<tag>-X.Y.Z`, updates `manifest.json` (digest pinned), opens a PR
   in `mincemeat-id/build-engine` to bump pinned `image_manifest_version`.

### 33.4 CVE budget

- **Block publish:** any CRITICAL with a fix available, OR > 5 HIGH with
  fixes available.
- **Warn:** any MEDIUM with a fix available.
- Trivy database refreshed in CI on each run.

### 33.5 Refresh cadence

| Trigger                        | Action                                                                        |
|--------------------------------|-------------------------------------------------------------------------------|
| Weekly cron                    | Rebuild all images; re-scan; if a new CVE crosses budget, open a release PR.  |
| Node / Bun / Hugo release      | Maintainer opens PR within 7 days for minor; within 24 h for major.           |
| Base-image security update     | Auto-rebuild via GitHub-managed `ubuntu` / `alpine` base.                     |
| `mincemeat-id/build-engine` major version | Coordinated bump of `engine_compat` and re-publish.               |

### 33.6 Rollback

- Each `manifest.json` version is a git tag and a GHCR `:X.Y.Z` tag.
- To roll back, revert the PR that bumped `image_manifest_version` in
  `build-engine`, deploy the older engine binary, or push a new
  `manifest.json` pointing at older digests.
- Image tags are **immutable** once published (no overwrite).

---

## 34. Compatibility Matrix

> Lives in the build-engine repo README post-relocation. Updated on every
> release of any of the three components.

### 34.1 Version axes

- **`coreapp`** (backend + worker + frontend) declares `proto_min`,
  `proto_max`, and optionally `min_engine_version`.
- **`build-engine`** binary declares `proto_version` and pins
  `image_manifest_version`.
- **`build-engine-images`** `manifest.json` declares `engine_compat`
  range.

### 34.2 Compatibility rules (resolution OQ-J)

1. **Backend ↔ engine:** Backend accepts engines whose
   `proto_version ∈ [proto_min, proto_max]`. Else close `4010`.
2. **Engine ↔ images:** Engine refuses `job.assign` whose required image
   is outside its pinned `image_manifest_version`. Operator must upgrade
   the engine binary or the assigned image.
3. **Engine ↔ backend (minimum version):** Backend may also enforce
   `min_engine_version`. On mismatch, close `4010` with hint to upgrade.

### 34.3 Initial matrix (v1.0 set)

| coreapp | build-engine | image-manifest | Notes                  |
|---------|--------------|----------------|------------------------|
| 1.0.x   | 1.0.x        | 1.0.x          | First GA combination.  |

### 34.4 Upgrade ordering

Recommended:

1. Publish new images (`build-engine-images`).
2. Release new engine binary that pins the new image manifest.
3. Drain + upgrade engines in the fleet.
4. Release backend update if `proto` changes.

Order **never** matters for patch-level releases that don't bump
`proto_version` or `image_manifest_version`.

---

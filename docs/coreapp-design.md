# Build Engine Coreapp Design

> **Repository:** `mincemeat-id/coreapp`
> **Status:** Design and decision documentation.
> **Audience:** Backend, worker, shared models, frontend, platform operators.

Coreapp owns build-engine registration, dispatch, audit, retention, pipeline
state, user/operator API surfaces, and UI. The standalone engine executes the
build, but coreapp remains the source of truth for jobs, attempts, logs,
artifacts, and user-visible deployment status.

## Goals

- Add a `BUILD` stage to new GitHub static-site pipelines.
- Keep no-build deployments working even when no build engines exist.
- Dispatch buildable projects to registered engines over a NAT-friendly agent
  channel.
- Persist build job/attempt state with race-safe idempotency.
- Stream BUILD logs into the existing pipeline WebSocket and expose full logs
  through the existing stage-log endpoint.
- Promote built output from platform-owned staging storage into the site's
  configured storage target.
- Give admins fleet visibility and give site owners build configuration,
  cache, and build-secret controls.

## Final Decisions Applied

| Question | Final Decision |
|----------|----------------|
| Busy engines | Queue jobs up to `BUILD_ENGINE_QUEUE_MAX_WAIT_SECONDS=1800`; show `WAITING_FOR_ENGINE`. |
| No compatible online engine | Fail the `BUILD` stage immediately with `NO_ENGINE_AVAILABLE`. |
| Artifact/log staging | Use a platform-owned staging bucket/prefix. |
| TLS termination | Dedicated agent hostname through Traefik/Nginx, not CDN-proxied. Forward verified peer certificate/fingerprint to FastAPI. |
| Multi-replica routing | Redis engine command fanout: `build-engine:commands:{engine_id}`. |
| Framework v1 GA | Astro, Vite, Eleventy, Docusaurus, VitePress, VuePress, Gatsby, Hugo, Zola, Next.js export, Nuxt generate, SvelteKit static, Angular static, Remix SPA, Generic. |
| Network controls | Record policy in job payload; engine enforces egress blocks. |
| Deployment source | Keep `Deployment.source = GITHUB`; add build metadata. |
| Historical pipelines | Do not backfill `BUILD`. |

## Architecture

```text
GitHub webhook / manual redeploy
  -> backend creates Pipeline with stages:
     PREPARE, FETCH, VALIDATE, BUILD, UPLOAD, ACTIVATE, FINALIZE
  -> worker runs PREPARE/FETCH/VALIDATE
  -> worker calls backend dispatcher for BUILD
  -> backend sends job.assign to connected engine through WSS command fanout
  -> engine streams status/log/artifact metadata back
  -> backend records BuildJobAttempt events and fanout to pipeline WS
  -> worker UPLOAD downloads build artifact from staging and promotes files
```

No-build path:

```text
PREPARE -> FETCH -> VALIDATE(no-build publish directory) -> BUILD(SKIPPED)
-> UPLOAD(existing ctx.files) -> ACTIVATE -> FINALIZE
```

Build path:

```text
PREPARE -> FETCH(source tarball) -> VALIDATE(source safety + classify)
-> BUILD(remote engine artifact) -> UPLOAD(validate artifact + promote)
-> ACTIVATE -> FINALIZE
```

## Data Model

Add models in `shared/src/shared/models/` and export them from
`shared.models.__init__`.

| Model | Purpose |
|-------|---------|
| `BuildEngine` | Registered build engine identity, status, cert fingerprint, capabilities, metrics snapshot, version/protocol metadata. |
| `BuildEngineToken` | One-time registration token hash, labels, expiry, consumption audit. |
| `BuildJob` | User-visible build job linked to one `Pipeline` and one `PipelineStage`. Stores source artifact, resolved framework/PM/image, current attempt, final artifact metadata, error state, cache flag. |
| `BuildJobAttempt` | One dispatch/run attempt on one engine. Stores `attempt_id`, engine, status, `last_seq`, artifact metadata, error state, timestamps. |
| `BuildJobEvent` | Status-changing event audit, keyed by `build_job_id` and optional `attempt_id`. Full logs are not stored here. |
| `BuildEngineMetric` | 15s rollup metrics per engine; pruned after `BUILD_ENGINE_METRIC_RETENTION_DAYS=7`. |
| `SiteBuildConfig` | `root_directory`, framework override, build command, output directory, detected output directory, node version, cache enabled. |
| `SiteBuildSecret` | Encrypted build-time env vars. Values are write-only after creation. |

Model changes:

- `PipelineStageName` gains `BUILD`.
- `PipelineStage` gains nullable `log_storage_key`, `log_storage_bytes`,
  `log_storage_compressed`.
- `Deployment.source` remains unchanged. Built GitHub deployments keep
  `Deployment.source = GITHUB`.
- `Deployment.deploy_metadata` adds:

```json
{
  "build_engine": true,
  "build_job_id": "uuid",
  "build_artifact_sha256": "hex",
  "framework_id": "astro",
  "package_manager": "pnpm"
}
```

Important table names for migrations: `users`, `pipelines`,
`pipeline_stages`, `static_sites`, `deployments`.

## Pipeline Integration

Create new pipelines with stage order:

| Index | Stage |
|-------|-------|
| 0 | `PREPARE` |
| 1 | `FETCH` |
| 2 | `VALIDATE` |
| 3 | `BUILD` |
| 4 | `UPLOAD` |
| 5 | `ACTIVATE` |
| 6 | `FINALIZE` |

Historical pipelines remain six-stage and immutable.

`VALIDATE` changes:

- Always validate source archive safety: path traversal, symlink escape,
  blocked file extensions, extracted byte/file limits.
- Resolve `SiteBuildConfig.root_directory`, defaulting to repo root.
- Classify project mode:
  - `NO_BUILD`
  - `BUILD_REQUIRED`
  - `BUILD_INCOMPATIBLE`
- For `NO_BUILD`, keep current publish-directory validation and populate
  `ctx.files`.
- For `BUILD_REQUIRED`, persist source metadata and do not populate `ctx.files`.

`BUILD` stage behavior:

- `NO_BUILD`: mark `SKIPPED`, `error_code = NO_BUILD_REQUIRED`.
- `BUILD_INCOMPATIBLE`: mark `FAILED` with structured guidance.
- `BUILD_REQUIRED`: create `BuildJob`, dispatch or queue, and wait for terminal
  job state.

`UPLOAD` changes:

- No-build: current upload behavior.
- Build: download artifact from platform staging, verify sha256/size/archive
  safety/index.html/file limits, then upload promoted files to the site's
  `StorageTarget`.

Cancellation:

- Pipeline cancellation sends `cancel` over Redis command fanout if an attempt
  is assigned.
- Queued, unassigned jobs become `CANCELLED`.
- In-flight attempts are best-effort cancelled by the engine.

## Dispatch Semantics

Candidate set:

```text
BuildEngine.status == ONLINE
proto_version compatible
image_manifest_version compatible
capabilities include requested image/runtime
not DRAINING for new assignments
```

Outcomes:

- Empty compatible-online set: fail immediately with `NO_ENGINE_AVAILABLE`.
- Compatible engines exist but all busy: keep `BuildJob.status = QUEUED` and
  `PipelineStage.status = RUNNING`; stage detail says `WAITING_FOR_ENGINE`.
- Queue wait exceeds 1800s: fail with `NO_ENGINE_AVAILABLE_TIMEOUT`.
- Capacity opens: round-robin assign to next compatible engine.

Multi-replica routing:

- Backend instance that owns an engine WSS writes:
  `build-engine:connections:{engine_id} = {instance_id, connected_at}`.
- Dispatcher publishes commands to `build-engine:commands:{engine_id}`.
- The owning instance consumes and forwards `job.assign`, `cancel`,
  `cache.reset`, and `drain` on the socket.
- If ownership is stale, dispatcher waits for heartbeat expiry or marks engine
  offline through the watcher.

## API Surface

Admin endpoints:

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/v1/admin/build-engines` | Fleet list with status/capacity. |
| `POST` | `/api/v1/admin/build-engines/registration-tokens` | Issue one-time token. |
| `GET` | `/api/v1/admin/build-engines/{engine_id}` | Engine detail. |
| `PATCH` | `/api/v1/admin/build-engines/{engine_id}` | Name, labels, max concurrency. |
| `POST` | `/api/v1/admin/build-engines/{engine_id}/drain` | Stop new jobs, finish current. |
| `POST` | `/api/v1/admin/build-engines/{engine_id}/disable` | Revoke engine. |
| `POST` | `/api/v1/admin/build-engines/{engine_id}/cache/reset` | Reset one/all site caches on engine. |
| `DELETE` | `/api/v1/admin/build-engines/{engine_id}` | Soft remove after disable. |
| `GET` | `/api/v1/admin/build-jobs` | Global jobs. |
| `GET` | `/api/v1/admin/build-jobs/{job_id}` | Job and attempts. |

Site-owner endpoints:

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/v1/sites/{site_id}/build-config` | Read effective config/detection. |
| `PUT` | `/api/v1/sites/{site_id}/build-config` | Update root, command, output, node, cache. |
| `GET` | `/api/v1/sites/{site_id}/build-secrets` | List redacted keys. |
| `PUT` | `/api/v1/sites/{site_id}/build-secrets/{key}` | Upsert write-only value. |
| `DELETE` | `/api/v1/sites/{site_id}/build-secrets/{key}` | Delete key. |
| `POST` | `/api/v1/sites/{site_id}/build-cache/reset` | Reset this site's cache on all online engines. |

Agent endpoints:

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/v1/build-engines/agent/register` | One-time token + cert registration. |
| `POST` | `/api/v1/build-engines/agent/sessions` | Mint short-lived JWT. |
| `POST` | `/api/v1/build-engines/agent/heartbeats` | Liveness/capacity. |
| `WS` | `/api/v1/build-engines/agent/ws` | Job/control/status/log stream. |
| `POST` | `/api/v1/build-engines/agent/jobs/{job_id}/attempts/{attempt_id}/artifact-upload-url` | Presigned staging PUT URL. |
| `POST` | `/api/v1/build-engines/agent/jobs/{job_id}/attempts/{attempt_id}/ack` | Attempt state acknowledgement. |
| `POST` | `/api/v1/build-engines/agent/metrics` | 15s metrics rollup. |
| `GET` | `/api/v1/build-engines/agent/health` | Doctor endpoint. |

## Auth And TLS

Registration:

1. Admin creates one-time token.
2. Engine self-generates cert/private key.
3. Engine POSTs token, cert PEM, name, capabilities.
4. Backend stores encrypted cert PEM and SHA256 fingerprint.
5. Backend returns `engine_id`, `engine_secret`, backend TLS leaf fingerprint,
   and initial JWT.

Steady state:

- Dedicated agent hostname is not CDN-proxied.
- Traefik/Nginx requests client certs and forwards verified peer cert or
  fingerprint through trusted headers to FastAPI.
- FastAPI matches fingerprint to an enabled `BuildEngine`.
- Short-lived JWT carries `engine_id`, protocol, and capability digest.
- Engine pins backend TLS leaf fingerprint.

## Build Secrets

- Key regex: `[A-Z_][A-Z0-9_]{0,127}`.
- Reserved prefixes: `MINCEMEAT_`, `BUILD_ENGINE_`, `GITHUB_`, `AWS_`,
  `S3_`, `CF_`, `CLOUDFLARE_`.
- Value cap: 16 KiB per key, 128 KiB total per job.
- Stored encrypted with AES-256-GCM/HKDF context `build-engine-secret`.
- Sent to engine only in job payload over authenticated WSS.
- Redaction is best effort; public framework variables may be embedded in the
  final client bundle.

## Config

Add to `.env.example`, backend settings, and worker settings as relevant:

| Variable | Default | Purpose |
|----------|---------|---------|
| `BUILD_ENGINE_AGENT_JWT_TTL_SECONDS` | `3600` | Engine session JWT TTL. |
| `BUILD_ENGINE_HEARTBEAT_TIMEOUT_SECONDS` | `45` | Offline threshold. |
| `BUILD_ENGINE_QUEUE_MAX_WAIT_SECONDS` | `1800` | Saturated-capacity wait. |
| `BUILD_ENGINE_STAGING_BUCKET` | required | Platform-owned staging bucket. |
| `BUILD_ENGINE_STAGING_PREFIX` | `build-engine/` | Artifact/log prefix. |
| `BUILD_ENGINE_LOG_STORAGE_PREFIX` | `build-logs/` | Full log prefix. |
| `BUILD_ENGINE_ARTIFACT_STORAGE_PREFIX` | `build-artifacts/` | Artifact prefix. |
| `BUILD_ENGINE_MAX_ARTIFACT_BYTES` | `524288000` | 500 MiB. |
| `BUILD_ENGINE_MAX_BUILD_SECONDS` | `600` | 10 minutes. |
| `BUILD_ENGINE_METRIC_RETENTION_DAYS` | `7` | Metric retention. |
| `BUILD_ENGINE_COMMAND_REDIS_PREFIX` | `build-engine:commands:` | Socket command fanout. |

## Frontend

Admin pages:

- `/admin/build-engines`
- `/admin/build-engines/new`
- `/admin/build-engines/{id}`
- `/admin/build-engines/{id}/jobs`
- `/admin/build-jobs`

Site-owner pages:

- Site settings -> Build:
  root directory, framework override, build command, output directory, node
  version, cache toggle, cache reset.
- Site settings -> Environment variables:
  write-only build secrets.
- Pipeline detail:
  `BUILD` stage, engine name/version, framework, package manager, cache
  hit/miss, waiting state, live log stream, attempt history.

Frontend must render both historical six-stage pipelines and new seven-stage
pipelines.

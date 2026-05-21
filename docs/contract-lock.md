# Build Engine Stage 0 Contract Lock

Status: Accepted
Date: 2026-05-19

This document is the Stage 0 lock for coreapp-facing build-engine contracts.
Runtime services may add implementation detail, but they should not rename
these schemas, routes, message types, Redis channels, or storage prefixes
without a new ADR and contract regeneration.

## OpenAPI

The canonical OpenAPI contract is generated from FastAPI and includes
OpenAPI-only build-engine paths from `backend/src/app/contracts/build_engine.py`.
The frontend schema generator must continue to expose these component names:

| Area | Generated names |
|------|-----------------|
| Shared | `BuildEngineStatus`, `BuildJobStatus`, `BuildErrorClass`, `BuildPackageManager`, `BuildEngineCapabilities` |
| Admin | `BuildEngineResponse`, `BuildEngineListResponse`, `BuildEngineRegistrationTokenCreate`, `BuildEngineRegistrationTokenResponse`, `BuildEngineUpdateRequest`, `BuildEngineCacheResetRequest`, `BuildEngineCacheResetResponse`, `BuildJobResponse`, `BuildJobAttemptResponse`, `BuildJobListResponse` |
| Site owner | `SiteBuildConfigResponse`, `SiteBuildConfigUpdateRequest`, `SiteBuildSecretItem`, `SiteBuildSecretListResponse`, `SiteBuildSecretUpsertRequest`, `SiteBuildCacheResetResponse` |
| Agent | `BuildEngineAgentRegisterRequest`, `BuildEngineAgentRegisterResponse`, `BuildEngineAgentSessionRequest`, `BuildEngineAgentSessionResponse`, `BuildEngineHeartbeatRequest`, `BuildArtifactUploadUrlRequest`, `BuildArtifactUploadUrlResponse`, `BuildAttemptAckRequest`, `BuildEngineMetricRollupRequest`, `BuildEngineAgentHealthResponse` |
| Redis/WSS command | `BuildEngineCommandType`, `BuildEngineCommandEnvelope` |

Locked route structure:

| Area | Routes |
|------|--------|
| Admin engines | `GET /api/v1/admin/build-engines`, `POST /api/v1/admin/build-engines/registration-tokens`, `GET/PATCH/DELETE /api/v1/admin/build-engines/{engine_id}`, `POST /api/v1/admin/build-engines/{engine_id}/disable`, `POST /api/v1/admin/build-engines/{engine_id}/drain`, `POST /api/v1/admin/build-engines/{engine_id}/cache/reset`, `GET /api/v1/admin/build-engines/{engine_id}/jobs` |
| Admin jobs | `GET /api/v1/admin/build-jobs`, `GET /api/v1/admin/build-jobs/{job_id}` |
| Site owner | `GET/PUT /api/v1/sites/{site_id}/build-config`, `GET /api/v1/sites/{site_id}/build-secrets`, `PUT/DELETE /api/v1/sites/{site_id}/build-secrets/{key}`, `POST /api/v1/sites/{site_id}/build-cache/reset` |
| Agent | `POST /api/v1/build-engines/agent/register`, `POST /api/v1/build-engines/agent/sessions`, `POST /api/v1/build-engines/agent/heartbeats`, `WS /api/v1/build-engines/agent/ws`, `POST /api/v1/build-engines/agent/jobs/{job_id}/attempts/{attempt_id}/artifact-upload-url`, `POST /api/v1/build-engines/agent/jobs/{job_id}/attempts/{attempt_id}/ack`, `POST /api/v1/build-engines/agent/metrics`, `GET /api/v1/build-engines/agent/health` |

## Redis Command Fanout

Commands from any dispatcher replica to the backend replica that owns an engine
socket are published to:

```text
build-engine:commands:{engine_id}
```

Each message is a JSON object matching `BuildEngineCommandEnvelope`:

```json
{
  "v": 1,
  "id": "01JY7P8W8FJ9ZQY9R7R7Q8SXCY",
  "type": "job.assign",
  "ts": "2026-05-19T07:00:01.123Z",
  "engine_id": "00000000-0000-0000-0000-000000000000",
  "payload": {}
}
```

Locked command types:

| Type | Payload |
|------|---------|
| `job.assign` | Same payload as the WSS `job.assign` frame: `build_job_id`, `attempt_id`, `pipeline_id`, `site_id`, `source_download_url`, `source_sha256`, `source_archive_format`, `root_directory`, `framework_id`, `package_manager`, `image`, `build_command`, optional `output_dir`, secret `env`, `cache_enabled`, `resource_limits`, `timeout_seconds`. |
| `cancel` | `build_job_id`, `attempt_id`, `reason`. |
| `cache.reset` | `site_id`, where `null` means all caches on that engine. |
| `drain` | Empty payload. |

The owning socket replica forwards the message unchanged over WSS except for
transport-local bookkeeping. `id` is a ULID unique per sender, and engines treat
`job.assign` idempotently by `(build_job_id, attempt_id)`.

## Staging Storage

Build artifacts and full BUILD logs use a platform-owned bucket, never a
customer site's final storage target.

Locked settings:

| Setting | Default | Notes |
|---------|---------|-------|
| `BUILD_ENGINE_STAGING_BUCKET` | required | Dedicated bucket, environment-scoped, for example `mincemeat-build-engine-staging-dev`. |
| `BUILD_ENGINE_STAGING_PREFIX` | `build-engine/` | Parent prefix for all build-engine staged objects. |
| `BUILD_ENGINE_LOG_STORAGE_PREFIX` | `build-logs/` | Full logs are stored under `build-engine/build-logs/sites/{site_id}/{build_job_id}/{attempt_id}.log.gz`. |
| `BUILD_ENGINE_ARTIFACT_STORAGE_PREFIX` | `build-artifacts/` | Artifacts are stored under `build-engine/build-artifacts/sites/{site_id}/{build_job_id}/{attempt_id}.tar.gz`. |

Lifecycle policy:

- Incomplete multipart uploads expire after 1 day.
- Failed, cancelled, stale-attempt, and superseded objects expire after 1 day.
- Successful staged artifacts expire after 7 days once promoted.
- Full BUILD logs expire with pipeline retention; default retention follows
  `PIPELINE_RETENTION_PER_SITE`.
- Object tags must include `build_job_id`, `attempt_id`, `site_id`, `pipeline_id`,
  `state`, and `expires_at`.

## Frontend Contract Names

Frontend code imports stable aliases from `frontend/src/schemas/buildEngine.ts`.
Route names are locked for Stage 5 implementation:

| View | Route name | Path |
|------|------------|------|
| Admin engine list | `admin-build-engines` | `/admin/build-engines` |
| Admin registration token | `admin-build-engine-new` | `/admin/build-engines/new` |
| Admin engine detail | `admin-build-engine-detail` | `/admin/build-engines/:id` |
| Admin engine jobs | `admin-build-engine-jobs` | `/admin/build-engines/:id/jobs` |
| Admin build jobs | `admin-build-jobs` | `/admin/build-jobs` |
| Site build settings | `site-settings-build` | `/sites/:id/settings/build` |
| Site build secrets | `site-settings-build-secrets` | `/sites/:id/settings/environment` |


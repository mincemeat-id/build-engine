# Build Engine Repository Design

> **Repository:** `mincemeat-id/build-engine`
> **Status:** Final implementation plan.
> **Audience:** Build-engine maintainers, backend integrators, platform
> operators.

The build engine is a standalone Python 3.14 single-binary agent. It connects
outbound to coreapp, accepts build attempts over WSS, executes them in Docker
containers using curated images, streams logs/status, uploads artifacts to
platform staging storage, and reports metrics.

## Goals

- Run static-site framework builds outside the coreapp worker.
- Require no inbound ports on engine hosts.
- Package as a PyInstaller `--onefile` binary for Ubuntu Server 24.04/26.04
  x86_64.
- Use a local durable SQLite WAL queue rather than Redis/Dramatiq.
- Run builds in isolated Docker containers with resource, time, and network
  controls.
- Support parallel workers, default `max_concurrency=2`.
- Provide operator diagnostics through `build-engine doctor`.

## Non-Goals

- Running dynamic/SSR application workloads.
- User-supplied builder images.
- arm64, musl, Windows, or macOS distribution.
- Self-update.
- Distributed cache.
- Inbound engine API.

## Repository Layout

```text
build-engine/
├── pyproject.toml
├── src/build_engine/
│   ├── main.py
│   ├── config.py
│   ├── agent/
│   │   ├── auth.py
│   │   ├── heartbeat.py
│   │   ├── job_loop.py
│   │   ├── protocol.py
│   │   └── uplink.py
│   ├── detect/
│   │   ├── compatibility.py
│   │   ├── framework.py
│   │   ├── lockfiles.py
│   │   └── package_json.py
│   ├── executor/
│   │   ├── artifact.py
│   │   ├── cache.py
│   │   ├── docker_runner.py
│   │   ├── network.py
│   │   ├── stream.py
│   │   ├── validate.py
│   │   └── workspace.py
│   ├── metrics/
│   │   ├── collector.py
│   │   └── reporter.py
│   ├── queue/
│   │   ├── dlq.py
│   │   ├── leases.py
│   │   └── store.py
│   └── cli/
│       └── commands.py
├── packaging/
│   ├── pyinstaller/
│   ├── systemd/build-engine.service
│   └── deb/
├── tests/
└── scripts/
```

## Runtime Model

```text
systemd service
  -> build-engine serve
  -> register/load credentials
  -> open WSS to coreapp agent endpoint
  -> receive job.assign attempts
  -> persist attempts in SQLite queue
  -> worker slots lease attempts
  -> Docker executor runs build
  -> status/log/artifact events stream to backend
```

The engine is single-process async. Worker slots are `asyncio` tasks. Docker
work is wrapped in cancellable subprocess/SDK operations.

## Configuration

Layering order:

| Layer | Path / Source |
|-------|---------------|
| Defaults | compiled settings |
| System config | `/etc/mincemeat/build-engine/config.toml` |
| Credentials | `/etc/mincemeat/build-engine/credentials.toml` |
| Environment | `BUILD_ENGINE_*` |
| CLI | explicit flags |

Required credential files:

- `/etc/mincemeat/build-engine/engine.crt`
- `/etc/mincemeat/build-engine/engine.key`
- `/etc/mincemeat/build-engine/credentials.toml`

Important settings:

| Setting | Default |
|---------|---------|
| `max_concurrency` | `2` |
| `heartbeat_interval_seconds` | `15` |
| `build_timeout_seconds` | `600` |
| `sigterm_grace_seconds` | `10` |
| `container_memory` | `2g` |
| `container_cpus` | `1.0` |
| `artifact_max_bytes` | `524288000` |
| `cache_site_max_bytes` | `5368709120` |
| `cache_ttl_days` | `30` |

## Registration And Auth

Registration:

1. Operator runs:

   ```bash
   build-engine register \
     --backend-url https://agent.mincemeat.id \
     --token <one-time-token> \
     --name build-engine-sfo-1 \
     --max-concurrency 2
   ```

2. CLI generates RSA-3072 or Ed25519 self-signed cert and private key.
3. CLI posts token, cert PEM, name, capabilities, protocol, and image manifest
   version to coreapp.
4. CLI stores `engine_id`, encrypted/hashed `engine_secret` material,
   backend TLS leaf fingerprint, cert path, and key path.

Steady state:

- Engine presents self-signed client cert to the dedicated agent hostname.
- Engine sends short-lived JWT on HTTP/WSS calls.
- Engine pins backend TLS leaf fingerprint.
- Engine rotates JWT before expiry through `/agent/sessions`.
- If backend fingerprint changes, engine refuses connection and `doctor`
  reports the mismatch.

## Agent WSS Protocol

All frames are JSON:

```json
{
  "v": 1,
  "id": "01HY...",
  "type": "status",
  "ts": "2026-05-19T07:00:01.123Z",
  "payload": {}
}
```

Attempt-scoped messages include:

```json
{
  "build_job_id": "uuid",
  "attempt_id": "uuid",
  "seq": 42
}
```

Inbound from backend:

| Type | Purpose |
|------|---------|
| `welcome` | Negotiated protocol and server time. |
| `job.assign` | Assign one build attempt. |
| `cancel` | Cancel attempt. |
| `cache.reset` | Delete site/all cache. |
| `drain` | Stop accepting new attempts and finish current. |
| `ping` | App-level liveness. |

Outbound to backend:

| Type | Purpose |
|------|---------|
| `hello` | Engine version/capabilities. |
| `job.ack` | Attempt state acknowledgement. |
| `status` | Phase updates. |
| `log` | stdout/stderr frames, max 64 KiB. |
| `metric` | Real-time metric events. |
| `artifact.ready` | Artifact sha256/size before upload. |
| `cache.event` | Hit/miss/poisoned/wiped. |
| `error` | Structured error. |
| `heartbeat` | Capacity and disk/cache metrics. |
| `pong` | App-level liveness. |

Ordering:

- `seq` is strictly increasing per `attempt_id`.
- Reconnect resends unacknowledged events from `last_seq + 1`.
- Duplicate `job.assign` for the same `(build_job_id, attempt_id)` returns
  the current local state and does not enqueue twice.

## Local Queue

SQLite path:

```text
/var/lib/build-engine/queue.sqlite
```

Settings:

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
```

Tables:

- `jobs`: attempt payload, state, lease owner, lease expiry, attempts,
  sequence cursor, timestamps, error.
- `events`: outbound event spool for reconnect replay.
- `dlq`: poison attempts after 3 local executor crashes.

States:

```text
QUEUED -> LEASED -> RUNNING -> SUCCEEDED
                         |-> FAILED
                         |-> CANCELLED
```

Lease visibility timeout should exceed heartbeat interval and be refreshed by
the worker while Docker is running.

## Docker Executor

Per attempt:

1. Create `/var/lib/build-engine/jobs/{attempt_id}`.
2. Download source from `source_download_url`.
3. Verify `source_sha256`.
4. Extract to `workspace/src`.
5. Resolve `root_directory`.
6. Run comprehensive framework/package-manager detection.
7. Pull required image by digest/tag from manifest.
8. Mount:
   - source root read-write
   - output directory
   - per-site package-manager cache
   - tmpfs `/tmp`
9. Run container with:
   - `--rm`
   - `--memory=2g --memory-swap=2g`
   - `--cpus=1.0`
   - `--pids-limit=1024`
   - `--read-only`
   - `--user 1000:1000`
   - `--cap-drop=ALL`
   - `--security-opt=no-new-privileges`
   - default seccomp and AppArmor profile
   - no Docker socket mount
10. Stream logs with secret redaction.
11. Enforce 10 minute wallclock; SIGTERM, 10s, SIGKILL.
12. Validate output, package tar.gz, compute sha256.
13. Request presigned upload URL and PUT to staging storage.
14. Ack success or structured failure.
15. Clean workspace, retaining last 5 failed workspaces only when configured.

## Network Policy

`NETWORK_FULL` means public outbound internet for installs/builds. It still
blocks:

- `169.254.169.254` and cloud metadata ranges.
- Docker bridge and host gateway.
- Engine host private addresses.
- Core platform private networks for MariaDB, Redis, MinIO, Nomad, backend
  private service addresses.

Implementation can use Docker bridge rules, `--add-host` avoidance, iptables
owner/container chains, or an engine-managed bridge network. The engine must
fail closed if network guard setup fails.

## Cache

Scope: per `site_id`.

Path:

```text
/var/lib/build-engine/cache/{site_id}/
```

Contents:

- npm `_cacache`
- pnpm store
- yarn cache
- bun install cache

No `node_modules` shadow cache in v1.

Rules:

- Max 5 GiB per site.
- TTL 30 days since last access.
- LRU prune across sites.
- Lockfile hash snapshot invalidates stale cache.
- Disable -> no mount.
- Re-enable -> wipe before reuse.
- `cache.reset` deletes matching site/all cache.

## Framework Detection

V1 GA profiles:

| Framework | Build | Output |
|-----------|-------|--------|
| Astro | package script / `astro build` | `dist` |
| Vite | package script / `vite build` | `dist` |
| Eleventy | package script / `eleventy` | `_site` |
| Docusaurus | package script / `docusaurus build` | `build` |
| VitePress | package script | `.vitepress/dist` |
| VuePress | package script | `dist` |
| Gatsby | package script / `gatsby build` | `public` |
| Hugo | `hugo` | `public` |
| Next.js static export | `next build` | `out` |
| Nuxt generate | `nuxi generate` | `.output/public` |
| SvelteKit static | package script | `build` |
| Generic | `<pm> run build` | inferred |

V1.x candidates after fixtures/docs/images:

- Zola
- Angular static
- Remix SPA mode

Generic output inference order:

```text
out/, dist/, build/, public/, _site/, .output/public/
```

The first directory containing `index.html` wins and is reported to coreapp as
`detected_output_dir`.

## Build Secrets

- Secrets arrive in memory in `job.assign`.
- Engine never persists secret values.
- Container receives secrets as env vars.
- Log redaction replaces exact secret values before outbound log frames.
- Redaction is best effort; transformed or encoded values may leak if user
  code prints them.

## Metrics

Push every 15 seconds:

- workers total/busy
- queue depth
- cache size bytes
- disk free bytes
- jobs running/completed
- docker errors
- uplink reconnects
- cache hit ratio

Inline events:

- job phase durations
- image pull duration
- install/build/package/upload durations
- artifact size
- cache hit/miss

## CLI

Commands:

| Command | Purpose |
|---------|---------|
| `build-engine serve` | Run agent service. |
| `build-engine register` | Register with one-time token. |
| `build-engine status` | Local status and backend reachability. |
| `build-engine doctor` | Full diagnostics, human or JSON. |
| `build-engine cache reset` | Local manual cache reset. |
| `build-engine drain` | Local drain request. |

`doctor` checks:

- binary/protocol version
- Docker reachable
- cgroup v2
- disk space >= 20 GiB
- workspace/cache writable
- cert/key parseable
- backend TLS fingerprint
- agent health endpoint
- WSS handshake
- image pull
- SQLite integrity
- clock skew within 60s
- network guard setup

## Packaging And Host Spec

Distribution:

- Ubuntu Server 24.04/26.04 x86_64.
- PyInstaller `--onefile`.
- Build on Ubuntu 24.04 for oldest supported glibc.
- Signed release artifact with SHA256.

Recommended host for `max_concurrency=2`:

| Resource | Recommended |
|----------|-------------|
| vCPU | 6 |
| RAM | 8 GiB |
| Root disk | 40 GiB |
| `/var/lib/build-engine` | 100 GiB |
| Network | 1 Gbps |
| Docker | 27.x or newer |

Systemd service:

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
ProtectSystem=strict
ReadWritePaths=/var/lib/build-engine /var/log/build-engine
NoNewPrivileges=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
```

## Implementation Plan

### Stage 0 - Contract And Scaffold

Estimate: 3-4 days. Complexity: M.

- [x] Create repo with `uv`, Ruff, ty, pytest, pre-commit.
- [x] Add package skeleton and CLI entrypoint.
- [x] Import protocol/OpenAPI/image manifest contracts from coreapp docs.
- [x] Add CI for lint, type-check, tests, and binary build smoke.
- [x] Add initial README and compatibility matrix.

### Stage 1 - Config, Registration, Auth

Estimate: 4-6 days. Complexity: L.

- [ ] Implement config layering.
- [ ] Implement cert/key generation and filesystem permissions.
- [ ] Implement `register` command.
- [ ] Implement backend TLS fingerprint pinning.
- [ ] Implement session JWT mint/refresh.
- [ ] Implement credential validation.
- [ ] Add unit tests for auth and config.

### Stage 2 - Uplink Protocol

Estimate: 1-1.5 weeks. Complexity: L.

- [ ] Implement WSS connect/reconnect/backoff.
- [ ] Implement protocol envelope validation.
- [ ] Implement hello/welcome negotiation.
- [ ] Implement heartbeat loop.
- [ ] Implement event spool and replay from `last_seq`.
- [ ] Implement command handlers for assign/cancel/drain/cache reset.
- [ ] Add protocol tests with a mock backend.

### Stage 3 - Durable Queue

Estimate: 4-6 days. Complexity: M.

- [ ] Implement SQLite WAL schema migrations.
- [ ] Implement enqueue idempotency by `(build_job_id, attempt_id)`.
- [ ] Implement lease acquisition and refresh.
- [ ] Implement event outbox.
- [ ] Implement DLQ behavior.
- [ ] Add crash/restart tests.

### Stage 4 - Detection And Planning

Estimate: 1 week. Complexity: L.

- [ ] Implement package manager detection.
- [ ] Implement Node version selection.
- [ ] Implement v1 GA framework profiles.
- [ ] Implement static compatibility checks and guidance payloads.
- [ ] Implement Generic output inference.
- [ ] Add fixture tests for each GA profile.

### Stage 5 - Docker Executor

Estimate: 1.5-2 weeks. Complexity: XL.

- [ ] Implement source download, sha256 verification, safe extract.
- [ ] Implement workspace setup and cleanup.
- [ ] Implement image pull by manifest.
- [ ] Implement container run with resource limits and hardening.
- [ ] Implement network guard setup and fail-closed behavior.
- [ ] Implement stdout/stderr streaming with redaction.
- [ ] Implement timeout/cancel SIGTERM->SIGKILL.
- [ ] Implement output validation and artifact packaging.
- [ ] Implement presigned upload flow.
- [ ] Add integration tests with Docker.

### Stage 6 - Cache And Metrics

Estimate: 4-6 days. Complexity: M.

- [ ] Implement per-site PM cache mount mapping.
- [ ] Implement lockfile hash invalidation.
- [ ] Implement TTL/LRU pruning.
- [ ] Implement cache reset command.
- [ ] Implement metrics collector/reporter.
- [ ] Add cache and metrics tests.

### Stage 7 - Doctor, Packaging, Operations

Estimate: 1 week. Complexity: L.

- [ ] Implement `doctor` human output.
- [ ] Implement `doctor --json`.
- [ ] Add systemd unit and install script.
- [ ] Add PyInstaller spec and hidden imports.
- [ ] Add release artifact signing/checksum.
- [ ] Add host setup docs and troubleshooting.
- [ ] Run install/upgrade/drain smoke on Ubuntu 24.04.

### Stage 8 - End-to-End Hardening

Estimate: 1-2 weeks. Complexity: XL.

- [ ] Run Astro/Vite end-to-end against local coreapp.
- [ ] Run all v1 GA framework fixtures.
- [ ] Run cancellation, timeout, OOM, engine-lost, stale-attempt, backend
  reconnect, storage failure, and cache reset drills.
- [ ] Tune log/event throughput and frame limits.
- [ ] Verify binary startup, memory, disk, and cleanup behavior.

## Acceptance Criteria

- Engine can register, reconnect, heartbeat, and show `ONLINE`.
- Duplicate `job.assign` for the same attempt is idempotent.
- Stale attempts cannot report success for current jobs.
- Docker builds run without Docker socket mount and with network guards.
- Astro and Vite complete end-to-end under limits.
- All v1 GA fixture builds pass cold/warm acceptance.
- `doctor` detects common operator failures.
- PyInstaller binary runs on Ubuntu 24.04 and 26.04.

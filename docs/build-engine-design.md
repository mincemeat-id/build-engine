# Build Engine Repository Design

> **Repository:** `mincemeat-id/build-engine`
> **Status:** Design and decision documentation.
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
│   └── systemd/build-engine.service
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

2. CLI posts token, name, capabilities, protocol, and image manifest
   version to coreapp.
3. CLI stores `engine_id`, encrypted/hashed `engine_secret` material, backend
   URL, name, and the initial session JWT.

Steady state:

- Engine sends short-lived JWT on HTTP/WSS calls.
- Engine rotates JWT before expiry through `/agent/sessions`.

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

Implementation uses an engine-managed Docker bridge named
`build-engine-guard` with Linux bridge interface `be-guard0`. Every build
container is started with `--network build-engine-guard`.

The engine installs an iptables chain named `BUILD_ENGINE_GUARD` and attaches
it from `DOCKER-USER` for traffic forwarded from `be-guard0`. The chain drops
egress to:

- `169.254.0.0/16`.
- RFC1918 ranges: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`.
- Alibaba metadata IP `100.100.100.200/32`.
- The Docker gateway discovered from `docker network inspect`.
- Operator/coreapp supplied `network_blocklist` entries.

Operators can extend the deny set with `network_blocklist` in config TOML,
`BUILD_ENGINE_NETWORK_BLOCKLIST`, or the service/doctor CLI flag:

```bash
build-engine serve --network-blocklist 203.0.113.0/24,198.51.100.7
build-engine doctor --network-blocklist 203.0.113.0/24
```

Coreapp may also include `network_blocklist` on `job.assign` as either a
comma-separated string or an array of CIDR/IP strings. Job-level entries are
added to the engine-local operator entries for that attempt.

The guard is fail-closed. If the Docker bridge, gateway discovery, CIDR
validation, or iptables rule installation fails, the engine aborts execution
before `docker run` and reports `error_class=EXEC_INFRA` with
`error_code=NETWORK_GUARD`.

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

Supported framework profiles:

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
| Zola | `zola build` | `public` |
| Next.js static export | `next build` | `out` |
| Nuxt generate | `nuxi generate` | `.output/public` |
| SvelteKit static | package script | `build` |
| Angular static | `ng build --configuration production` | `dist/<project>/browser/` |
| Remix SPA | package script | `build/client/` |
| Generic | `<pm> run build` | inferred |

Generic output inference order:

```text
out/, dist/, build/, public/, _site/, .output/public/
```

The first directory containing `index.html` wins and is reported to coreapp as
`detected_output_dir`.

## Build Secrets

### Contract

Coreapp passes secrets to the engine inline on the `job.assign` payload under
the `secrets` field, as a flat `KEY: VALUE` JSON object:

```json
{
  "secrets": {
    "NPM_TOKEN": "npm_xxxxxxxxxxxx",
    "SENTRY_AUTH_TOKEN": "sntrys_xxxxxxxxxxxx"
  }
}
```

Rules:

- Keys MUST be non-empty strings; valid env var names are `[A-Za-z_][A-Za-z0-9_]*`.
  Engine rejects (skips) any other key.
- Values MUST be non-empty strings; non-string and empty values are ignored.
- Values MUST NOT contain newline characters (`\n`, `\r`).
- Secrets are scoped to a single attempt and never reused across attempts.
- Accepted pairs become container environment variables with exactly the same
  names and string values. For example, payload key `NPM_TOKEN` is visible to
  user build code as `NPM_TOKEN`.
- Coreapp owns filtering which site/build settings are allowed to become build
  secrets. The engine treats the received map as already scoped to the attempt
  and enforces only env-name/value safety.

### Handling

- Secrets arrive in memory in `job.assign`.
- Engine never persists secret values to disk except for a transient
  `mkstemp`-created env file (mode `0600`, randomized name) that is fed to
  `docker run --env-file` and unlinked as soon as the container exits.
- Secrets are passed via `--env-file`, never `--env`, to keep `KEY=VALUE`
  pairs off the host process listing (`ps`, `/proc/<pid>/cmdline`).
- The container receives each pair as a regular environment variable
  (`os.environ["NPM_TOKEN"]`).
- Log redaction is seeded with the same secret values and replaces every
  exact occurrence with `[REDACTED]` before outbound log frames.
- Redaction is best effort; transformed or encoded values (base64, URL
  encoding, partial slices) may leak if user code prints them.

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
- credentials parseable
- agent health endpoint
- WSS handshake
- image pull
- SQLite integrity
- clock skew within 60s
- network guard setup

## Packaging

Distribution:

- Ubuntu Server 24.04/26.04 x86_64.
- PyInstaller `--onefile`.
- Build on Ubuntu 24.04 for oldest supported glibc.
- Signed release artifact with SHA256.

Host sizing, installation, upgrade, diagnostics, and CI runner requirements
live in the [operations runbook](build-engine-operations.md). Release workflow
design lives in the [release process](build-engine-release.md).

Systemd service:

```ini
[Unit]
Description=Mincemeat Build Engine
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=simple
User=build-engine
Group=build-engine
SupplementaryGroups=docker
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

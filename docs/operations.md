# Operations runbook

The supported deployment is Ubuntu 24.04 amd64, systemd, PyInstaller, and a
host Docker daemon. The engine makes outbound HTTPS/WSS connections; no
control-plane listener is required.

## Install and register

Install the packaged binary and service account, then verify the host:

```bash
build-engine --version
build-engine doctor --json
```

Register once with a short-lived coreapp token:

```bash
build-engine register \
  --backend-url https://coreapp.example.com \
  --token '<one-time-token>' \
  --name build-engine-sfo-1 \
  --max-concurrency 2
```

Credentials are written atomically to
`/etc/mincemeat/build-engine/credentials.toml` with mode `0600`; ownership is
set to `build-engine:build-engine` when that account exists. Do not copy the
secret into tickets, shell history, logs, or configuration management output.

## Configuration

The system configuration is
`/etc/mincemeat/build-engine/config.toml`; runtime state is
`/var/lib/build-engine/`. Production configuration should set the HTTPS
`backend_url`, approved `storage_origins`, resource ceilings, and retention
limits. Unknown TOML keys are rejected so a typo cannot silently weaken policy.

The engine advertises protocol v2, manifest `1.0.0`, the eight certified
profiles, npm/pnpm, and unrestricted builder egress. There is no network guard,
iptables chain, per-job blocklist, arbitrary command option, or mutable image
override.

## Health and lifecycle

`doctor` is read-only. It checks credentials, Docker, cgroup v2, disk space,
writable state paths, SQLite integrity, backend health, WSS handshake, and the
bundled digest manifest. Skip only explicitly named checks while diagnosing a
known local dependency.

Use the durable local drain marker during maintenance:

```bash
build-engine drain
# wait for active attempts to finish
build-engine resume
```

The same drain/resume action can be delivered as a WSS `drain` command. A
draining engine rejects new assignments but continues active work until the
operator stops the service.

## Cache and recovery

Caches live under `/var/lib/build-engine/cache/<site-id>/` and are protected by
site locks. Lockfile changes wipe the affected cache. Reset one site or all
sites with `build-engine cache reset [--site-id ...]`.

SQLite leases are refreshed continuously. On restart, expired leases are
recoverable by a new worker; stale lease owners/tokens cannot terminalize the
attempt. The outbox replays unacknowledged events after WSS reconnect and
prunes by age and size. Transport errors never rerun an attempt that already
committed a terminal state.

## Observability

Heartbeats carry capacity, queue, cache, Docker, upload, and reconnect
counters over WSS. A Prometheus textfile is written to
`/var/lib/build-engine/metrics.prom`. Logs are structured and secrets are
redacted before publication. Useful signals include:

- lease refresh failures and stale-worker rejections;
- retryable infrastructure failures and abandoned-attempt recovery;
- Docker stop/kill/remove cleanup results;
- JWT refresh and WSS reconnect counts;
- cache hits, misses, wipes, and lock contention;
- source download, artifact upload, and dropped-log counts.

## Incident handling

For a stuck build, inspect `doctor --json`, the WSS heartbeat, queue depth, and
the attempt workspace. Cancel through coreapp or the WSS command; do not kill
the Docker daemon. If a builder must be quarantined, drain it, wait for active
attempts, collect the bounded failed workspace if enabled, and disable the
engine in coreapp.

For a source or artifact origin failure, fix the approved origin configuration
and reissue the attempt. Never bypass HTTPS or redirect validation in
production.

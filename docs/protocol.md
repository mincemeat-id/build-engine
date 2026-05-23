# Build Engine Protocol Reference

> **Status:** Public contract documentation.
> **Audience:** Build-engine maintainers and operators of the control plane
> (coreapp) the engine connects to.

This document captures the wire-level contract between the build engine agent
and its control plane (coreapp). The contract is locked by:

- `contracts/openapi/build-engine.openapi.json` — agent HTTP surface.
- `contracts/protocol/wss-v1.json` — WSS envelope and message-type names.
- `contracts/image-manifest/manifest.schema.json` — accepted builder-image
  manifest schema.

The build engine is open source; the control plane that originally drives it
is not part of this repository. This document describes only the surface the
engine consumes so any compatible backend implementation can be validated
against it.

## High-Level Flow

```text
Control plane                              Build engine agent
─────────────                              ──────────────────
Register one-time token   --register-->    Persist engine credentials
                                           Open outbound WSS
Welcome (proto, server time)    <--ws--    Hello (version, capabilities)
job.assign (attempt payload)    --ws-->    Enqueue, lease, run Docker
                                <--ws--    status / log / metric / heartbeat
                                <--ws--    artifact.ready (sha256, size)
Mint presigned upload URL   <--http--      Request artifact upload URL
                            --http-->      PUT artifact to staging storage
                                <--ws--    job.ack (success or structured error)
```

## Authentication

### Registration

1. An operator obtains a one-time registration token from the control plane.
2. The operator runs:

   ```bash
   build-engine register \
     --backend-url https://agent.example.com \
     --token <one-time-token> \
     --name build-engine-sfo-1 \
     --max-concurrency 2
   ```

3. The engine POSTs the token, name, capabilities, protocol version, and
   accepted image-manifest version to
   `POST /api/v1/build-engines/agent/register`.
4. The control plane returns `engine_id`, `engine_secret`, and an initial
   short-lived session JWT. The engine persists credentials at
   `/etc/mincemeat/build-engine/credentials.toml` with mode `0600`.

### Session refresh

The engine mints short-lived session JWTs through
`POST /api/v1/build-engines/agent/sessions` using `engine_id` and
`engine_secret`. The JWT carries `engine_id`, the negotiated protocol version,
and a capability digest. JWTs are presented on every HTTP and WSS request.

## WSS Envelope

All frames are JSON. The envelope schema is locked in
`contracts/protocol/wss-v1.json`:

```json
{
  "v": 1,
  "id": "01HY...",
  "type": "status",
  "ts": "2026-05-19T07:00:01.123Z",
  "payload": {}
}
```

Attempt-scoped payloads include:

```json
{
  "build_job_id": "uuid",
  "attempt_id": "uuid",
  "seq": 42
}
```

`seq` is strictly increasing per `attempt_id`. After a reconnect the engine
resends unacknowledged events from `last_seq + 1`. Duplicate `job.assign`
deliveries for the same `(build_job_id, attempt_id)` return the current local
state without enqueueing twice.

### Inbound (control plane → engine)

| Type | Purpose |
|------|---------|
| `welcome` | Negotiated protocol and server time. |
| `job.assign` | Assign one build attempt. |
| `cancel` | Cancel attempt. |
| `cache.reset` | Delete site/all cache. |
| `drain` | Stop accepting new attempts and finish current. |
| `ping` | App-level liveness. |

### Outbound (engine → control plane)

| Type | Purpose |
|------|---------|
| `hello` | Engine version and capabilities. |
| `job.ack` | Attempt state acknowledgement. |
| `status` | Phase updates. |
| `log` | stdout/stderr frames, max 64 KiB. |
| `metric` | Real-time metric events. |
| `artifact.ready` | Artifact sha256/size before upload. |
| `cache.event` | Hit/miss/poisoned/wiped. |
| `error` | Structured error. |
| `heartbeat` | Capacity and disk/cache metrics (authoritative liveness signal). |
| `pong` | App-level liveness. |

## Build Job Lifecycle

States observed by the engine for a single attempt:

```text
QUEUED -> LEASED -> RUNNING -> SUCCEEDED
                          |-> FAILED
                          |-> CANCELLED
```

Phase-level statuses streamed over `status` events include `pulling_image`,
`installing`, `building`, `packaging`, `uploading`, and terminal states.

## HTTP Agent Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/v1/build-engines/agent/register` | One-time token registration. |
| `POST` | `/api/v1/build-engines/agent/sessions` | Mint short-lived JWT. |
| `WS` | `/api/v1/build-engines/agent/ws` | Job/control/status/log stream. |
| `POST` | `/api/v1/build-engines/agent/jobs/{job_id}/attempts/{attempt_id}/artifact-upload-url` | Request presigned staging PUT URL. |
| `POST` | `/api/v1/build-engines/agent/jobs/{job_id}/attempts/{attempt_id}/ack` | Attempt state acknowledgement. |
| `POST` | `/api/v1/build-engines/agent/metrics` | 15s metrics rollup. |
| `GET` | `/api/v1/build-engines/agent/health` | Doctor endpoint. |

The HTTP fallback `POST /api/v1/build-engines/agent/heartbeats` is reserved in
the OpenAPI contract but is not used by the engine — heartbeats are sent
exclusively over the WSS `heartbeat` envelope. Compatible control planes
should treat the WSS heartbeat as authoritative for liveness.

## Build Secrets Contract

The control plane passes per-attempt secrets inline on the `job.assign`
payload under the `secrets` field as a flat `KEY: VALUE` JSON object:

```json
{
  "secrets": {
    "NPM_TOKEN": "npm_xxxxxxxxxxxx",
    "SENTRY_AUTH_TOKEN": "sntrys_xxxxxxxxxxxx"
  }
}
```

Engine-enforced rules:

- Keys MUST match `[A-Za-z_][A-Za-z0-9_]*`.
- Values MUST be non-empty strings without newline characters.
- Secrets are scoped to a single attempt and never reused across attempts.
- Accepted pairs become container environment variables of the same name.
- The control plane is responsible for filtering which site/build settings
  may become build secrets; the engine treats the received map as already
  scoped to the attempt and enforces only env-name/value safety.

Handling on the engine side is documented in [`design.md`](design.md#build-secrets).

## Network Policy

`NETWORK_FULL` permits public outbound internet for installs/builds. The
engine still blocks egress to:

- Cloud metadata ranges (`169.254.0.0/16` and provider-specific metadata IPs).
- RFC1918 ranges.
- Docker bridge and host gateway.
- Operator- or control-plane-supplied `network_blocklist` entries.

The control plane may include `network_blocklist` on `job.assign` as either a
comma-separated string or an array of CIDR/IP strings. Job-level entries are
added to engine-local operator entries for that attempt.

## Manifest Compatibility

The engine ships an accepted manifest version (see `EngineConfig`
defaults) and refuses `job.assign` payloads whose required image is not
present in the accepted manifest. See [`images.md`](images.md) for the
builder-image manifest contract.

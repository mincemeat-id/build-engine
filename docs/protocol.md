# Build-engine protocol v2

Protocol v2 is a clean break. The normative envelope is
[`contracts/protocol/wss-v2.json`](../contracts/protocol/wss-v2.json), and the
OpenAPI subset is regenerated from the separate coreapp checkout.

## Transport

The engine connects to:

```text
wss://<coreapp>/api/v2/build-engines/agent/ws
```

Registration and session refresh use HTTPS:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v2/build-engines/agent/register` | Consume a one-time registration token. |
| `POST` | `/api/v2/build-engines/agent/sessions` | Mint a short-lived session JWT. |
| `GET` | `/api/v2/build-engines/agent/health` | Read-only protocol health check. |
| `GET` | `/api/v2/build-engines/agent/jobs/{job_id}/attempts/{attempt_id}/secrets` | Authenticated transient secret fetch. |
| `POST` | `/api/v2/build-engines/agent/jobs/{job_id}/attempts/{attempt_id}/artifact-upload-url` | Request an attempt-bound artifact PUT URL. |

Registration includes the engine package version (`0.3.0`) separately from the
builder image manifest compatibility version (`1.0.0`).

Heartbeat, commands, status, logs, artifacts, errors, and event acknowledgements
are WSS messages. The old HTTP heartbeat, metrics, and acknowledgement routes
are not part of v2.

## Envelope

Every frame is a JSON object with:

```json
{
  "v": 2,
  "id": "canonical-uuid",
  "type": "attempt.status",
  "ts": "2026-08-13T00:00:00.000Z",
  "engine_id": "canonical-uuid",
  "build_job_id": "canonical-uuid",
  "attempt_id": "canonical-uuid",
  "seq": 7,
  "payload": {}
}
```

`id`, `engine_id`, and attempt identifiers use canonical UUID spelling.
Timestamps must be within five minutes of the receiver clock. Frames are at
most 1 MiB and log data is at most 64 KiB per frame. Unknown envelope fields,
negative sequences, and sequences without an attempt are rejected.

The authenticated engine ID must match the envelope. Attempt events use a
strictly increasing sequence per attempt. Repeating the same sequence with the
same logical event is idempotent; a different event at that sequence is a
protocol error.

## Message types

Backend to engine:

| Type | Meaning |
|---|---|
| `welcome` | Negotiated protocol, heartbeat interval, and replay cursors. |
| `job.assign` | Assignment metadata for one attempt. |
| `job.cancel` | Cancellation request; it does not directly force a terminal state. |
| `cache.reset` | Site-scoped or global cache reset. |
| `drain` | Enter drain mode, or use payload action `resume` to leave it. |
| `ping` | Liveness probe. |
| `event.ack` | Replay cursor acknowledgement. |

Engine to backend:

| Type | Meaning |
|---|---|
| `hello` | Version, capabilities, and manifest compatibility after `welcome`. |
| `attempt.status` | Lifecycle phase, metrics, or terminal state. |
| `attempt.log` | Bounded, redacted best-effort output. |
| `artifact.ready` | Verified artifact digest and byte count. |
| `heartbeat` | Liveness, capacity, cache, and operational counters. |
| `pong` | Response to `ping`. |
| `error` | Structured execution or protocol error. |

## Assignment boundary

An assignment contains only:

- build, site, and attempt UUIDs;
- attempt-scoped HTTPS source URL, archive format, byte count, and SHA-256;
- root directory;
- one of the certified framework IDs and profile version;
- bounded timeout and resource limits;
- cache policy.

Image references, commands, manifest paths, network blocklists, and secret
values do not cross the wire. The engine chooses commands and immutable images
from its bundled profile and manifest resources.

## Source, secrets, and artifacts

Coreapp creates a short-lived source GET URL only after locking and verifying
the current attempt. The engine checks HTTPS, approved origins, redirects,
declared size, archive traversal, and SHA-256 before building.

Secrets are fetched over an authenticated attempt-scoped HTTPS endpoint only
after current-attempt and engine ownership checks. They are held in memory,
provided through a mode-`0600` transient Docker env file, redacted from logs,
and deleted after container cleanup. They are absent from SQLite, event
payloads, command lines, and artifact metadata.

Artifact upload URLs are checked for HTTPS, approved origin, canonical attempt
scope, expected size, and expected SHA-256. Redirects are rejected for uploads.

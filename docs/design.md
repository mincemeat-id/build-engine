# Build-engine v2 design

The build engine is an outbound-only Python 3.14 service. It authenticates to
coreapp, receives assignments over WSS, downloads a verified source archive,
builds it in a certified Docker image, packages only `out/`, uploads the
artifact, and reports lifecycle events over the same WSS connection.

## Boundaries

```text
coreapp                 build engine                         Docker builder
---------               -------------------                  -------------
assignment  -- WSS -->  SQLite assignment/state  -- mounts --> src / out / cache
source URL  <---------  HTTPS source + SHA-256                  no host socket
secret fetch <--------  HTTPS, transient only
artifact URL -------->  HTTPS PUT, origin checked
events      <-- WSS --  status / bounded logs / heartbeat
```

The engine never accepts a build command, image path, mutable image tag,
manifest path, network blocklist, or secret value from an assignment. The
profile registry resolves commands and image keys locally.

## Runtime state

`<state>/queue.sqlite` is the single transactional local repository. It stores
assignment metadata, the attempt state, lease owner/token, cancellation flag,
sequence cursor, crash/DLQ information, and the outbound event spool. Secrets
and transient secret environments are never written to it. Expiring source
URLs are kept only for the lifetime of the in-memory assignment.

Attempt states are explicit:

```text
QUEUED -> LEASED -> RUNNING -> SUCCEEDED
                         ├──> FAILED
                         ├──> CANCELLED
                         └──> TIMED_OUT
```

An expired lease can be reclaimed with a compare-and-set owner/token check.
Cancellation is a separate request flag; a worker observes it and performs
container cleanup before committing the terminal state. A completed attempt is
never rerun because an event or transport publish failed.

The repository opens SQLite in WAL mode, uses immediate transactions for lease
operations, refreshes live leases continuously, commits the outbox before
returning, replays events after WSS reconnect, and prunes terminal rows and
bounded event history. `close()` is explicit and idempotent.

## Attempt workspace

Every attempt receives a fresh canonical workspace:

```text
<state>/workspaces/<attempt-uuid>/
  src/       verified and safely extracted source
  out/       builder output; the only packaged directory
  cache/     attempt-local mount staging
  tmp/       source archive, manifest, and transient files
```

Source downloads require HTTPS by default, an approved origin, a positive
declared size, a SHA-256 match, and a configured byte limit. Redirects remain
HTTPS and within the approved origin set. Archive members, links, root
directories, attempt IDs, site IDs, and upload URLs are validated before use.

## Build lifecycle

1. Validate the assignment and obtain the attempt-scoped secret environment.
2. Download and verify the source archive, then extract it with traversal and
   device-entry checks.
3. Resolve the project through the versioned eight-profile registry.
4. Resolve an image through the bundled manifest to `tag@sha256:digest`.
5. Prepare the per-site cache under a lock and enforce local resource limits.
6. Run Docker with `--init`, dynamic service UID/GID, labels, a read-only root,
   dropped capabilities, no-new-privileges, bounded memory/CPU/PIDs, and the
   selected unrestricted outbound network mode.
7. Stop, kill, and remove the container on cancellation, timeout, or shutdown.
8. Validate and deterministically package only `out/`, request an attempt-bound
   upload URL, verify its HTTPS origin, and upload the artifact.
9. Commit the terminal state first, then publish durable status and artifact
   events. Log publishing is bounded and best effort.

Build failures, user configuration failures, cancellation, timeout, Docker
failures, transport failures, and shutdown are represented separately so retry
policy cannot mistake a completed build for an incomplete one.

## Certified profile registry

Release `0.3.0` supports only Astro, Vite, Eleventy, Docusaurus, VitePress,
VuePress, Gatsby, and Hugo. Node profiles use Node 22 and npm or pnpm. Hugo
uses the pinned Hugo image and has no Node package manager. Compatibility
checks are execution-blocking. Bun, Yarn, Zola, Generic, Angular, Remix,
Next, Nuxt, and SvelteKit are explicitly deferred and never silently fall
back to Vite or a generic command.

## Authentication and events

Registration consumes a one-time token and atomically writes credentials with
mode `0600` and service-user ownership when the account exists. The engine
refreshes short-lived JWTs before reconnecting. WSS v2 requires canonical UUIDs,
bounded clock skew, bounded frames/logs, authenticated `engine_id`, and
attempt-scoped monotonic sequences. Coreapp acknowledges replayed events and
deduplicates identical sequences while rejecting conflicting duplicates.

Heartbeats, status, artifacts, errors, commands, and acknowledgements use WSS;
there is no parallel v1 HTTP heartbeat or acknowledgement path. Metrics are
included in heartbeats and written locally as a Prometheus textfile.

## Operations

The read-only `doctor` command checks credentials, Docker, cgroup v2, disk,
paths, SQLite integrity, backend health, WSS handshake, and the pinned image
manifest. `drain` stops new assignments while allowing active work to finish;
`resume` removes the durable local drain marker. Cache reset is site-scoped or
global and uses the same per-site locking discipline as builds.

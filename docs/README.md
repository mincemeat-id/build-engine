# Build Engine Documentation

![Status: protocol v2 production candidate](https://img.shields.io/badge/status-protocol%20v2%20production%20candidate-blue)

The build engine is a Python 3.14 single-binary service for Ubuntu 24.04
amd64. It connects outbound to coreapp over authenticated WSS, executes
curated Docker builds, streams bounded logs and durable lifecycle events, and
uploads artifacts to platform staging storage.

## Documentation index

- [Design](design.md) — architecture, state repository, worker lifecycle, and
  security boundaries.
- [Protocol](protocol.md) — clean-break WSS v2 and the small HTTP control
  surface.
- [Builder images](images.md) — digest-pinned manifest and eight-profile
  certification matrix.
- [Operations](operations.md) — install, health, drain/resume, recovery, and
  observability.
- [Release](release.md) — package, manifest, verification, and smoke process.
- [Contributor guide](../CONTRIBUTING.md) — hooks and required checks.
- [Security policy](../SECURITY.md) — private vulnerability reporting.

## Fixed decisions

| Area | Decision |
|------|----------|
| Protocol | WSS v2 only for commands, events, heartbeats, and event acknowledgements. |
| State | One transactional SQLite repository with leases, cancellation flags, event spool, and retention. |
| Source | Attempt-scoped HTTPS URL with declared byte count and SHA-256. |
| Secrets | Authenticated, transient attempt-scoped fetch; never persisted in state or events. |
| Builders | Docker with immutable manifest references, dynamic UID/GID, resource limits, `--init`, and unrestricted outbound egress. |
| Profiles | Astro, Vite, Eleventy, Docusaurus, VitePress, VuePress, Gatsby, and Hugo only. |
| Package | Version `0.3.0`, PyInstaller one-file binary, systemd on Ubuntu 24.04 amd64. |

`make verify` is the required local deployment-readiness gate. The opt-in Docker
harness is required before a production image or package release is approved.

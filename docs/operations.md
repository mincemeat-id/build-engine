# Build Engine Operations

This runbook covers one Ubuntu Server 24.04/26.04 amd64 host running the
standalone Mincemeat build engine.

## Host Setup

Recommended host profile for `max_concurrency = 2`:

| Resource | Recommended |
|----------|-------------|
| vCPU | 6 |
| RAM | 8 GiB |
| Root disk | 40 GiB |
| `/var/lib/build-engine` | 100 GiB |
| Network | 1 Gbps |
| Docker | 27.x or newer |

Install host dependencies:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl docker.io tzdata
sudo systemctl enable --now docker
```

Install or upgrade the Debian package:

```bash
sudo apt-get install -y ./mincemeat-build-engine_0.2.0_amd64.deb
```

Register the engine with a one-time token issued by the control plane
(coreapp):

```bash
sudo build-engine register \
  --backend-url https://agent.example.com \
  --token <one-time-token> \
  --name build-engine-sfo-1 \
  --max-concurrency 2
```

Start the service:

```bash
sudo systemctl enable --now build-engine
sudo build-engine doctor
```

The packaged unit intentionally uses `Type=simple`; readiness is enforced by
the agent's startup self-test before it opens the WSS uplink. The service runs
as `build-engine:build-engine` and receives Docker access through the
supplementary `docker` group. `credentials.toml` must be mode `0600`, owned by
that service uid/gid, and contain an ASCII `engine_secret` of at least 32 bytes.

## Upgrade

The v1 engine does not self-update. Drain the engine in coreapp when possible,
then replace the package:

```bash
sudo systemctl stop build-engine
sudo apt-get install -y ./mincemeat-build-engine_0.2.1_amd64.deb
sudo systemctl start build-engine
sudo build-engine doctor
```

## Release Artifacts

Build the PyInstaller one-file binary, then produce checksums and optional
signatures. The Debian package is built from the same binary and included in
release checksums.

```bash
uv run pyinstaller packaging/pyinstaller/build-engine.spec --noconfirm
make deb
bash scripts/release-artifacts.sh
COSIGN_SIGN=1 bash scripts/release-artifacts.sh
GPG_SIGN=1 bash scripts/release-artifacts.sh
```

`scripts/release-artifacts.sh` writes:

- `dist/build-engine-<version>-linux-amd64`
- `dist/mincemeat-build-engine_<version>_amd64.deb`
- `dist/SHA256SUMS`
- `dist/build-engine-<version>-linux-amd64.sig` and `.pem` when `COSIGN_SIGN=1`
- `dist/build-engine-<version>-linux-amd64.asc` when GPG signing is enabled
- `dist/build-engine-<version>-linux-amd64.cdx.json` when the release workflow
  generated the CycloneDX SBOM

## Verifying The Release

Release consumers should verify the binary before installing it:

```bash
scripts/verify-release.sh v0.2.0
sudo apt-get install -y ./mincemeat-build-engine_0.2.0_amd64.deb
```

The helper downloads the GitHub Release assets with `gh`, checks
`SHA256SUMS`, verifies the cosign keyless blob signature against the GitHub
OIDC issuer, verifies SLSA provenance with `slsa-verifier`, and confirms the
CycloneDX SBOM is present.

## Diagnostics

Use human output for an operator shell:

```bash
sudo build-engine doctor
```

Use JSON for automation:

```bash
sudo build-engine doctor --json | jq .
```

`doctor` exits non-zero when any required check fails. It verifies the binary
and protocol version, Docker, cgroup v2, disk space, writable workspace/cache
paths, credentials, agent health, WSS welcome negotiation, image pull, SQLite
integrity, clock skew, and network guard setup.

`build-engine serve` runs the same startup diagnostics before connecting to
coreapp, skipping only `image_pull` and `wss_handshake` to avoid a slow pull and
because the serve path itself opens the persistent WSS connection. Operators can
run a confined local development engine with:

```bash
build-engine serve --state-dir ./state --no-network-guard
```

This uses Docker `--network none` for build containers and avoids installing the
iptables network-guard chain. When both flags are present, credentials are
validated against the current uid/gid instead of the packaged service account.

## Metrics Textfile

The agent writes Prometheus-compatible textfile metrics to
`/var/lib/build-engine/metrics.prom` on every metrics interval and again during
graceful shutdown. Point node-exporter at `/var/lib/build-engine` with
`--collector.textfile.directory=/var/lib/build-engine` to scrape local gauges
and counters such as `build_engine_workers_busy`,
`build_engine_queue_depth`, `build_engine_cache_size_bytes`, and
`build_engine_uplink_reconnects_total`.

## Graceful Drain

`SIGTERM` and `SIGINT` put the agent into local drain mode, persist
`/var/lib/build-engine/drain.json`, stop accepting new assignments, close the
WSS uplink with code `1001` / reason `engine_drain`, and wait up to
`sigterm_grace_seconds` for running attempts to finish before cancelling local
tasks.

## Troubleshooting

| Symptom | Likely Cause | Action |
|---------|--------------|--------|
| `credentials` fails | Engine has not been registered or credentials are unreadable | Re-run `build-engine register` with a fresh token. |
| `docker` fails | Docker is stopped or the `build-engine` user is not in the Docker group | Run `systemctl status docker`; fix group membership; restart the service. |
| `disk_space` fails | `/var/lib/build-engine` has less than 20 GiB free | Run `build-engine cache reset`, prune Docker images, or expand the volume. |
| `sqlite_integrity` fails | Local queue database is corrupted | Stop the service, move `queue.sqlite` aside, and restart. Coreapp will redispatch lost attempts. |
| `image_pull` fails | Docker registry/network outage or bad image config | Try `docker pull <image>` manually and inspect proxy/firewall settings. |
| `wss_handshake` fails | Auth, protocol, or backend routing issue | Refresh the session, check backend logs, and confirm the agent WSS hostname bypasses CDN proxying. |

For a local packaging smoke on Ubuntu 24.04:

```bash
bash scripts/smoke-ubuntu-24.04.sh
```

## CI Infrastructure

The build-engine CI workflows run on GitHub-hosted runners (`ubuntu-24.04`).
The default runner image already provides everything the workflows need:

- Docker engine for the executor smoke and the optional Ubuntu container
  smoke (`make ubuntu-24-smoke`).
- `iptables` / `nftables` tooling for the network-guard fixtures when they
  are exercised on the host (privileged tests that require live `NET_ADMIN`
  are gated to opt-in suites).
- `uv` + Python 3.14 installed on demand via `astral-sh/setup-uv` and
  `uv python install 3.14`.
- PyInstaller binary smoke against the same Ubuntu 24.04 baseline used for
  the supported engine host spec.

### Workflow pinning

Every workflow in `.github/workflows/` pins to the GitHub-hosted runner
label so runs are reproducible across forks:

```yaml
runs-on: ubuntu-24.04
```

The release workflow can optionally fan out to `ubuntu-24.04-arm` when the
repository variable `ENABLE_ARM64=true` is set.

Forks that want to use a self-hosted runner pool can override `runs-on`
through reusable workflows or a fork-local patch; nothing in the agent or
test suite assumes a self-hosted environment.

### Installer smoke matrix

The `install` matrix entry in `.github/workflows/ci.yml` re-uses the
PyInstaller binary produced by the `verify` step and runs
`scripts/install-build-engine.sh` with `DESTDIR=$(mktemp -d)`. This
verifies that the on-disk layout the production deploy depends on
(binary, systemd unit, default config, sysconfdir layout) still installs
cleanly on the runner image without root privileges.

The release workflow builds a Debian package with `packaging/deb/build-deb.sh`
from that same staged installer, so the smoke path and package layout stay in
lockstep.

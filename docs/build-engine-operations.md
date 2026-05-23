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

Install or upgrade the binary:

```bash
sudo BUILD_ENGINE_BINARY=dist/build-engine bash scripts/install-build-engine.sh
```

Register the engine with a one-time token from coreapp:

```bash
sudo build-engine register \
  --backend-url https://agent.mincemeat.id \
  --token <one-time-token> \
  --name build-engine-sfo-1 \
  --max-concurrency 2
```

Start the service:

```bash
sudo systemctl enable --now build-engine
sudo build-engine doctor
```

## Upgrade

The v1 engine does not self-update. Drain the engine in coreapp when possible,
then replace the binary:

```bash
sudo systemctl stop build-engine
sudo BUILD_ENGINE_BINARY=build-engine.new bash scripts/install-build-engine.sh
sudo systemctl start build-engine
sudo build-engine doctor
```

## Release Artifacts

Build the PyInstaller one-file binary, then produce checksums and optional
signatures:

```bash
uv run pyinstaller packaging/pyinstaller/build-engine.spec --noconfirm
bash scripts/release-artifacts.sh
COSIGN_SIGN=1 bash scripts/release-artifacts.sh
GPG_SIGN=1 bash scripts/release-artifacts.sh
```

`scripts/release-artifacts.sh` writes:

- `dist/build-engine-<version>-linux-amd64`
- `dist/SHA256SUMS`
- `dist/build-engine-<version>-linux-amd64.sig` when `COSIGN_SIGN=1`
- `dist/build-engine-<version>-linux-amd64.asc` when GPG signing is enabled

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

The build-engine CI workflows run exclusively on the project's sanctioned
self-hosted runner pool. Public hosted runners cannot satisfy the engine's
test requirements:

- **Docker-in-Docker** for the executor integration tests (job lifecycle,
  workspace, network guard).
- **nftables / iptables (NET_ADMIN)** for the network-guard egress chain
  fixtures in `tests/test_network_guard.py`.
- **PyInstaller binary smoke** against the same baseline image as the
  supported engine host spec.

### Pool baseline

| Property | Value |
|----------|-------|
| OS | Ubuntu 24.04 LTS |
| Architecture | amd64 (`x64`) |
| Container runtime | Docker 27.x with overlay2 + cgroup v2 |
| Privileges | NET_ADMIN for nftables fixtures, rootless `actions` user otherwise |
| Toolchain | `uv` cache directory pre-warmed; Python 3.14 installed on demand by `uv python install` |
| Disk | ≥ 60 GiB free on `/var/lib/docker` |
| Network | Outbound HTTPS only; metadata range `169.254.169.254/16` blocked at host firewall |

### Workflow pinning

Every workflow in `.github/workflows/` MUST pin to the labelled selector
below so unintended runners (including stray repository-level runners or
the GitHub-hosted fallback) cannot match:

```yaml
runs-on: [self-hosted, linux, x64, ubuntu-24.04]
```

The bare `runs-on: self-hosted` selector is forbidden because it would
allow any runner registered against the org — including unprivileged
arm64 or non-Ubuntu hosts — to pick up the job.

### Hardening expectations

Runners in the sanctioned pool are configured to:

- run each job in an ephemeral workspace (`actions-runner --ephemeral`);
- reset Docker state (`docker system prune -af --volumes`) between jobs;
- enforce a 30-minute job timeout matching the workflow `timeout-minutes`;
- block egress to RFC1918 and cloud metadata ranges except for the
  coreapp staging endpoint and the configured registry mirrors;
- rotate the runner registration token monthly.

### Installer smoke matrix

The `install` matrix entry in `.github/workflows/ci.yml` re-uses the
PyInstaller binary produced by the `verify` step and runs
`scripts/install-build-engine.sh` with `DESTDIR=$(mktemp -d)`. This
verifies that the on-disk layout the production deploy depends on
(binary, systemd unit, default config, sysconfdir layout) still installs
cleanly on the runner image without root privileges.

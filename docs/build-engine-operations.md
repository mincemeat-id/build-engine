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

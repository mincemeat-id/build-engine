# Debian Package

Build the package from a PyInstaller binary:

```bash
make binary-smoke
make deb
```

The package is written to `dist/mincemeat-build-engine_<version>_<arch>.deb`.
It installs the binary, systemd unit, default config, operations runbook, and
runtime directories using the same staged layout as `scripts/install-build-engine.sh`.

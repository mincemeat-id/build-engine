# AGENTS.md — Mincemeat Build Engine

## Architecture

This repository contains the standalone Mincemeat build-engine agent. It is a
Python 3.14 single-binary service that connects outbound to coreapp, accepts
build attempts over WSS, executes static-site builds in curated Docker builder
images, streams logs/status, uploads staged artifacts, and reports metrics.

**Repository layout:**

| Directory | Purpose |
|-----------|---------|
| `src/build_engine/` | Python package for the agent, CLI, auth, protocol, queue, executor, detection, cache, and metrics |
| `contracts/` | Imported OpenAPI, WSS protocol, and builder image manifest contracts |
| `docs/` | Canonical design docs and implementation checklist |
| `packaging/pyinstaller/` | PyInstaller onefile binary spec and smoke path |
| `scripts/` | Local maintenance scripts, including contract sync and hook installation |
| `tests/` | Unit and contract smoke tests |

The adjacent coreapp checkout is expected at `../coreapp` when refreshing the
OpenAPI subset with `make contracts-sync`.

---

## Build & Run

```bash
uv sync
uv run build-engine --version
uv run build-engine doctor --json
```

Registration/auth commands:

```bash
uv run build-engine register \
  --backend-url https://agent.mincemeat.id \
  --token <one-time-token> \
  --name build-engine-sfo-1 \
  --max-concurrency 2

uv run build-engine status
uv run build-engine session refresh
```

Important paths:

| Path | Purpose |
|------|---------|
| `/etc/mincemeat/build-engine/config.toml` | System config |
| `/etc/mincemeat/build-engine/credentials.toml` | Registered engine credentials |
| `/var/lib/build-engine/` | Queue, cache, workspaces, and runtime state |

---

## Deployment Readiness — required before declaring a task done

> **Mandatory for every code change** (humans and AI agents alike).
> If `make verify` is red, do not push, do not open a PR, and do not tell the
> user the task is complete. Fix the failures first.

```bash
make verify
```

`make verify` runs, in order:

1. `make contracts-sync` — refreshes the coreapp OpenAPI subset.
2. `python -m compileall src tests` — fast syntax-regression canary.
3. `ruff check .` — Python lint.
4. `ruff format --check .` — Python formatting.
5. `ty check` — Python type-check.
6. `pytest` — unit and contract smoke tests.
7. PyInstaller binary smoke — builds `dist/build-engine` and runs
   `./dist/build-engine --version`.

During iteration, narrower targets are fine:

```bash
make lint
make typecheck
make test
make binary-smoke
```

The final gate before reporting completion is always `make verify`.

### Local Guardrails — install once per checkout

Repo-tracked git hooks enforce the same gate locally so failures are caught
before they reach CI:

```bash
make hooks-install
```

| Hook | Runs |
|------|------|
| `pre-commit` | Ruff lint + Ruff format check on staged Python files |
| `pre-push` | Full `make verify`, matching the CI gate |

Bypass only for emergencies: `BYPASS_HOOKS=1 git commit ...`,
`BYPASS_HOOKS=1 git push ...`, or Git's `--no-verify`. The tracked
`pre-push` hook rejects `BYPASS_HOOKS=1` on `master`, `main`, `release/*`,
and tags.

---

## Code Style

- Python **3.14+**, managed with `uv`.
- Ruff line length is 100, target version is `py314`, and lint rules include
  `E/F/I/UP/B/SIM/ANN`.
- `ty` is the type checker; run from the repo root.
- Prefer stdlib and small focused modules until later stages justify a runtime
  dependency.
- Use structured parsers/APIs for TOML, JSON, and contracts.
- Keep Stage work scoped to the implementation plan in
  `docs/build-engine-design.md`.

### Python 3.14 Grammar

This repository targets Python ≥ 3.14. Do not rewrite valid modern grammar into
older equivalents just because it looks unfamiliar. If in doubt, run:

```bash
uv run python -m compileall path/to/file.py
```

Prefer:

| Construct | Example |
|-----------|---------|
| PEP 695 aliases/generics | `type Vector = list[float]` |
| PEP 604 unions | `str | None` |
| `match` / `case` | Use freely where it clarifies branching |
| PEP 758 unparenthesized multi-except without binding | `except ValueError, TypeError:` |

Use parentheses when binding exceptions with `as`, and always chain raised
exceptions with `from` unless suppression is intentional.

---

## Contracts

- `contracts/openapi/build-engine.openapi.json` is generated from
  `../coreapp/frontend/openapi.json`.
- `contracts/protocol/wss-v1.json` locks WSS envelope/message names.
- `contracts/image-manifest/manifest.schema.json` locks the builder image
  manifest schema.

After coreapp contract changes, run:

```bash
make contracts-sync
make verify
```

---

## Documentation Map

- Start with `docs/README.md` for the design index.
- Current implementation checklist lives in `docs/build-engine-design.md`.
- Builder image repository design lives in `docs/build-engine-images-design.md`.
- Coreapp integration design lives in `docs/coreapp-design.md`.

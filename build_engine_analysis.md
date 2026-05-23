# Build Engine — Production-Readiness Analysis & Implementation Plan

> **Status:** Analysis only — no code changes performed.
> **Scope:** Cross-checked against `~/work/Mincemeat/coreapp` (OpenAPI / backend
> routes), `~/work/Mincemeat/build-engine-images` (Dockerfiles, manifest
> contract, CI patterns), and the locally tracked design / contract docs.
> **Author:** Pre-launch hardening review.
> **Last Updated:** 2026-05-23.

This document captures every issue that should be addressed before the
build-engine is exposed to first production traffic, plus an automated
GitHub Actions release pipeline with attestation. Each stage groups related
work, each task is a checkbox with a time estimate and a complexity rating.

Complexity legend:

| Tag | Meaning |
|-----|---------|
| **S** | Small — local change, low risk, < 1 day |
| **M** | Medium — touches one subsystem, needs tests, 1–3 days |
| **L** | Large — multi-module or cross-repo, needs design alignment, 3–7 days |
| **XL** | Extra-large — security-critical or cross-repo coordination, > 1 week |

Effort estimates are calendar hours for one senior engineer with full context.

---

## Findings Summary

### 🔴 Critical (must fix before first production engine)

| # | Area | Issue | File(s) |
|---|------|-------|---------|
| C-1 | Executor / secrets | `job.assign` secrets are extracted only for **redaction**, never injected as env vars into the build container. Builds that need any secret (private deps, API tokens, signed installers) will silently fail or run with empty env. | [src/build_engine/agent/job_loop.py](src/build_engine/agent/job_loop.py#L256-L266), [src/build_engine/executor/docker_runner.py](src/build_engine/executor/docker_runner.py#L132-L168) |
| C-2 | Executor / network policy | `ensure_network_guard()` creates a stock Docker bridge **without** any block rules for `169.254.169.254`, cloud metadata ranges, host gateway, engine private networks, or platform private CIDRs. The design's "fail-closed" guarantee is not implemented. | [src/build_engine/executor/network.py](src/build_engine/executor/network.py) |
| C-3 | CLI / runtime | [src/build_engine/cli/doctor.py:410](src/build_engine/cli/doctor.py#L410) uses `except TypeError, ValueError:`. This is PEP 758 syntax legal only in Python ≥ 3.14, but it sits inside an `_clock_skew_seconds` helper that is exercised on every `doctor` run and on agent_health checks. Confirm the deployed PyInstaller binary actually targets ≥ 3.14 grammar (otherwise this raises `SyntaxError` at parse time on 3.13). | [src/build_engine/cli/doctor.py](src/build_engine/cli/doctor.py#L405-L414) |
| C-4 | Workspace lifecycle | `cleanup_workspace()` writes a `FAILED` marker to the *attempt* dir, then prunes by mtime of the **attempt directory** instead of the marker. The mtime of the directory is updated on every nested file write — pruning order is therefore close to insertion order, not failure-time order, and can keep stale workspaces while deleting newer ones. | [src/build_engine/executor/workspace.py](src/build_engine/executor/workspace.py#L138-L192) |
| C-5 | CI runner | ~~`.github/workflows/ci.yml` runs on `self-hosted`.~~ **Resolved by infra decision:** the project's self-hosted runner pool is a hard requirement (Docker-in-Docker for executor integration tests, network-guard nftables fixtures, and PyInstaller binary smoke against the production image baseline). The pool is provisioned on Ubuntu 24.04 LTS / amd64 — matching the supported engine host spec — so `runs-on: self-hosted` is the correct target. Action item demoted: document the runner pool in `docs/build-engine-operations.md` and pin runner labels (e.g. `[self-hosted, linux, x64, ubuntu-24.04]`) so unintended runners can never match the workflow. | [.github/workflows/ci.yml](.github/workflows/ci.yml) |
| C-6 | Release pipeline | No GitHub Actions workflow publishes the PyInstaller binary, SBOMs, signatures, or build provenance. The only release path is `scripts/release-artifacts.sh` on an operator workstation — no attestation, no SLSA provenance, no checksums hosted with the release. | (missing) |

### 🟠 High (block GA but not first canary)

| # | Area | Issue |
|---|------|-------|
| H-1 | Contracts | `EngineConfig.image_manifest_version` defaults to `"1.0.0"` but the actually shipped `build-engine-images/manifest.json` is at `0.1.0-dev`. Registration will currently advertise an unreleased manifest version. |
| H-2 | Contracts | `EngineConfig.images` defaults include `node:20`, which the design and adjacent build-engine-images repository have already replaced as the GA matrix (preferred: `node:22`, `bun:1`, `hugo:latest`, `zola:latest`). |
| H-3 | Executor | `docker_runner.docker_run_args` runs `sh -lc <command>`. The `-l` flag triggers login-shell init files inside the container — none of the curated images define `~/.profile`, but the flag is unnecessary and prevents predictable PATH handling. |
| H-4 | Executor | `image_pull` in `doctor` always pulls `config.images[0]` which is `node:20`. On clean hosts this pulls ~1 GiB before any work happens and races against the network guard check. The doctor should pull from the *manifest* (cheapest tag), or accept a `--image` override. |
| H-5 | Executor | `prune_cache` is called **before** `prepare_site_cache`, so a fresh install always sees the site cache as MISS. The CACHE event the publisher reports is correct, but the metric ordering means cold first-builds never get a HIT. |
| H-6 | Queue | `SQLiteEventOutbox.append` calls `self.store.initialize()` on **every** event publish. `initialize` opens a new connection and runs `PRAGMA user_version` — under load that is a per-event SQLite open. Move initialization to the constructor. |
| H-7 | Queue | `SQLiteEventOutbox._lock` is an `asyncio.Lock` but the actual SQLite work runs synchronously inside that lock from the event-loop thread. Heavy events block all other coroutines. Wrap in `asyncio.to_thread`. |
| H-8 | Protocol | `EventSpool` (JSONL) in `agent/uplink.py` is dead code — `BuildEngineUplink` always defaults to `EventSpool(...)` when no spool is passed, but production wiring in `commands._serve` always provides `SQLiteEventOutbox`. Either remove or document the JSONL fallback as test-only. |
| H-9 | Auth | Credentials JSON in `write_credentials` is built by hand with `json.dumps` to render TOML strings — works, but `tomli_w` (or a tiny custom TOML emitter) would be safer. |
| H-10 | Auth | `refresh_session_if_needed` evaluates `expires_at - aware_utcnow() > refresh_window` only when `credentials.session_jwt` is set. If the JWT field is empty, it forces a refresh — but `expires_at` is still parsed first and will raise on an empty string. Initial-registration flows should not depend on that ordering. |
| H-11 | Heartbeat HTTP | Coreapp exposes `/api/v1/build-engines/agent/heartbeats` (HTTP) as a fallback alongside WSS heartbeats, but the engine never posts to it. Decide: deprecate the HTTP endpoint in coreapp, or wire the engine to fall back when WSS is unhealthy. |
| H-12 | Metrics | `run_metrics_reporter` `suppress(MetricsReportError)` swallows every push failure with zero logging. Repeated outages would be invisible to operators. |

### 🟡 Medium (quality, correctness, polish)

| # | Area | Issue |
|---|------|-------|
| M-1 | Config | `_path_value` is called twice in `load_config`. The env-coercion path already returns `Path` for `_path` / `_dir` keys. |
| M-2 | Config | `EngineConfig` fields and `_config_defaults()` duplicate every default — drift risk. Build the defaults dict from `EngineDefaults` + per-field annotations using `dataclasses.fields`. |
| M-3 | Config | `proto_version` and `image_manifest_version` are stored in `EngineConfig` but constants like `PROTOCOL_VERSION` already live in `agent/protocol.py`. The config field is redundant and the doctor check `_check_version` exists only because of this redundancy. |
| M-4 | CLI | `_drain` is a stub that prints `"drain scaffold ready"` — the design promises it sets local drain mode. Either implement (flip `SQLiteCommandHandlers.draining`) or remove the command from the CLI. |
| M-5 | CLI | `main.py`, `__main__.py`, and `cli/commands.main` form a 3-hop indirection. Collapse to one entrypoint. |
| M-6 | Detection | `_node_clause_satisfies` re-implements a tiny semver subset. For Node "engines" ranges containing `||`, hyphen, pre-release, etc., it silently mis-matches. Use [`packaging.specifiers.SpecifierSet`] (stdlib via `packaging` dep) or vendor a documented subset. |
| M-7 | Detection | `_script_matches_profile` uses substring matching against the framework's `default_command` first word. Frameworks with overloaded scripts (`build:storybook`, `build:docs`) can mis-route. |
| M-8 | Detection | `FRAMEWORK_PROFILES` is missing `zola`, `angular-static`, `remix-spa` listed in the design's V1 GA matrix and present in `build-engine-images/manifest.json`. |
| M-9 | Uplink | `InMemoryCommandHandlers` is a production class but only used by tests. Move to `tests/_fakes.py`. |
| M-10 | Uplink | `BuildEngineUplink.connect_once` constructs a heartbeat task **after** replay. Long replays delay the first heartbeat by more than the heartbeat interval and risk a backend timeout. |
| M-11 | Uplink | `welcome.payload` `heartbeat_interval_seconds` is honored on first connect but not persisted across reconnects — every reconnect resets to the configured default. |
| M-12 | Executor | `package_output` opens the destination once but discards the `mtime=0` GzipFile compression level (defaults to 9). Add `compresslevel=6` to halve packaging time on large outputs. |
| M-13 | Executor | `validate_output_dir` walks symlinks but does not `resolve` correctly when the symlink chain leaves and re-enters output_dir — guard rejects safe relative symlinks. |
| M-14 | Executor | `stream.py _frame_chunks` shrinks `limit` one byte at a time when the chunk crosses a UTF-8 boundary. Use `encode` + truncate-at-boundary helper. |
| M-15 | Executor | `pull_image` has no progress reporting — long pulls look hung over WSS. Stream output. |
| M-16 | Cache | `touch_site_cache` only touches the metadata file. `_last_access` falls back to `site_root` mtime when metadata is missing; the inconsistency can keep dead caches alive past TTL. |
| M-17 | Cache | `prune_cache` walks every site root twice and then re-walks for size — O(N) over file tree per prune. Cache size on hot path. |
| M-18 | Queue | `SQLiteQueueStore._connect` re-opens a connection for every call. Use a single connection per store instance (SQLite is single-writer anyway). |
| M-19 | Queue | `record_executor_crash` increments `attempts` from the **events** count, but `MAX_LOCAL_CRASHES` is checked against the *jobs* attempts column already incremented by `acquire_lease`. Off-by-one: third crash is the second DLQ candidate. |
| M-20 | Tests | `test_hardening.py` patches `pull_image` via monkeypatch and uses a fake docker subprocess. The "end-to-end" tests do not actually exercise the Docker integration. Naming should reflect "fixture smoke", not "end-to-end". |
| M-21 | Tests | No `conftest.py`; fixtures (such as the corev tmp_path patterns) are duplicated across files. |
| M-22 | Tests | No coverage tooling. `make test` should optionally produce `coverage.xml`. |
| M-23 | Tests | `tests/fixtures/sites/*` contain `package.json` snippets but no real lockfiles for most frameworks — package-manager detection in tests therefore exercises only the `fallback`/`packageManager` paths. |
| M-24 | Tests | No fuzz or property tests for `decode_frame` / `validate_envelope`. |
| M-25 | Packaging | `docs/build-engine-design.md` advertises a `packaging/deb/` directory that does not exist. Either build a debian package or remove the claim. |
| M-26 | Packaging | The PyInstaller spec lists `aiosqlite`, `httpx`, `pydantic`, `pydantic_settings`, `structlog` as optional hidden imports. None are dependencies — the spec is aspirational. Trim to actual imports to shrink the bundle. |
| M-27 | Packaging | `release-artifacts.sh` writes `SHA256SUMS` with only the renamed artifact. No `.sig`, `.asc`, or SBOM is hashed. |
| M-28 | Hooks | `.pre-commit-config.yaml` and `scripts/install-git-hooks.sh` install two parallel hook systems. Pick one canonical path. |

### 🟢 Low / Nice-to-have

| # | Area | Issue |
|---|------|-------|
| L-1 | Docs | `README.md` declares "Stage 3 completed" — drop stage language from user-facing docs after GA. |
| L-2 | Docs | `docs/README.md` last-updated date is `2026-05-21` — add a CHANGELOG so reviewers can see what shifted. |
| L-3 | Docs | No `CONTRIBUTING.md` despite repo conventions enforced by `AGENTS.md`. |
| L-4 | Docs | `docs/build-engine-design.md` repeats the host spec already in `docs/build-engine-operations.md`. Pick one source of truth. |
| L-5 | Lint | `pyproject.toml` enables `ANN` everywhere; tests get redundant return-type annotations on every test function. Add a `tests/` override that exempts `ANN` and `D` rules. |
| L-6 | Lint | `pyproject.toml` does not configure ruff `per-file-ignores` for `__init__.py` (`F401`). |
| L-7 | Build | `pyproject.toml` `version` is hard-coded; `__init__.py.__version__` is also hard-coded. Move both to `__about__.py` or read from `importlib.metadata`. |
| L-8 | Build | `make verify` runs `contracts-sync` which writes to a tracked file. In CI this can produce noisy diffs. Add a `--check` mode that only validates. |
| L-9 | Build | `make verify` rebuilds the PyInstaller binary every time — slow. Cache `dist/` between local runs. |

---

## Stage 1 — Critical Correctness & Security Fixes

> **Goal:** Close every issue that would manifest as a customer-visible
> failure or weaken the documented security posture.
> **Estimated total:** ~6 days.

### 1.1 Wire build secrets into container env

- [x] **S · 3h** Add `secret_env` mapping to `DockerRunSpec` and pass each
  `KEY=VALUE` through `--env-file` (not `--env`, to keep secrets off the host
  process list).
- [x] **S · 1h** Update `job_loop._secret_values` to also return the key/value
  pairs (currently only values for the redactor).
- [x] **M · 4h** Add a `tests/test_executor.py` case that asserts a known env
  var reaches the container and is redacted from the log stream.
- [x] **S · 1h** Document the secrets contract (`payload.secrets: {KEY: VALUE}`)
  in `docs/build-engine-design.md` § Build Secrets.

### 1.2 Implement the network guard contract

- [x] **L · 1d** Replace the bare bridge in `network.py` with an iptables /
  nftables egress chain attached to the bridge that drops:
  `169.254.0.0/16`, RFC1918 CIDRs, Docker gateway, and a configurable
  block-list passed from coreapp.
- [x] **M · 4h** Add a `network_blocklist` config setting + CLI flag so
  operators can extend the deny set without a rebuild.
- [x] **M · 4h** "Fail closed": if `ensure_network_guard()` cannot install
  the chain, abort job execution and emit `error_class=EXEC_INFRA,
  error_code=NETWORK_GUARD`.
- [x] **M · 4h** Add integration test that launches a throwaway container
  and asserts `curl 169.254.169.254` returns non-zero.
- [x] **S · 2h** Document the implementation in
  `docs/build-engine-design.md` § Network Policy.

### 1.3 Verify 3.14 grammar everywhere & fix doctor bug

- [x] **S · 1h** Run `uv run python -m compileall -j0 src` in the actual
  PyInstaller build container and confirm zero `SyntaxError`s on PEP 758.
- [x] **S · 30m** Add a unit test for `_clock_skew_seconds` with both a valid
  RFC date header and a malformed one to exercise both `TypeError` and
  `ValueError` branches.

### 1.4 Fix failed-workspace pruning by timestamp source

- [x] **S · 2h** Replace `path.stat().st_mtime` with the marker file's mtime,
  or record a sidecar JSON `{failed_at}` and sort by that.
- [x] **S · 1h** Add a regression test in `tests/test_executor.py` that
  retains the *newest* failures and prunes the oldest.

### 1.5 Harden self-hosted CI runner pinning

> **Context update:** the self-hosted runner pool is required (DinD for
> executor integration tests, nftables for the network guard, baseline
> PyInstaller smoke). The pool already runs Ubuntu 24.04 LTS / amd64,
> matching the supported engine host spec. The remaining work is to make
> the pinning explicit and the pool documented — not to move off it.

- [x] **S · 1h** Replace bare `runs-on: self-hosted` with a labelled
  selector such as `runs-on: [self-hosted, linux, x64, ubuntu-24.04]` in
  `.github/workflows/ci.yml` so only the sanctioned pool can match.
- [x] **S · 1h** Add a matrix entry that also installs the resulting binary
  via `scripts/install-build-engine.sh` with `DESTDIR=$(mktemp -d)` to
  exercise the installer.
- [x] **S · 1h** Document the self-hosted runner pool (hosts, labels,
  hardening expectations, image baseline) in
  `docs/build-engine-operations.md` § CI Infrastructure.

---

## Stage 2 — Contracts & Config Consistency

> **Goal:** Make every contract version, image identifier, and protocol
> constant authoritative in exactly one place.
> **Estimated total:** ~2 days.

- [x] **S · 1h** Pin `EngineConfig.image_manifest_version` default to the
  *actual* shipped manifest version (`0.1.0-dev` until image GA) and add a
  `make contracts-sync` step that errors when the version drifts. **[H-1]**
- [x] **S · 1h** Trim `EngineConfig.images` default to the V1 GA matrix
  (`node:22`, `bun:1`, `hugo:latest`, `zola:latest`) and update the README
  compatibility table. **[H-2]**
- [x] **S · 1h** Remove `proto_version` from `EngineConfig` — single-source
  it from `agent.protocol.PROTOCOL_VERSION` and delete `_check_version` or
  rewrite it to check the **manifest** instead. **[M-3]**
- [x] **S · 2h** Add `FRAMEWORK_PROFILES` entries for `zola`,
  `angular-static`, `remix-spa` (or mark explicitly "candidate, not GA in
  v1") and reconcile with the design's matrix. **[M-8]**
- [x] **S · 2h** Verify every contract subset in
  `contracts/openapi/build-engine.openapi.json` corresponds to an actually
  reachable coreapp route — `BuildEngineCacheResetResponse` is exported but
  the engine never POSTs cache-reset to coreapp; flag for removal in
  `sync_contracts.py`'s allow-list. **[design hygiene]**
- [x] **M · 4h** Add `tests/test_contracts.py` cases that assert:
  - the local `EngineConfig.image_manifest_version` matches
    `contracts/image-manifest/manifest.schema.json` `$id` version;
  - every framework in `FRAMEWORK_PROFILES` appears in at least one image
    in the build-engine-images manifest;
  - every coreapp route the engine actually calls is present in the OpenAPI
    subset.
- [x] **S · 1h** Decide on `/api/v1/build-engines/agent/heartbeats` HTTP
  fallback (use, deprecate, or remove). Capture decision in
  `docs/coreapp-design.md`. **[H-11]**

---

## Stage 3 — Code Quality, Refactoring, Code Smells

> **Goal:** Reduce duplication, dead code, and risky patterns.
> **Estimated total:** ~3 days.

### 3.1 Dead code & duplication

- [x] **S · 1h** Delete `EventSpool` (JSONL) from `agent/uplink.py` or move
  to `tests/_fakes.py`. **[H-8]**
- [x] **S · 1h** Move `InMemoryCommandHandlers` to `tests/_fakes.py`. **[M-9]**
- [x] **S · 30m** Collapse `__main__.py` → `main.py` → `cli/commands.main`
  to a single entrypoint. **[M-5]**
- [x] **S · 1h** Remove the duplicate `_path_value` invocation in
  `load_config`. **[M-1]**
- [x] **S · 2h** Replace `_config_defaults` hand-written dict with
  introspection on `EngineDefaults` + `EngineConfig` field types. **[M-2]**
- [x] **S · 1h** Drop `EngineConfig.os` / `EngineConfig.arch` defaults
  unless they will be overridden; otherwise read from `platform`.

### 3.2 Runtime correctness

- [x] **S · 30m** Drop `-l` flag from `sh -lc` in
  `docker_runner.docker_run_args`. **[H-3]**
- [x] **M · 3h** Move `SQLiteQueueStore.initialize()` to construction; make
  `SQLiteEventOutbox` cache a connection. **[H-6][M-18]**
- [x] **M · 3h** Wrap synchronous SQLite operations in `asyncio.to_thread`
  inside `SQLiteEventOutbox`. **[H-7]**
- [x] **S · 2h** Persist `welcome.heartbeat_interval_seconds` between
  reconnects in `BuildEngineUplink`. **[M-11]**
- [x] **S · 2h** Start the heartbeat task **before** replay so backends do
  not time out the agent during long replays. **[M-10]**
- [x] **S · 2h** Reorder cache prune / prepare so MISS↔HIT events reflect
  reality. **[H-5]**
- [x] **S · 1h** Add logging around `run_metrics_reporter`'s suppressed
  errors; rate-limit warnings to once per minute. **[H-12]**
- [x] **S · 2h** Fix DLQ off-by-one in `record_executor_crash`. **[M-19]**
- [x] **S · 2h** Implement `cli._drain` against `SQLiteCommandHandlers`
  (flip `draining`, persist via a JSON marker file). **[M-4]**

### 3.3 Detection accuracy

- [x] **M · 4h** Replace `_node_clause_satisfies` with `packaging.specifiers`
  (added as a dependency). **[M-6]**
- [x] **S · 2h** Tighten `_script_matches_profile` to require the framework
  command **as a token**, not a substring. **[M-7]**
- [x] **S · 1h** Add fixture sites for `astro`, `next-export`, and
  `sveltekit-static` with **multiple** build-like scripts to cover the
  matcher edge cases.

### 3.4 Linter / type-checker tightening

- [x] **S · 1h** Add `per-file-ignores` for `tests/*` (`ANN`, `D101`-`D107`)
  and `__init__.py` (`F401`). **[L-5][L-6]**
- [x] **S · 1h** Source `__version__` from `importlib.metadata` so
  `pyproject.toml` is the single source of truth. **[L-7]**

---

## Stage 4 — Testing Apparatus

> **Goal:** Get to repeatable, high-signal CI with clear coverage and a real
> integration tier.
> **Estimated total:** ~4 days.

### 4.1 Coverage and structure

- [x] **S · 2h** Add `coverage[toml]` dev dep, `pytest-cov` plugin, and
  `make coverage` producing `coverage.xml` and an HTML report.
- [x] **S · 1h** Add `tests/conftest.py` with shared fixtures (tmp state
  dir, engine config builder, fake credentials).
- [x] **S · 1h** Rename `test_hardening.py` ↔ `test_v1_ga_fixture_smoke.py`
  (more accurate). **[M-20]**
- [x] **S · 2h** Generate real lockfiles for the V1 GA fixtures via
  `tests/fixtures/sites/_regenerate.sh`. **[M-23]**

### 4.2 New test categories

- [x] **M · 4h** Add a fuzz / property suite for `validate_envelope` and
  `decode_frame` (Hypothesis) — payloads, types, sizes, sequence bounds.
- [x] **M · 4h** Add a contract-pin test that diffs
  `contracts/openapi/build-engine.openapi.json` against a known-good SHA
  and instructs the operator to re-run `make contracts-sync`.
- [x] **M · 6h** Add a `tests/integration/test_docker.py` (opt-in via
  `BUILD_ENGINE_DOCKER_TESTS=1`) that runs a real `docker run` against the
  GA `hugo:latest` image fixture and validates packaged output.
- [x] **M · 4h** Add a `tests/integration/test_uplink.py` that spins up an
  in-process `websockets.serve` and drives the full reconnect / replay
  state machine.
- [x] **S · 2h** Add `mypy --strict` parallel-to-`ty` gate on a CI label so
  the two checkers do not silently disagree.
- [x] **S · 2h** Add `bandit -r src/` to CI for security lint.

### 4.3 CI feedback quality

- [x] **S · 2h** Make `pytest` emit JUnit XML and upload as a workflow
  artifact so flakiness can be tracked.
- [x] **S · 2h** Add `make ubuntu-24-smoke` to CI as a matrix entry that
  runs inside an `ubuntu:24.04` container so `install-build-engine.sh` is
  exercised by every PR.

---

## Stage 5 — Documentation Refresh

> **Goal:** Eliminate drift; keep operators and contributors aligned with
> reality.
> **Estimated total:** ~1.5 days.

- [x] **S · 2h** Drop "Stage N completed" language from `README.md` and
  `docs/README.md`; replace with a status badge. **[L-1]**
- [x] **S · 1h** Add `CHANGELOG.md` (keep-a-changelog format) and an
  enforcement test that fails if `pyproject.toml` version bumps without a
  new entry. **[L-2]**
- [x] **S · 2h** Add `CONTRIBUTING.md` covering: `make verify` gate, hook
  install, AGENTS.md highlights, release process, and how to refresh
  contracts. **[L-3]**
- [x] **S · 1h** Delete the duplicate "Host Spec" section from
  `docs/build-engine-design.md` and link to
  `docs/build-engine-operations.md`. **[L-4]**
- [x] **S · 1h** Remove the `packaging/deb/` reference from
  `docs/build-engine-design.md` (or add the directory & build target).
  **[M-25]**
- [x] **S · 1h** Document the build-secret env contract in
  `docs/build-engine-design.md` (alongside the Stage 1.1 code fix).
- [x] **S · 2h** Add `docs/build-engine-release.md` describing the new
  GitHub Actions release pipeline (Stage 7).
- [x] **S · 1h** Cross-link `docs/build-engine-images-design.md` with the
  build-engine-images repo so reviewers know the source of truth.

---

## Stage 6 — Operational Hardening

> **Goal:** Make a freshly registered engine survive realistic failure
> modes.
> **Estimated total:** ~2 days.

- [x] **S · 2h** Validate `credentials.toml` ownership (uid/gid) — currently
  only mode is validated. Reject if owner ≠ service user.
- [x] **S · 2h** Add structured (JSON) logging via stdlib `logging` +
  `RichHandler`/`JsonFormatter`. All `print(...)` calls in CLI / serve
  paths should funnel through it.
- [x] **S · 2h** Add `systemd-notify` (`Type=notify` already declared in
  AGENTS.md, but service file uses `Type=simple`). Either implement
  `sd_notify` or change the unit to `Type=simple` consistently. The
  service file at `packaging/systemd/build-engine.service` currently does
  the latter — confirm and document.
- [x] **S · 2h** Add startup self-test that runs the equivalent of
  `doctor --json --skip=image_pull,wss_handshake` before opening the WSS
  uplink; refuse to start on `fail`.
- [x] **M · 4h** Implement graceful drain: on `SIGTERM`, set
  `SQLiteCommandHandlers.draining=True`, finish running attempts, close
  uplink with code `1001` and reason `engine_drain`.
- [x] **S · 2h** Add `--state-dir` and `--no-network-guard` flags to
  `serve` so operators can run a confined dev engine without sudo.
- [x] **S · 2h** Validate `engine_secret` length / charset in
  `validate_credentials_file` (length ≥ 32 bytes, ASCII).
- [x] **S · 2h** Add Prometheus-compatible textfile metrics writer under
  `/var/lib/build-engine/metrics.prom` for node-exporter scraping.

---

## Stage 7 — Automated GitHub Actions Build, Sign, Attest, Publish

> **Goal:** Produce a reproducible, signed, attested PyInstaller binary on
> every tagged release with SBOM and SLSA L3 provenance — matching the
> conventions already used by `build-engine-images`.
> **Estimated total:** ~4 days.

### 7.1 Workflow scaffolding

- [x] **S · 2h** Create `.github/workflows/release.yml` triggered on
  `push: tags: ['v*.*.*']` and `workflow_dispatch`.
- [x] **S · 1h** Add `permissions: contents: write, id-token: write,
  attestations: write, packages: read` (write only when publishing).
- [x] **S · 1h** Add `concurrency` group keyed on the tag to prevent
  duplicate runs.

### 7.2 Build matrix

- [x] **M · 4h** Build job on the sanctioned self-hosted pool
  (`runs-on: [self-hosted, linux, x64, ubuntu-24.04]`, which matches the
  supported engine host spec) that:
  - runs `make verify` (lint, type-check, tests, contracts-sync);
  - builds `dist/build-engine` via the PyInstaller spec;
  - renames to `build-engine-<version>-linux-amd64`;
  - emits `dist/SHA256SUMS`;
  - uploads as a workflow artifact.
- [x] **S · 2h** Pin every `actions/*` and third-party action by **commit
  SHA** with a version comment, matching the
  build-engine-images convention.
- [x] **S · 2h** Add a separate `linux-arm64` matrix entry behind a
  `if: vars.ENABLE_ARM64 == 'true'` gate (out of v1 scope but plumbing
  ready).

### 7.3 SBOM & vulnerability scan

- [x] **M · 4h** Generate CycloneDX SBOM with `anchore/sbom-action` against
  the source tree.
- [x] **M · 3h** Run `trivy fs --severity HIGH,CRITICAL --exit-code 1
  --skip-dirs .venv .` and upload SARIF.
- [x] **S · 2h** Run `bandit -r src/` and `pip-audit` against `uv.lock`.

### 7.4 Sign & attest

- [x] **M · 4h** `cosign sign-blob --yes --output-signature
  build-engine-<version>-linux-amd64.sig` using OIDC keyless against
  Sigstore.
- [x] **M · 4h** `actions/attest-build-provenance@v3` over the binary and
  SBOM (SLSA v1 provenance).
- [x] **S · 2h** `actions/attest-sbom@v3` for the CycloneDX SBOM.
- [x] **S · 2h** Optional GPG detached sign when
  `secrets.GPG_SIGNING_KEY` is set, gated by `if: env.GPG_SIGNING_KEY`.

### 7.5 Publish

- [x] **M · 3h** `softprops/action-gh-release@v2` step that uploads:
  - the binary
  - `SHA256SUMS`
  - `*.sig` and `*.pem` from cosign
  - the CycloneDX SBOM
  - `*.intoto.jsonl` attestations.
- [x] **S · 2h** On `push: tags`, also tag `build-engine-<version>` on the
  build-engine-images registry namespace so engine ↔ image version
  alignment is visible.
- [x] **S · 1h** Add a draft step that auto-populates release notes from
  `CHANGELOG.md` between tags.

### 7.6 Build hygiene

- [x] **S · 2h** `actions/setup-python` is replaced by `astral-sh/setup-uv`
  with `enable-cache: true` and `cache-dependency-glob: uv.lock`.
- [x] **S · 1h** Verify the workflow runs on a clean checkout — no host
  state leaks into `dist/`.
- [x] **S · 2h** `dependency-review-action` on PRs to flag new direct
  dependencies.
- [x] **S · 1h** `actionlint` workflow mirroring `build-engine-images`'s
  pattern.
- [x] **S · 2h** Verify binary is **reproducible**: rerun build job and
  compare SHA256. Document any sources of nondeterminism (PyInstaller
  timestamps, `__pycache__` ordering) in `docs/build-engine-release.md`.

### 7.7 Verifier UX

- [x] **S · 2h** Add `scripts/verify-release.sh` for downstream consumers:
  - downloads binary + signature + SBOM;
  - runs `cosign verify-blob` with the public Fulcio bundle;
  - runs `slsa-verifier verify-artifact` against the provenance attestation;
  - prints PASS/FAIL.
- [x] **S · 1h** Document the verification path in
  `docs/build-engine-operations.md` under a new "Verifying the release"
  section.

---

## Stage 8 — Release Process, Packaging, Versioning

> **Goal:** Tag → build → ship in one operation.
> **Estimated total:** ~1.5 days.

- [x] **S · 2h** Single-source version: read `__version__` via
  `importlib.metadata`. Delete the hard-coded literal in
  `src/build_engine/__init__.py`. **[L-7]**
- [x] **S · 2h** Add `make release VERSION=x.y.z` that bumps
  `pyproject.toml`, appends a `CHANGELOG.md` heading, commits, tags, and
  pushes (with `git push --follow-tags`).
- [x] **S · 2h** Trim `optional_hiddenimports` in the PyInstaller spec to
  imports the package actually uses. **[M-26]**
- [x] **S · 2h** Extend `release-artifacts.sh` to include `.sig`, `.asc`,
  and SBOM hashes in `SHA256SUMS`. **[M-27]**
- [x] **S · 2h** Convert `scripts/install-build-engine.sh` into a `.deb`
  build under `packaging/deb/` (or remove the design promise). **[M-25]**
- [x] **S · 1h** Reconcile `.pre-commit-config.yaml` and `.githooks/` —
  keep one. Recommend: `.githooks/` (already wired into `make
  hooks-install`); make `.pre-commit-config.yaml` opt-in for upstream
  bots only. **[M-28]**

---

## Suggested Execution Order

```diagram
╭──────────────────╮
│ Stage 1: Critical │  ◀── must complete before first canary
╰────────┬──────────╯
         ▼
╭──────────────────────────╮
│ Stage 2: Contracts/Config │
╰────────┬──────────────────╯
         ▼
╭───────────────────────────────╮
│ Stage 4: Testing Apparatus     │  ◀── unlocks confident refactor
╰────────┬──────────────────────╯
         ▼
╭─────────────────────────────────╮
│ Stage 3: Code Quality/Refactor   │
╰────────┬────────────────────────╯
         ▼
╭─────────────────────────────────╮
│ Stage 6: Operational Hardening   │
╰────────┬────────────────────────╯
         ▼
╭─────────────────────────────────╮
│ Stage 7: GitHub Actions Release  │ ◀── parallelizable with 6
╰────────┬────────────────────────╯
         ▼
╭─────────────────────────────────╮
│ Stage 8: Release/Packaging       │
╰────────┬────────────────────────╯
         ▼
╭─────────────────────────────────╮
│ Stage 5: Documentation Refresh   │ ◀── continuously throughout
╰─────────────────────────────────╯
```

Total effort estimate (single senior engineer, sequential):
**~23 working days (≈ 4.5 weeks)** with Stages 6 and 7 parallelizable by
roughly 30%, bringing realistic delivery to **~3.5 weeks** with two
engineers.

---

## Acceptance Checklist — "Ready for production"

A signed release is ready for the first production engine when **all of
the following are true**:

- [ ] Stage 1 items C-1 through C-6 are merged and verified by tests.
- [ ] `make verify` is green on the sanctioned self-hosted runner pool
      (`[self-hosted, linux, x64, ubuntu-24.04]`), with labels pinned in
      every workflow so unintended runners cannot match.
- [ ] `release.yml` produces a binary, SBOM, SHA256SUMS, cosign signature,
      and SLSA provenance for the tagged version.
- [ ] `scripts/verify-release.sh` returns PASS on a fresh download.
- [ ] `doctor --json` returns `status: ok` on a freshly installed Ubuntu
      24.04 host with no manual tweaks.
- [ ] One end-to-end smoke build (Hugo or Astro fixture) succeeds against
      a staging coreapp deployment with a real registration token.
- [ ] `CHANGELOG.md` and `docs/build-engine-release.md` are current.

---

## Appendix A — Cross-Repo Contract Pins

The following contract surfaces must move in lockstep across all three
repos. Any change to one requires a coordinated PR in the others.

| Surface | Source of truth | Consumers |
|---------|----------------|-----------|
| WSS protocol v1 envelope | `build-engine/contracts/protocol/wss-v1.json` | coreapp WSS handler, engine |
| Builder image manifest schema | `build-engine/contracts/image-manifest/manifest.schema.json` | build-engine-images CI, engine |
| Builder image manifest content | `build-engine-images/manifest.json` | engine `image_manifest_version` field |
| OpenAPI subset | `build-engine/contracts/openapi/build-engine.openapi.json` (generated) | engine HTTP client, contract tests |
| Framework GA matrix | `docs/build-engine-design.md` § Framework Detection | engine `FRAMEWORK_PROFILES`, build-engine-images README |

Adopt a small `scripts/check-contract-pins.py` (Stage 4.2) that fails CI
when any of these drift.

---

## Appendix B — Files Touched per Stage (estimate)

| Stage | New files | Modified files |
|-------|-----------|----------------|
| 1 | `tests/test_network_guard.py` | `executor/network.py`, `executor/docker_runner.py`, `agent/job_loop.py`, `cli/doctor.py`, `executor/workspace.py`, `.github/workflows/ci.yml` |
| 2 | `tests/test_contract_drift.py` | `config.py`, `detect/framework.py`, `cli/doctor.py`, `scripts/sync_contracts.py` |
| 3 | `tests/_fakes.py` | `agent/uplink.py`, `agent/job_loop.py`, `cli/commands.py`, `cli/__init__.py`, `config.py`, `executor/docker_runner.py`, `executor/cache.py`, `queue/store.py`, `queue/dlq.py`, `detect/framework.py`, `metrics/reporter.py` |
| 4 | `tests/conftest.py`, `tests/integration/*`, `tests/test_envelope_fuzz.py`, `tests/fixtures/sites/_regenerate.sh` | `pyproject.toml`, `Makefile`, `.github/workflows/ci.yml` |
| 5 | `CHANGELOG.md`, `CONTRIBUTING.md`, `docs/build-engine-release.md` | `README.md`, `docs/README.md`, `docs/build-engine-design.md`, `docs/build-engine-operations.md` |
| 6 | `src/build_engine/logging_config.py` | `agent/auth.py`, `cli/commands.py`, `packaging/systemd/build-engine.service` |
| 7 | `.github/workflows/release.yml`, `.github/workflows/actionlint.yml`, `scripts/verify-release.sh` | (none in src) |
| 8 | `packaging/deb/*` (optional) | `src/build_engine/__init__.py`, `packaging/pyinstaller/build-engine.spec`, `scripts/release-artifacts.sh`, `Makefile` |

# Contributing

Thanks for helping keep the build engine boring in production. This repository
ships an agent that runs on customer-facing infrastructure, so every change
needs a clean local verification path and an explicit contract story.

Contributions are accepted under the project's [AGPL-3.0-or-later](LICENSE)
license. By submitting a patch you agree to license it under the same terms.

## Verification Gate

Run the full deployment gate before you declare a code change complete:

```bash
make verify
```

`make verify` refreshes the control-plane OpenAPI subset, compiles
source/tests, runs Ruff, ty, Bandit, pytest, and the PyInstaller binary
smoke. During iteration, narrower targets are fine:

```bash
make lint
make typecheck
make test
make binary-smoke
```

Documentation-only changes should still run the focused tests that cover the
files you touched. Changes to release docs, package metadata, or scripts
should at least run `uv run pytest tests/test_packaging_ops.py
tests/test_changelog.py`.

## Local Hooks

Install repo-tracked hooks once per checkout:

```bash
make hooks-install
```

The pre-commit hook runs Ruff lint/format checks on staged Python files. The
pre-push hook runs the same full `make verify` gate used by CI. Emergency
bypasses exist, but do not bypass hooks on `main`, release branches, or tags.

## Agent Notes

Read `AGENTS.md` before making changes. The highlights are:

- Keep work scoped to the implementation plan and existing module boundaries.
- Prefer structured parsers for TOML, JSON, and contract files.
- Preserve Python 3.14 grammar, including PEP 695 aliases/generics and PEP 758
  unparenthesized multi-except syntax.
- Do not revert unrelated user or agent changes in a dirty worktree.
- Update documentation and tests together when behavior or operator workflow
  changes.

## Contract Refresh

The adjacent control-plane checkout is expected at `../coreapp`. After it
regenerates its OpenAPI output, refresh the engine snapshot:

```bash
make contracts-sync
make verify
```

The sync script extracts only the build-engine agent routes into
`contracts/openapi/build-engine.openapi.json`. Protocol message names live in
`contracts/protocol/wss-v1.json`, the builder-image manifest schema lives in
`contracts/image-manifest/manifest.schema.json`, and the pinned published
builder-image manifest snapshot lives at repo-root `manifest.json`.

When the builder-image manifest changes, update repo-root `manifest.json`,
update the engine's accepted manifest version, and run the contract tests. If
you are validating against a published manifest asset, set
`BUILD_ENGINE_IMAGES_MANIFEST_URL` while running `make contracts-sync`.

## Release Process

Release mechanics are documented in [`docs/release.md`](docs/release.md).
In short:

1. Run `make verify` on a clean checkout.
2. Run `make release VERSION=X.Y.Z` to bump metadata, add the changelog
   heading, commit, tag, and push with `git push --follow-tags`.
3. Let the GitHub Actions release workflow build, verify, sign, attest, and
   upload the Linux binary and Debian package artifact set.
4. Verify the published artifact with `scripts/verify-release.sh` once the
   downstream helper lands.

The test suite fails when the project version is missing from the changelog,
so metadata bumps and release notes stay paired.

## Reporting Security Issues

Do not file security issues in the public tracker. Use GitHub private
vulnerability reporting or the maintainer contact path in
[`SECURITY.md`](SECURITY.md).

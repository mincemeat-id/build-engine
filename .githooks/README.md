# Git Hooks

These version-controlled git hooks run the same checks earlier and locally, so
lint, type-check, test, or binary smoke failures are caught before they reach CI.

## Install

From the repo root:

```bash
make hooks-install
# or
bash scripts/install-git-hooks.sh
```

The installer points git at this directory:

```bash
git config core.hooksPath .githooks
```

`.pre-commit-config.yaml` is kept only for upstream bots that already know how
to run `pre-commit`; `.githooks/` is the canonical local hook path.

To remove:

```bash
make hooks-uninstall
# or
git config --unset core.hooksPath
```

## What Runs

| Hook | Speed | Checks |
|------|-------|--------|
| `pre-commit` | fast | Ruff lint + Ruff format check on staged Python files |
| `pre-push` | medium | Full `make verify`, matching CI |

## Bypass

If you genuinely need to skip hooks:

```bash
BYPASS_HOOKS=1 git commit ...
BYPASS_HOOKS=1 git push ...
git commit --no-verify ...
git push --no-verify ...
```

The tracked `pre-push` hook rejects `BYPASS_HOOKS=1` on protected refs:
`master`, `main`, `release/*`, and tags.

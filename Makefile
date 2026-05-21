.PHONY: verify lint fmt typecheck test binary-smoke contracts-sync deploy-check hooks-install hooks-uninstall

verify: contracts-sync
	uv run python -m compileall src tests
	uv run ruff check .
	uv run ruff format --check .
	uv run ty check
	uv run pytest
	$(MAKE) binary-smoke

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff format .
	uv run ruff check . --fix

typecheck:
	uv run ty check

test:
	uv run pytest

binary-smoke:
	uv run pyinstaller packaging/pyinstaller/build-engine.spec --noconfirm
	./dist/build-engine --version

contracts-sync:
	uv run python scripts/sync_contracts.py

deploy-check: verify

hooks-install:
	bash scripts/install-git-hooks.sh

hooks-uninstall:
	git config --unset core.hooksPath || true
	@echo "✓ core.hooksPath unset; default .git/hooks/ restored."

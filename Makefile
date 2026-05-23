.PHONY: verify lint fmt typecheck security test coverage binary-smoke release-artifacts ubuntu-24-smoke contracts-sync deploy-check hooks-install hooks-uninstall

verify: contracts-sync
	uv run python -m compileall src tests
	uv run ruff check .
	uv run ruff format --check .
	uv run ty check
	uv run bandit -r src/
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

security:
	uv run bandit -r src/

test:
	uv run pytest

coverage:
	uv run pytest --cov=build_engine --cov-report=xml:coverage.xml --cov-report=html

binary-smoke:
	uv run pyinstaller packaging/pyinstaller/build-engine.spec --noconfirm
	./dist/build-engine --version

release-artifacts: binary-smoke
	bash scripts/release-artifacts.sh

ubuntu-24-smoke:
	bash scripts/smoke-ubuntu-24.04.sh

contracts-sync:
	uv run python scripts/sync_contracts.py

deploy-check: verify

hooks-install:
	bash scripts/install-git-hooks.sh

hooks-uninstall:
	git config --unset core.hooksPath || true
	@echo "✓ core.hooksPath unset; default .git/hooks/ restored."

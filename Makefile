.PHONY: verify lint typecheck test binary-smoke contracts-sync

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

typecheck:
	uv run ty check

test:
	uv run pytest

binary-smoke:
	uv run pyinstaller packaging/pyinstaller/build-engine.spec --noconfirm
	./dist/build-engine --version

contracts-sync:
	uv run python scripts/sync_contracts.py


.PHONY: verify lint fmt typecheck security test coverage binary-smoke deb release-artifacts release ubuntu-24-smoke docker-integration contracts-sync deploy-check hooks-install hooks-uninstall

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

deb: binary-smoke
	bash packaging/deb/build-deb.sh

release-artifacts: binary-smoke
	bash scripts/release-artifacts.sh

release:
ifndef VERSION
	$(error VERSION is required, for example: make release VERSION=0.3.0)
endif
	bash scripts/prepare-release.sh "$(VERSION)"

ubuntu-24-smoke:
	bash scripts/smoke-ubuntu-24.04.sh

docker-integration:
	@test "$${BUILD_ENGINE_DOCKER_INTEGRATION:-}" = 1 || (echo "Set BUILD_ENGINE_DOCKER_INTEGRATION=1 to run the required Docker harness" >&2; exit 2)
	uv run pytest tests/integration/test_docker_v2_harness.py tests/integration/test_docker.py tests/integration/test_final_images.py -q

contracts-sync:
	uv run python scripts/sync_contracts.py

deploy-check: verify

hooks-install:
	bash scripts/install-git-hooks.sh

hooks-uninstall:
	git config --unset core.hooksPath || true
	@echo "✓ core.hooksPath unset; default .git/hooks/ restored."

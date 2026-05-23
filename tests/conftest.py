"""Shared pytest fixtures for build-engine tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from build_engine.config import EngineConfig, EngineCredentials, load_config


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """Return an isolated state directory for one test."""

    path = tmp_path / "state"
    path.mkdir()
    return path


@pytest.fixture
def engine_config_builder(
    state_dir: Path,
) -> Callable[..., EngineConfig]:
    """Build an EngineConfig with local-test defaults and caller overrides."""

    def build(**overrides: Any) -> EngineConfig:
        values: dict[str, object] = {
            "backend_url": "https://agent.example",
            "name": "test-engine",
            "state_dir": state_dir,
        }
        values.update(overrides)
        return load_config(
            config_path=state_dir / "missing.toml",
            credentials_path=state_dir / "credentials.toml",
            overrides=values,
        )

    return build


@pytest.fixture
def fake_credentials() -> EngineCredentials:
    """Return deterministic credentials suitable for local protocol tests."""

    return EngineCredentials(
        engine_id="11111111-1111-1111-1111-111111111111",
        engine_secret="test-engine-secret",
        session_jwt="session-token",
        session_jwt_expires_at="2030-01-01T00:00:00+00:00",
        backend_url="https://agent.example",
        name="test-engine",
    )

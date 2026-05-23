"""Registration and credential tests."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import override

import pytest
from pytest import MonkeyPatch

from build_engine.agent.auth import (
    MIN_ENGINE_SECRET_BYTES,
    AuthError,
    BuildEngineAuthClient,
    Session,
    refresh_session,
    register_engine,
    validate_credentials_file,
    validate_engine_secret,
    validate_secret_file_ownership,
)
from build_engine.config import EngineCredentials, load_config, load_credentials, write_credentials


class FakeRegistrationClient(BuildEngineAuthClient):
    """Fake auth client for registration tests."""

    def __init__(self) -> None:
        self.last_payload: dict[str, object] | None = None

    @override
    def register(
        self,
        *,
        registration_token: str,
        name: str,
        capabilities: dict[str, object],
    ) -> dict[str, object]:
        self.last_payload = {
            "registration_token": registration_token,
            "name": name,
            "capabilities": capabilities,
        }
        return {
            "engine_id": "11111111-1111-1111-1111-111111111111",
            "engine_secret": "x" * MIN_ENGINE_SECRET_BYTES,
            "session_jwt": "session-token",
            "session_jwt_expires_at": "2030-01-01T00:00:00+00:00",
        }


class FakeRefreshClient(BuildEngineAuthClient):
    """Fake auth client for session refresh tests."""

    def __init__(self) -> None:
        super().__init__("https://agent.example")

    @override
    def create_session(self, *, engine_id: str, engine_secret: str) -> Session:
        del engine_secret
        expires_at = datetime(2030, 1, 1, tzinfo=UTC)
        return Session(engine_id=engine_id, token="fresh-token", expires_at=expires_at)


def test_validate_credentials_rejects_open_credentials_permissions(tmp_path: Path) -> None:
    credentials_path = tmp_path / "credentials.toml"
    write_credentials(
        credentials_path,
        EngineCredentials(
            engine_id="11111111-1111-1111-1111-111111111111",
            engine_secret="x" * MIN_ENGINE_SECRET_BYTES,
            session_jwt="jwt",
            session_jwt_expires_at="2030-01-01T00:00:00+00:00",
            backend_url="https://agent.example",
            name="test-engine",
        ),
    )
    credentials_path.chmod(0o644)

    with pytest.raises(AuthError, match="permissions"):
        validate_credentials_file(credentials_path)


def test_validate_credentials_rejects_wrong_owner(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    from build_engine.agent import auth as auth_module

    credentials_path = tmp_path / "credentials.toml"
    write_credentials(
        credentials_path,
        EngineCredentials(
            engine_id="11111111-1111-1111-1111-111111111111",
            engine_secret="x" * MIN_ENGINE_SECRET_BYTES,
            session_jwt="jwt",
            session_jwt_expires_at="2030-01-01T00:00:00+00:00",
            backend_url="https://agent.example",
            name="test-engine",
        ),
    )
    monkeypatch.setattr(auth_module, "_expected_uid", lambda _service_user: 999999)
    monkeypatch.setattr(auth_module, "_expected_gid", lambda _service_group: 999999)

    with pytest.raises(AuthError, match="ownership"):
        validate_secret_file_ownership(
            credentials_path,
            service_user="build-engine",
            service_group="build-engine",
        )


def test_validate_engine_secret_rejects_short_or_non_ascii_values() -> None:
    with pytest.raises(AuthError, match="at least"):
        validate_engine_secret("short")
    with pytest.raises(AuthError, match="ASCII"):
        validate_engine_secret("x" * (MIN_ENGINE_SECRET_BYTES - 1) + "é")


def test_register_engine_generates_and_persists_credentials(tmp_path: Path) -> None:
    config = load_config(
        config_path=tmp_path / "missing.toml",
        credentials_path=tmp_path / "credentials.toml",
        overrides={
            "backend_url": "https://agent.example",
            "name": "test-engine",
        },
    )
    client = FakeRegistrationClient()

    result = register_engine(config, registration_token="one-time-token", client=client)

    credentials = load_credentials(config.credentials_path)
    assert result.engine_id == "11111111-1111-1111-1111-111111111111"
    assert credentials.engine_secret == "x" * MIN_ENGINE_SECRET_BYTES
    assert config.credentials_path.stat().st_mode & 0o077 == 0
    assert client.last_payload is not None
    assert client.last_payload["registration_token"] == "one-time-token"
    assert client.last_payload["name"] == "test-engine"


def test_refresh_session_updates_expired_persisted_token(tmp_path: Path) -> None:
    credentials_path = tmp_path / "credentials.toml"
    expired = datetime.now(UTC) - timedelta(minutes=1)
    write_credentials(
        credentials_path,
        EngineCredentials(
            engine_id="11111111-1111-1111-1111-111111111111",
            engine_secret="secret",
            session_jwt="old-token",
            session_jwt_expires_at=expired.isoformat(),
            backend_url="https://agent.example",
            name="test-engine",
        ),
    )

    refreshed = refresh_session(credentials_path, client=FakeRefreshClient())

    assert refreshed.session_jwt == "fresh-token"
    assert load_credentials(credentials_path).session_jwt == "fresh-token"

"""Registration and credential tests."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import override

import pytest

from build_engine.agent.auth import (
    AuthError,
    BuildEngineAuthClient,
    Session,
    certificate_fingerprint_sha256,
    ensure_engine_certificate,
    refresh_session,
    register_engine,
    validate_credentials_file,
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
        cert_pem: str,
        name: str,
        capabilities: dict[str, object],
    ) -> dict[str, object]:
        self.last_payload = {
            "registration_token": registration_token,
            "cert_pem": cert_pem,
            "name": name,
            "capabilities": capabilities,
        }
        return {
            "engine_id": "11111111-1111-1111-1111-111111111111",
            "engine_secret": "engine-secret",
            "backend_cert_fingerprint": "a" * 64,
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


def test_ensure_engine_certificate_creates_pair_with_restrictive_key(tmp_path: Path) -> None:
    cert_path = tmp_path / "engine.crt"
    key_path = tmp_path / "engine.key"

    ensure_engine_certificate(cert_path, key_path, common_name="test-engine")

    assert "BEGIN CERTIFICATE" in cert_path.read_text()
    assert "BEGIN PRIVATE KEY" in key_path.read_text()
    assert key_path.stat().st_mode & 0o077 == 0
    assert len(certificate_fingerprint_sha256(cert_path.read_bytes())) == 64


def test_validate_credentials_rejects_open_key_permissions(tmp_path: Path) -> None:
    cert_path = tmp_path / "engine.crt"
    key_path = tmp_path / "engine.key"
    credentials_path = tmp_path / "credentials.toml"
    ensure_engine_certificate(cert_path, key_path, common_name="test-engine")
    key_path.chmod(0o644)
    write_credentials(
        credentials_path,
        EngineCredentials(
            engine_id="11111111-1111-1111-1111-111111111111",
            engine_secret="secret",
            backend_cert_fingerprint="b" * 64,
            session_jwt="jwt",
            session_jwt_expires_at="2030-01-01T00:00:00+00:00",
            cert_path=cert_path,
            key_path=key_path,
            backend_url="https://agent.example",
            name="test-engine",
        ),
    )

    with pytest.raises(AuthError, match="permissions"):
        validate_credentials_file(credentials_path)


def test_register_engine_generates_and_persists_credentials(tmp_path: Path) -> None:
    config = load_config(
        config_path=tmp_path / "missing.toml",
        credentials_path=tmp_path / "credentials.toml",
        overrides={
            "backend_url": "https://agent.example",
            "name": "test-engine",
            "cert_path": tmp_path / "engine.crt",
            "key_path": tmp_path / "engine.key",
        },
    )
    client = FakeRegistrationClient()

    result = register_engine(config, registration_token="one-time-token", client=client)

    credentials = load_credentials(config.credentials_path)
    assert result.engine_id == "11111111-1111-1111-1111-111111111111"
    assert credentials.engine_secret == "engine-secret"
    assert credentials.backend_cert_fingerprint == "a" * 64
    assert config.credentials_path.stat().st_mode & 0o077 == 0
    assert client.last_payload is not None
    assert client.last_payload["registration_token"] == "one-time-token"
    assert client.last_payload["name"] == "test-engine"


def test_refresh_session_updates_expired_persisted_token(tmp_path: Path) -> None:
    cert_path = tmp_path / "engine.crt"
    key_path = tmp_path / "engine.key"
    credentials_path = tmp_path / "credentials.toml"
    ensure_engine_certificate(cert_path, key_path, common_name="test-engine")
    expired = datetime.now(UTC) - timedelta(minutes=1)
    write_credentials(
        credentials_path,
        EngineCredentials(
            engine_id="11111111-1111-1111-1111-111111111111",
            engine_secret="secret",
            backend_cert_fingerprint="c" * 64,
            session_jwt="old-token",
            session_jwt_expires_at=expired.isoformat(),
            cert_path=cert_path,
            key_path=key_path,
            backend_url="https://agent.example",
            name="test-engine",
        ),
    )

    refreshed = refresh_session(credentials_path, client=FakeRefreshClient())

    assert refreshed.session_jwt == "fresh-token"
    assert load_credentials(credentials_path).session_jwt == "fresh-token"

"""Registration and credential helpers."""

from __future__ import annotations

import json
import os
import pwd
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib import error, request

from build_engine import __version__
from build_engine.config import (
    EngineConfig,
    EngineCredentials,
    config_capabilities,
    load_credentials,
    write_credentials,
)


class AuthError(RuntimeError):
    """Raised when registration or credentials fail."""


SERVICE_USER = "build-engine"
SERVICE_GROUP = "build-engine"
MIN_ENGINE_SECRET_BYTES = 32


@dataclass(frozen=True, slots=True)
class Session:
    """Short-lived backend session JWT."""

    engine_id: str
    token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    """Registration result after credentials have been persisted."""

    engine_id: str
    session_jwt_expires_at: datetime
    credentials_path: Path


class BuildEngineAuthClient:
    """Small stdlib HTTP client for build-engine agent auth endpoints."""

    def __init__(
        self,
        backend_url: str,
        *,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.backend_url = backend_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def register(
        self,
        *,
        registration_token: str,
        name: str,
        capabilities: dict[str, object],
    ) -> dict[str, object]:
        """Register the engine using a one-time token."""

        return self._post_json(
            "/api/v1/build-engines/agent/register",
            {
                "registration_token": registration_token,
                "name": name,
                "capabilities": capabilities,
            },
        )

    def create_session(self, *, engine_id: str, engine_secret: str) -> Session:
        """Request a fresh backend-minted engine session JWT."""

        payload = self._post_json(
            "/api/v1/build-engines/agent/sessions",
            {"engine_id": engine_id, "engine_secret": engine_secret},
        )
        return _session_from_payload(payload)

    def refresh_session_if_needed(
        self,
        credentials: EngineCredentials,
        *,
        refresh_window: timedelta = timedelta(minutes=5),
    ) -> EngineCredentials:
        """Refresh the session JWT when it is missing or near expiry."""

        expires_at = parse_datetime(credentials.session_jwt_expires_at)
        if credentials.session_jwt and expires_at - aware_utcnow() > refresh_window:
            return credentials
        session = self.create_session(
            engine_id=credentials.engine_id,
            engine_secret=credentials.engine_secret,
        )
        refreshed = EngineCredentials(
            engine_id=credentials.engine_id,
            engine_secret=credentials.engine_secret,
            session_jwt=session.token,
            session_jwt_expires_at=session.expires_at.isoformat(),
            backend_url=credentials.backend_url,
            name=credentials.name,
        )
        return refreshed

    def _post_json(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.backend_url}{path}",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:  # nosec B310
                data = response.read()
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise AuthError(f"Backend auth request failed: HTTP {exc.code} {detail}") from exc
        except error.URLError as exc:
            raise AuthError(f"Backend auth request failed: {exc.reason}") from exc
        decoded = json.loads(data.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise AuthError("Backend auth response was not a JSON object")
        return decoded


def register_engine(
    config: EngineConfig,
    *,
    registration_token: str,
    client: BuildEngineAuthClient | None = None,
) -> RegistrationResult:
    """Generate credentials, register with coreapp, and persist auth material."""

    if not config.backend_url:
        raise AuthError("backend_url is required to register")
    if not config.name:
        raise AuthError("name is required to register")

    auth_client = client or BuildEngineAuthClient(config.backend_url)
    response = auth_client.register(
        registration_token=registration_token,
        name=config.name,
        capabilities=config_capabilities(config),
    )
    credentials = _credentials_from_registration_response(
        response,
        config=config,
    )
    write_credentials(config.credentials_path, credentials)
    validate_credentials_file(config.credentials_path)
    return RegistrationResult(
        engine_id=credentials.engine_id,
        session_jwt_expires_at=parse_datetime(credentials.session_jwt_expires_at),
        credentials_path=config.credentials_path,
    )


def refresh_session(
    credentials_path: Path,
    *,
    backend_url: str | None = None,
    client: BuildEngineAuthClient | None = None,
) -> EngineCredentials:
    """Refresh a persisted session JWT and write it back to disk."""

    credentials = load_credentials(credentials_path)
    selected_backend_url = backend_url or credentials.backend_url
    if not selected_backend_url:
        raise AuthError("backend_url is required to refresh a session")
    auth_client = client or BuildEngineAuthClient(selected_backend_url)
    refreshed = auth_client.refresh_session_if_needed(credentials)
    if refreshed != credentials:
        write_credentials(credentials_path, refreshed)
    return refreshed


def validate_credentials_file(
    credentials_path: Path,
    *,
    service_user: str | None = None,
    service_group: str | None = None,
) -> EngineCredentials:
    """Validate credential content and file permissions."""

    credentials = load_credentials(credentials_path)
    validate_secret_file_permissions(credentials_path)
    validate_secret_file_ownership(
        credentials_path,
        service_user=service_user,
        service_group=service_group,
    )
    validate_engine_secret(credentials.engine_secret)
    parse_datetime(credentials.session_jwt_expires_at)
    return credentials


def validate_secret_file_permissions(path: Path) -> None:
    """Reject group/world-readable secret files."""

    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise AuthError(f"Secret file permissions are too open: {path}")


def validate_secret_file_ownership(
    path: Path,
    *,
    service_user: str | None = None,
    service_group: str | None = None,
) -> None:
    """Reject credentials not owned by the expected service identity."""

    stat_result = path.stat()
    expected_uid = _expected_uid(service_user)
    expected_gid = _expected_gid(service_group)
    if stat_result.st_uid != expected_uid or stat_result.st_gid != expected_gid:
        raise AuthError(
            "Secret file ownership is invalid: "
            f"{path} uid={stat_result.st_uid} gid={stat_result.st_gid}; "
            f"expected uid={expected_uid} gid={expected_gid}"
        )


def validate_engine_secret(value: str) -> None:
    """Validate the persisted engine secret shape before use."""

    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise AuthError("engine_secret must contain only ASCII characters") from exc
    if len(encoded) < MIN_ENGINE_SECRET_BYTES:
        raise AuthError(f"engine_secret must be at least {MIN_ENGINE_SECRET_BYTES} bytes")


def parse_datetime(value: str) -> datetime:
    """Parse backend ISO timestamps as aware UTC datetimes."""

    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def aware_utcnow() -> datetime:
    """Return timezone-aware UTC now."""

    return datetime.now(UTC)


def _expected_uid(service_user: str | None) -> int:
    if service_user is None:
        return os.geteuid()
    try:
        return pwd.getpwnam(service_user).pw_uid
    except KeyError:
        return os.geteuid()


def _expected_gid(service_group: str | None) -> int:
    if service_group is None:
        return os.getegid()
    import grp

    try:
        return grp.getgrnam(service_group).gr_gid
    except KeyError:
        return os.getegid()


def _credentials_from_registration_response(
    payload: dict[str, object],
    *,
    config: EngineConfig,
) -> EngineCredentials:
    required = (
        "engine_id",
        "engine_secret",
        "session_jwt",
        "session_jwt_expires_at",
    )
    missing = [key for key in required if not payload.get(key)]
    if missing:
        raise AuthError(f"Registration response missing required keys: {', '.join(missing)}")
    return EngineCredentials(
        engine_id=str(payload["engine_id"]),
        engine_secret=str(payload["engine_secret"]),
        session_jwt=str(payload["session_jwt"]),
        session_jwt_expires_at=str(payload["session_jwt_expires_at"]),
        backend_url=config.backend_url,
        name=config.name,
    )


def _session_from_payload(payload: dict[str, object]) -> Session:
    required = ("engine_id", "session_jwt", "session_jwt_expires_at")
    missing = [key for key in required if not payload.get(key)]
    if missing:
        raise AuthError(f"Session response missing required keys: {', '.join(missing)}")
    return Session(
        engine_id=str(payload["engine_id"]),
        token=str(payload["session_jwt"]),
        expires_at=parse_datetime(str(payload["session_jwt_expires_at"])),
    )


def client_headers_for_credentials(credentials: EngineCredentials) -> dict[str, str]:
    """Return headers used by authenticated agent requests."""

    return {
        "Authorization": f"Bearer {credentials.session_jwt}",
        "User-Agent": f"mincemeat-build-engine/{__version__}",
    }

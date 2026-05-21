"""Registration, credential, and TLS pinning helpers."""

from __future__ import annotations

import json
import secrets
import socket
import ssl
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib import error, request
from urllib.parse import urlparse

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID

from build_engine import __version__
from build_engine.config import (
    EngineConfig,
    EngineCredentials,
    config_capabilities,
    load_credentials,
    write_credentials,
)


class AuthError(RuntimeError):
    """Raised when registration, credentials, or TLS pinning fail."""


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
    cert_path: Path
    key_path: Path


class BuildEngineAuthClient:
    """Small stdlib HTTP client for build-engine agent auth endpoints."""

    def __init__(
        self,
        backend_url: str,
        *,
        pinned_fingerprint: str | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.backend_url = backend_url.rstrip("/")
        self.pinned_fingerprint = (
            normalize_fingerprint(pinned_fingerprint) if pinned_fingerprint else None
        )
        self.timeout_seconds = timeout_seconds

    def register(
        self,
        *,
        registration_token: str,
        cert_pem: str,
        name: str,
        capabilities: dict[str, object],
    ) -> dict[str, object]:
        """Register the engine using a one-time token."""

        self.verify_backend_tls_pin()
        return self._post_json(
            "/api/v1/build-engines/agent/register",
            {
                "registration_token": registration_token,
                "cert_pem": cert_pem,
                "name": name,
                "capabilities": capabilities,
            },
        )

    def create_session(self, *, engine_id: str, engine_secret: str) -> Session:
        """Request a fresh backend-minted engine session JWT."""

        self.verify_backend_tls_pin()
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
            backend_cert_fingerprint=credentials.backend_cert_fingerprint,
            session_jwt=session.token,
            session_jwt_expires_at=session.expires_at.isoformat(),
            cert_path=credentials.cert_path,
            key_path=credentials.key_path,
            backend_url=credentials.backend_url,
            name=credentials.name,
        )
        return refreshed

    def verify_backend_tls_pin(self) -> None:
        """Verify the HTTPS leaf certificate fingerprint against the stored pin."""

        if self.pinned_fingerprint is None:
            return
        actual = backend_tls_fingerprint(self.backend_url, timeout_seconds=self.timeout_seconds)
        if not secrets.compare_digest(actual, self.pinned_fingerprint):
            raise AuthError("Backend TLS fingerprint mismatch")

    def _post_json(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.backend_url}{path}",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
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

    ensure_engine_certificate(config.cert_path, config.key_path, common_name=config.name)
    cert_pem = config.cert_path.read_text()
    auth_client = client or BuildEngineAuthClient(config.backend_url)
    response = auth_client.register(
        registration_token=registration_token,
        cert_pem=cert_pem,
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
        cert_path=config.cert_path,
        key_path=config.key_path,
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
    auth_client = client or BuildEngineAuthClient(
        selected_backend_url,
        pinned_fingerprint=credentials.backend_cert_fingerprint,
    )
    refreshed = auth_client.refresh_session_if_needed(credentials)
    if refreshed != credentials:
        write_credentials(credentials_path, refreshed)
    return refreshed


def ensure_engine_certificate(cert_path: Path, key_path: Path, *, common_name: str) -> None:
    """Create an Ed25519 self-signed client certificate if the pair is absent."""

    if cert_path.exists() and key_path.exists():
        validate_certificate_pair(cert_path, key_path)
        validate_secret_file_permissions(key_path)
        return
    if cert_path.exists() != key_path.exists():
        raise AuthError("Certificate and key must either both exist or both be absent")

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.parent.chmod(0o700)
    key_path.parent.chmod(0o700)

    private_key = ed25519.Ed25519PrivateKey.generate()
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Mincemeat Build Engine"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )
    now = aware_utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(private_key, algorithm=None)
    )

    key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)

    key_path.write_bytes(key_pem)
    key_path.chmod(0o600)
    cert_path.write_bytes(cert_pem)
    cert_path.chmod(0o644)


def validate_certificate_pair(cert_path: Path, key_path: Path) -> None:
    """Parse the configured certificate and private key."""

    try:
        x509.load_pem_x509_certificate(cert_path.read_bytes())
        serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    except ValueError as exc:
        raise AuthError("Certificate or private key is invalid") from exc


def validate_credentials_file(credentials_path: Path) -> EngineCredentials:
    """Validate credential content, linked cert/key files, and permissions."""

    credentials = load_credentials(credentials_path)
    normalize_fingerprint(credentials.backend_cert_fingerprint)
    validate_secret_file_permissions(credentials_path)
    validate_secret_file_permissions(credentials.key_path)
    validate_certificate_pair(credentials.cert_path, credentials.key_path)
    parse_datetime(credentials.session_jwt_expires_at)
    return credentials


def validate_secret_file_permissions(path: Path) -> None:
    """Reject group/world-readable secret files."""

    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise AuthError(f"Secret file permissions are too open: {path}")


def certificate_fingerprint_sha256(cert_pem: str | bytes) -> str:
    """Return the normalized SHA256 fingerprint for a PEM certificate."""

    cert_bytes = cert_pem.encode("utf-8") if isinstance(cert_pem, str) else cert_pem
    try:
        cert = x509.load_pem_x509_certificate(cert_bytes)
    except ValueError as exc:
        raise AuthError("Invalid certificate PEM") from exc
    return cert.fingerprint(hashes.SHA256()).hex()


def backend_tls_fingerprint(backend_url: str, *, timeout_seconds: float = 15.0) -> str:
    """Fetch the HTTPS leaf certificate SHA256 fingerprint."""

    parsed = urlparse(backend_url)
    if parsed.scheme != "https":
        return ""
    if parsed.hostname is None:
        raise AuthError("Backend URL is missing a hostname")
    port = parsed.port or 443
    context = ssl.create_default_context()
    with (
        socket.create_connection((parsed.hostname, port), timeout=timeout_seconds) as sock,
        context.wrap_socket(sock, server_hostname=parsed.hostname) as tls,
    ):
        cert_der = tls.getpeercert(binary_form=True)
    if cert_der is None:
        raise AuthError("Backend did not present a TLS certificate")
    cert = x509.load_der_x509_certificate(cert_der)
    return cert.fingerprint(hashes.SHA256()).hex()


def normalize_fingerprint(fingerprint: str) -> str:
    """Normalize and validate a SHA256 certificate fingerprint."""

    normalized = fingerprint.strip().replace(":", "").lower()
    if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
        raise AuthError("Certificate fingerprint must be a SHA256 hex digest")
    return normalized


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


def _credentials_from_registration_response(
    payload: dict[str, object],
    *,
    config: EngineConfig,
) -> EngineCredentials:
    required = (
        "engine_id",
        "engine_secret",
        "backend_cert_fingerprint",
        "session_jwt",
        "session_jwt_expires_at",
    )
    missing = [key for key in required if not payload.get(key)]
    if missing:
        raise AuthError(f"Registration response missing required keys: {', '.join(missing)}")
    return EngineCredentials(
        engine_id=str(payload["engine_id"]),
        engine_secret=str(payload["engine_secret"]),
        backend_cert_fingerprint=normalize_fingerprint(str(payload["backend_cert_fingerprint"])),
        session_jwt=str(payload["session_jwt"]),
        session_jwt_expires_at=str(payload["session_jwt_expires_at"]),
        cert_path=config.cert_path,
        key_path=config.key_path,
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

    cert_fingerprint = certificate_fingerprint_sha256(credentials.cert_path.read_bytes())
    return {
        "Authorization": f"Bearer {credentials.session_jwt}",
        "X-Client-Cert-Fingerprint": cert_fingerprint,
        "User-Agent": f"mincemeat-build-engine/{__version__}",
    }

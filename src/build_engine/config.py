"""Configuration loading for the build engine."""

import grp
import os
import platform
import pwd
import tempfile
import tomllib
import uuid
from collections.abc import Mapping
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path
from typing import Any

from build_engine.agent.protocol import PROTOCOL_VERSION

DEFAULT_CONFIG_PATH = Path("/etc/mincemeat/build-engine/config.toml")
DEFAULT_CREDENTIALS_PATH = Path("/etc/mincemeat/build-engine/credentials.toml")
DEFAULT_STATE_DIR = Path("/var/lib/build-engine")

# Pinned to the published build-engine-images manifest snapshot in the repo
# root. Drift is detected by `scripts/sync_contracts.py` so registration never
# advertises an unreleased or stale manifest version.
DEFAULT_IMAGE_MANIFEST_VERSION = "1.0.0"

# Production v2 builder matrix. The manifest is the source of truth for the
# immutable references used by these logical image keys.
DEFAULT_IMAGES: tuple[str, ...] = ("node:22", "hugo:latest")


@dataclass(frozen=True, slots=True)
class EngineDefaults:
    """Compiled defaults documented in the build-engine design."""

    max_concurrency: int = 2
    heartbeat_interval_seconds: int = 15
    build_timeout_seconds: int = 600
    sigterm_grace_seconds: int = 10
    container_memory: str = "2g"
    container_cpus: float = 1.0
    artifact_max_bytes: int = 524_288_000
    cache_site_max_bytes: int = 5_368_709_120
    cache_ttl_days: int = 30


DEFAULTS = EngineDefaults()


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """Runtime settings after defaults, files, environment, and CLI overrides."""

    backend_url: str | None = None
    name: str | None = None
    max_concurrency: int = DEFAULTS.max_concurrency
    heartbeat_interval_seconds: int = DEFAULTS.heartbeat_interval_seconds
    build_timeout_seconds: int = DEFAULTS.build_timeout_seconds
    sigterm_grace_seconds: int = DEFAULTS.sigterm_grace_seconds
    container_memory: str = DEFAULTS.container_memory
    container_cpus: float = DEFAULTS.container_cpus
    artifact_max_bytes: int = DEFAULTS.artifact_max_bytes
    cache_site_max_bytes: int = DEFAULTS.cache_site_max_bytes
    cache_ttl_days: int = DEFAULTS.cache_ttl_days
    credentials_path: Path = DEFAULT_CREDENTIALS_PATH
    state_dir: Path = DEFAULT_STATE_DIR
    image_manifest_version: str = DEFAULT_IMAGE_MANIFEST_VERSION
    images: tuple[str, ...] = DEFAULT_IMAGES
    storage_origins: tuple[str, ...] = ()
    lease_seconds: int = 120
    outbox_max_bytes: int = 268_435_456
    state_retention_days: int = 7
    allow_local_source_urls: bool = False
    os: str = field(default_factory=lambda: platform.system().lower())
    arch: str = field(default_factory=lambda: _normalize_arch(platform.machine()))

    def __post_init__(self) -> None:
        """Reject invalid operational values before the service starts."""

        if not 1 <= self.max_concurrency <= 16:
            raise ValueError("max_concurrency must be between 1 and 16")
        for name in (
            "heartbeat_interval_seconds",
            "build_timeout_seconds",
            "sigterm_grace_seconds",
            "artifact_max_bytes",
            "cache_site_max_bytes",
            "cache_ttl_days",
            "lease_seconds",
            "outbox_max_bytes",
            "state_retention_days",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.container_cpus <= 0:
            raise ValueError("container_cpus must be positive")
        if not self.image_manifest_version:
            raise ValueError("image_manifest_version must not be empty")
        if not self.images:
            raise ValueError("at least one builder image must be configured")
        if any(not isinstance(origin, str) or not origin for origin in self.storage_origins):
            raise ValueError("storage_origins must contain non-empty origins")
        if self.backend_url is not None and not _is_secure_origin(self.backend_url):
            raise ValueError("backend_url must use HTTPS")
        if any(not _is_secure_origin(origin) for origin in self.storage_origins):
            raise ValueError("storage_origins must use HTTPS")


@dataclass(frozen=True, slots=True)
class EngineCredentials:
    """Credentials persisted after registration."""

    engine_id: str
    engine_secret: str
    session_jwt: str
    session_jwt_expires_at: str
    backend_url: str | None = None
    name: str | None = None


def load_config(
    *,
    config_path: Path | str | None = None,
    credentials_path: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    overrides: dict[str, object | None] | None = None,
) -> EngineConfig:
    """Load configuration using the documented layer order."""

    selected_config_path = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH
    selected_credentials_path = Path(credentials_path) if credentials_path is not None else None
    values = _config_defaults()

    if selected_config_path.exists():
        values.update(_load_toml(selected_config_path))

    if selected_credentials_path is not None:
        values["credentials_path"] = selected_credentials_path

    active_credentials_path = _path_value(values["credentials_path"])
    values["credentials_path"] = active_credentials_path
    if active_credentials_path.exists():
        credentials_values = _load_toml(active_credentials_path)
        for key in ("backend_url", "name"):
            if key in credentials_values:
                values[key] = credentials_values[key]

    values.update(_env_overrides(env or os.environ))
    for key, value in (overrides or {}).items():
        if value is not None:
            values[key] = value

    known_fields = {config_field.name for config_field in fields(EngineConfig)}
    unknown_fields = sorted(set(values) - known_fields)
    if unknown_fields:
        raise ValueError("Unknown configuration keys: " + ", ".join(unknown_fields))
    values = _coerce_config_values(values)

    return EngineConfig(**values)


def load_credentials(path: Path | str) -> EngineCredentials:
    """Load persisted engine credentials."""

    raw = _load_toml(Path(path))
    required = (
        "engine_id",
        "engine_secret",
        "session_jwt",
        "session_jwt_expires_at",
    )
    missing = [key for key in required if not raw.get(key)]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"Credentials file is missing required keys: {joined}")
    try:
        uuid.UUID(str(raw["engine_id"]))
    except ValueError as exc:
        raise ValueError("Credentials engine_id must be a UUID") from exc
    return EngineCredentials(
        engine_id=str(raw["engine_id"]),
        engine_secret=str(raw["engine_secret"]),
        session_jwt=str(raw["session_jwt"]),
        session_jwt_expires_at=str(raw["session_jwt_expires_at"]),
        backend_url=str(raw["backend_url"]) if raw.get("backend_url") else None,
        name=str(raw["name"]) if raw.get("name") else None,
    )


def write_credentials(path: Path | str, credentials: EngineCredentials) -> None:
    """Persist credentials as a small TOML file with restrictive permissions."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(
        (
            f"engine_id = {_toml_string(credentials.engine_id)}",
            f"engine_secret = {_toml_string(credentials.engine_secret)}",
            f"session_jwt = {_toml_string(credentials.session_jwt)}",
            f"session_jwt_expires_at = {_toml_string(credentials.session_jwt_expires_at)}",
            f"backend_url = {_toml_string(credentials.backend_url or '')}",
            f"name = {_toml_string(credentials.name or '')}",
            "",
        )
    )
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _chown_service_file(temporary)
        temporary.replace(destination)
        destination.chmod(0o600)
        _chown_service_file(destination)
    finally:
        temporary.unlink(missing_ok=True)


def config_capabilities(config: EngineConfig) -> dict[str, object]:
    """Return the registration capabilities payload expected by coreapp."""

    from build_engine.detect.framework import SUPPORTED_FRAMEWORK_IDS

    return {
        "os": config.os,
        "arch": config.arch,
        "max_concurrency": config.max_concurrency,
        "images": list(config.images),
        "proto_version": PROTOCOL_VERSION,
        "image_manifest_version": config.image_manifest_version,
        "frameworks": sorted(SUPPORTED_FRAMEWORK_IDS),
        "package_managers": ["npm", "pnpm"],
        "network": "unrestricted",
    }


def _config_defaults() -> dict[str, Any]:
    defaults: dict[str, Any] = {}
    for config_field in fields(EngineConfig):
        if config_field.default is not MISSING:
            defaults[config_field.name] = config_field.default
        elif config_field.default_factory is not MISSING:
            defaults[config_field.name] = config_field.default_factory()
    return defaults


def _coerce_config_values(values: dict[str, Any]) -> dict[str, Any]:
    coerced = dict(values)
    coerced["credentials_path"] = _path_value(coerced["credentials_path"])
    coerced["state_dir"] = _path_value(coerced["state_dir"])
    if isinstance(coerced.get("images"), list):
        coerced["images"] = tuple(str(item) for item in coerced["images"])
    elif isinstance(coerced.get("images"), str):
        coerced["images"] = _split_csv(str(coerced["images"]))
    if isinstance(coerced.get("storage_origins"), list):
        coerced["storage_origins"] = tuple(str(item) for item in coerced["storage_origins"])
    elif isinstance(coerced.get("storage_origins"), str):
        coerced["storage_origins"] = _split_csv(str(coerced["storage_origins"]))
    if isinstance(coerced.get("allow_local_source_urls"), str):
        coerced["allow_local_source_urls"] = _bool_value(str(coerced["allow_local_source_urls"]))
    return coerced


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return {str(key): value for key, value in data.items()}


def _env_overrides(env: Mapping[str, str]) -> dict[str, Any]:
    keys = _config_defaults().keys()
    result: dict[str, Any] = {}
    for key in keys:
        env_name = f"BUILD_ENGINE_{key.upper()}"
        if env_name in env:
            result[key] = _coerce_env_value(key, env[env_name])
    return result


def _coerce_env_value(key: str, value: str) -> object:
    if key in {
        "max_concurrency",
        "heartbeat_interval_seconds",
        "build_timeout_seconds",
        "sigterm_grace_seconds",
        "artifact_max_bytes",
        "cache_site_max_bytes",
        "cache_ttl_days",
        "lease_seconds",
        "outbox_max_bytes",
        "state_retention_days",
    }:
        return int(value)
    if key == "container_cpus":
        return float(value)
    if key == "allow_local_source_urls":
        return _bool_value(value)
    if key in {"images", "storage_origins"}:
        return _split_csv(value)
    if key.endswith("_path") or key.endswith("_dir"):
        return Path(value)
    return value


def _path_value(value: object) -> Path:
    return value if isinstance(value, Path) else Path(str(value))


def _toml_string(value: str) -> str:
    import json

    return json.dumps(value)


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _is_secure_origin(value: str) -> bool:
    from urllib.parse import urlparse

    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def _chown_service_file(path: Path) -> None:
    """Apply service-user ownership when the packaged account exists."""

    try:
        user = pwd.getpwnam("build-engine")
        group = grp.getgrnam("build-engine")
    except KeyError:
        return
    try:
        os.chown(path, user.pw_uid, group.gr_gid)
    except PermissionError:
        if os.geteuid() == 0:
            raise


def _bool_value(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value}")


def _normalize_arch(machine: str) -> str:
    normalized = machine.lower()
    if normalized in {"x86_64", "amd64"}:
        return "amd64"
    if normalized in {"aarch64", "arm64"}:
        return "arm64"
    return normalized

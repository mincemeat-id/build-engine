"""Configuration loading for the build engine."""

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("/etc/mincemeat/build-engine/config.toml")
DEFAULT_CREDENTIALS_PATH = Path("/etc/mincemeat/build-engine/credentials.toml")
DEFAULT_CERT_PATH = Path("/etc/mincemeat/build-engine/engine.crt")
DEFAULT_KEY_PATH = Path("/etc/mincemeat/build-engine/engine.key")


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
    cert_path: Path = DEFAULT_CERT_PATH
    key_path: Path = DEFAULT_KEY_PATH
    image_manifest_version: str = "1.0.0"
    images: tuple[str, ...] = ("node:20", "node:22", "bun:1", "hugo:latest")
    proto_version: int = 1
    os: str = "linux"
    arch: str = "amd64"


@dataclass(frozen=True, slots=True)
class EngineCredentials:
    """Credentials persisted after registration."""

    engine_id: str
    engine_secret: str
    backend_cert_fingerprint: str
    session_jwt: str
    session_jwt_expires_at: str
    cert_path: Path
    key_path: Path
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
    if active_credentials_path.exists():
        credentials_values = _load_toml(active_credentials_path)
        for key in ("backend_url", "name", "cert_path", "key_path"):
            if key in credentials_values:
                values[key] = credentials_values[key]

    values.update(_env_overrides(env or os.environ))
    for key, value in (overrides or {}).items():
        if value is not None:
            values[key] = value

    values["credentials_path"] = _path_value(values["credentials_path"])
    values["cert_path"] = _path_value(values["cert_path"])
    values["key_path"] = _path_value(values["key_path"])
    if isinstance(values.get("images"), list):
        values["images"] = tuple(str(item) for item in values["images"])
    elif isinstance(values.get("images"), str):
        values["images"] = tuple(part.strip() for part in str(values["images"]).split(",") if part)

    return EngineConfig(**values)


def load_credentials(path: Path | str) -> EngineCredentials:
    """Load persisted engine credentials."""

    raw = _load_toml(Path(path))
    required = (
        "engine_id",
        "engine_secret",
        "backend_cert_fingerprint",
        "session_jwt",
        "session_jwt_expires_at",
        "cert_path",
        "key_path",
    )
    missing = [key for key in required if not raw.get(key)]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"Credentials file is missing required keys: {joined}")
    return EngineCredentials(
        engine_id=str(raw["engine_id"]),
        engine_secret=str(raw["engine_secret"]),
        backend_cert_fingerprint=str(raw["backend_cert_fingerprint"]),
        session_jwt=str(raw["session_jwt"]),
        session_jwt_expires_at=str(raw["session_jwt_expires_at"]),
        cert_path=_path_value(raw["cert_path"]),
        key_path=_path_value(raw["key_path"]),
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
            f"backend_cert_fingerprint = {_toml_string(credentials.backend_cert_fingerprint)}",
            f"session_jwt = {_toml_string(credentials.session_jwt)}",
            f"session_jwt_expires_at = {_toml_string(credentials.session_jwt_expires_at)}",
            f"cert_path = {_toml_string(str(credentials.cert_path))}",
            f"key_path = {_toml_string(str(credentials.key_path))}",
            f"backend_url = {_toml_string(credentials.backend_url or '')}",
            f"name = {_toml_string(credentials.name or '')}",
            "",
        )
    )
    destination.write_text(content)
    destination.chmod(0o600)


def config_capabilities(config: EngineConfig) -> dict[str, object]:
    """Return the registration capabilities payload expected by coreapp."""

    return {
        "os": config.os,
        "arch": config.arch,
        "max_concurrency": config.max_concurrency,
        "images": list(config.images),
        "proto_version": config.proto_version,
        "image_manifest_version": config.image_manifest_version,
    }


def _config_defaults() -> dict[str, Any]:
    return {
        "backend_url": None,
        "name": None,
        "max_concurrency": DEFAULTS.max_concurrency,
        "heartbeat_interval_seconds": DEFAULTS.heartbeat_interval_seconds,
        "build_timeout_seconds": DEFAULTS.build_timeout_seconds,
        "sigterm_grace_seconds": DEFAULTS.sigterm_grace_seconds,
        "container_memory": DEFAULTS.container_memory,
        "container_cpus": DEFAULTS.container_cpus,
        "artifact_max_bytes": DEFAULTS.artifact_max_bytes,
        "cache_site_max_bytes": DEFAULTS.cache_site_max_bytes,
        "cache_ttl_days": DEFAULTS.cache_ttl_days,
        "credentials_path": DEFAULT_CREDENTIALS_PATH,
        "cert_path": DEFAULT_CERT_PATH,
        "key_path": DEFAULT_KEY_PATH,
        "image_manifest_version": "1.0.0",
        "images": ("node:20", "node:22", "bun:1", "hugo:latest"),
        "proto_version": 1,
        "os": "linux",
        "arch": "amd64",
    }


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
        "proto_version",
    }:
        return int(value)
    if key == "container_cpus":
        return float(value)
    if key == "images":
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if key.endswith("_path"):
        return Path(value)
    return value


def _path_value(value: object) -> Path:
    return value if isinstance(value, Path) else Path(str(value))


def _toml_string(value: str) -> str:
    import json

    return json.dumps(value)

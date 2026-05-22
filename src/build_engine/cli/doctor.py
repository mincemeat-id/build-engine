"""Operator diagnostics for the build-engine host."""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal
from urllib import error, request

import websockets

from build_engine import __version__
from build_engine.agent.auth import (
    AuthError,
    client_headers_for_credentials,
    validate_credentials_file,
)
from build_engine.agent.protocol import PROTOCOL_VERSION, Envelope, ProtocolError, decode_frame
from build_engine.agent.uplink import uplink_headers, websocket_url
from build_engine.config import EngineConfig, EngineCredentials
from build_engine.executor.docker_runner import DockerError, pull_image
from build_engine.executor.network import NetworkGuardError, ensure_network_guard

CheckStatus = Literal["ok", "fail", "skip"]
Clock = Callable[[], datetime]

MIN_FREE_BYTES = 20 * 1024**3
HEALTH_PATH = "/api/v1/build-engines/agent/health"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    """One diagnostic result."""

    name: str
    status: CheckStatus
    detail: str
    values: dict[str, object] | None = None

    @property
    def ok(self) -> bool:
        """Return compatibility with the original boolean CLI payload."""

        return self.status == "ok"

    def to_dict(self) -> dict[str, object]:
        """Return the JSON representation for `doctor --json`."""

        payload: dict[str, object] = {
            "name": self.name,
            "status": self.status,
            "ok": self.ok,
            "detail": self.detail,
        }
        if self.values:
            payload["values"] = self.values
        return payload


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Full diagnostic payload."""

    version: str
    protocol_version: int
    status: Literal["ok", "error"]
    checks: tuple[DoctorCheck, ...]

    def to_dict(self) -> dict[str, object]:
        """Return a stable machine-readable payload."""

        return {
            "version": self.version,
            "protocol_version": self.protocol_version,
            "status": self.status,
            "checks": [check.to_dict() for check in self.checks],
        }


def run_doctor(
    config: EngineConfig,
    *,
    timeout_seconds: float = 15.0,
    image_pull_timeout_seconds: float = 60.0,
    clock: Clock | None = None,
) -> DoctorReport:
    """Run host, credential, backend, executor, and queue diagnostics."""

    checks: list[DoctorCheck] = [_check_version(config)]
    credentials: EngineCredentials | None = None

    try:
        credentials = validate_credentials_file(config.credentials_path)
    except (AuthError, ValueError, FileNotFoundError, OSError) as exc:
        checks.append(_fail("credentials", str(exc)))
    else:
        checks.append(
            _ok(
                "credentials",
                str(config.credentials_path),
                {"engine_id": credentials.engine_id},
            )
        )

    checks.extend(
        (
            _check_docker(timeout_seconds=timeout_seconds),
            _check_cgroup_v2(),
            _check_disk_space(config),
            _check_writable_paths(config),
            _check_sqlite_integrity(config.state_dir / "queue.sqlite"),
            _check_network_guard(config, timeout_seconds=timeout_seconds),
        )
    )

    checks.append(
        _check_agent_health(
            config,
            credentials=credentials,
            timeout_seconds=timeout_seconds,
            clock=clock or (lambda: datetime.now(UTC)),
        )
    )
    checks.append(
        _check_wss_handshake(config, credentials=credentials, timeout_seconds=timeout_seconds)
    )
    checks.append(_check_image_pull(config, timeout_seconds=image_pull_timeout_seconds))

    status: Literal["ok", "error"]
    status = "error" if any(check.status == "fail" for check in checks) else "ok"
    return DoctorReport(
        version=__version__,
        protocol_version=PROTOCOL_VERSION,
        status=status,
        checks=tuple(checks),
    )


def render_human(report: DoctorReport) -> str:
    """Render human-friendly diagnostics."""

    lines = [
        f"build-engine doctor: {report.status}",
        f"version: {report.version}",
        f"protocol: v{report.protocol_version}",
    ]
    for check in report.checks:
        label = {"ok": "OK", "fail": "FAIL", "skip": "SKIP"}[check.status]
        lines.append(f"- {check.name}: {label} ({check.detail})")
    return "\n".join(lines)


def render_json(report: DoctorReport) -> str:
    """Render machine-friendly diagnostics."""

    return json.dumps(report.to_dict(), sort_keys=True)


def _check_version(config: EngineConfig) -> DoctorCheck:
    if config.proto_version != PROTOCOL_VERSION:
        return _fail(
            "version",
            "configured protocol version does not match the bundled protocol",
            {"version": __version__, "configured_proto": config.proto_version},
        )
    return _ok(
        "version",
        f"build-engine {__version__}, protocol v{PROTOCOL_VERSION}",
        {"version": __version__, "protocol_version": PROTOCOL_VERSION},
    )


def _check_docker(*, timeout_seconds: float) -> DoctorCheck:
    try:
        result = subprocess.run(
            ["docker", "version", "--format", "{{json .Server}}"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return _fail("docker", str(exc))
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or "docker version failed"
        return _fail("docker", detail)

    detail = "daemon reachable"
    values: dict[str, object] = {}
    try:
        decoded = json.loads(result.stdout)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict):
        version = decoded.get("Version")
        cgroup_driver = decoded.get("CgroupDriver")
        values = {
            key: value
            for key, value in {"version": version, "cgroup_driver": cgroup_driver}.items()
            if isinstance(value, str)
        }
        if isinstance(version, str):
            detail = f"daemon reachable, server {version}"
    return _ok("docker", detail, values or None)


def _check_cgroup_v2() -> DoctorCheck:
    controllers = Path("/sys/fs/cgroup/cgroup.controllers")
    if not controllers.exists():
        return _fail("cgroup_v2", "unified cgroup v2 hierarchy was not detected")
    try:
        available = controllers.read_text(encoding="utf-8").strip().split()
    except OSError as exc:
        return _fail("cgroup_v2", str(exc))
    return _ok("cgroup_v2", "unified hierarchy detected", {"controllers": available})


def _check_disk_space(config: EngineConfig) -> DoctorCheck:
    target = _nearest_existing_path(config.state_dir)
    try:
        usage = shutil.disk_usage(target)
    except OSError as exc:
        return _fail("disk_space", str(exc))
    values: dict[str, object] = {
        "path": str(target),
        "free_bytes": usage.free,
        "required_bytes": MIN_FREE_BYTES,
    }
    if usage.free < MIN_FREE_BYTES:
        return _fail(
            "disk_space",
            f"{_format_gib(usage.free)} free; {_format_gib(MIN_FREE_BYTES)} required",
            values,
        )
    return _ok("disk_space", f"{_format_gib(usage.free)} free", values)


def _check_writable_paths(config: EngineConfig) -> DoctorCheck:
    paths = (config.state_dir / "jobs", config.state_dir / "cache")
    try:
        for path in paths:
            path.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(prefix=".doctor-", dir=path, delete=True):
                pass
    except OSError as exc:
        return _fail("writable_paths", str(exc), {"paths": [str(path) for path in paths]})
    return _ok(
        "writable_paths",
        "workspace and cache paths are writable",
        {"paths": [str(path) for path in paths]},
    )


def _check_sqlite_integrity(queue_path: Path) -> DoctorCheck:
    if not queue_path.exists():
        return _ok(
            "sqlite_integrity",
            "queue database has not been created yet",
            {"path": str(queue_path)},
        )
    try:
        with sqlite3.connect(queue_path) as db:
            row = db.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as exc:
        return _fail("sqlite_integrity", str(exc), {"path": str(queue_path)})
    result = str(row[0]) if row else ""
    if result != "ok":
        return _fail(
            "sqlite_integrity",
            result or "integrity_check returned no rows",
            {"path": str(queue_path)},
        )
    return _ok("sqlite_integrity", "ok", {"path": str(queue_path)})


def _check_network_guard(config: EngineConfig, *, timeout_seconds: float) -> DoctorCheck:
    del timeout_seconds
    try:
        guard = ensure_network_guard(blocklist=config.network_blocklist)
    except (NetworkGuardError, FileNotFoundError, OSError) as exc:
        return _fail("network_guard", str(exc))
    return _ok(
        "network_guard",
        f"Docker network {guard.name} is available",
        {"network": guard.name, "blocklist": list(guard.blocklist)},
    )


def _check_agent_health(
    config: EngineConfig,
    *,
    credentials: EngineCredentials | None,
    timeout_seconds: float,
    clock: Clock,
) -> DoctorCheck:
    backend_url = _backend_url(config, credentials)
    if credentials is None:
        return _skip("agent_health", "credentials are unavailable")
    if not backend_url:
        return _fail("agent_health", "backend_url is not configured")
    url = _join_backend_path(backend_url, HEALTH_PATH)
    req = request.Request(url, headers=client_headers_for_credentials(credentials))
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            body = response.read()
            date_header = response.headers.get("Date")
    except error.HTTPError as exc:
        return _fail("agent_health", f"HTTP {exc.code}")
    except (error.URLError, OSError) as exc:
        return _fail("agent_health", str(getattr(exc, "reason", exc)))

    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        return _fail("agent_health", f"health response was not JSON: {exc}")
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return _fail("agent_health", "health endpoint did not report ok")
    proto_min = payload.get("proto_min")
    proto_max = payload.get("proto_max")
    if (
        not isinstance(proto_min, int)
        or not isinstance(proto_max, int)
        or not proto_min <= PROTOCOL_VERSION <= proto_max
    ):
        return _fail("agent_health", "backend does not support this protocol version")

    values: dict[str, object] = {"proto_min": proto_min, "proto_max": proto_max}
    skew = _clock_skew_seconds(date_header, now=clock())
    if skew is not None:
        values["clock_skew_seconds"] = skew
        if abs(skew) > 60:
            return _fail("clock_skew", f"clock skew is {skew:.1f}s", values)
        return _ok("agent_health", f"healthy; clock skew {skew:.1f}s", values)
    return _ok("agent_health", "healthy; backend Date header unavailable", values)


def _check_wss_handshake(
    config: EngineConfig,
    *,
    credentials: EngineCredentials | None,
    timeout_seconds: float,
) -> DoctorCheck:
    backend_url = _backend_url(config, credentials)
    if credentials is None:
        return _skip("wss_handshake", "credentials are unavailable")
    if not backend_url:
        return _fail("wss_handshake", "backend_url is not configured")
    try:
        welcome = asyncio.run(
            _receive_welcome(config, credentials, timeout_seconds=timeout_seconds)
        )
    except (OSError, ProtocolError, AuthError, TimeoutError, websockets.WebSocketException) as exc:
        return _fail("wss_handshake", str(exc))
    proto = welcome.payload.get("proto_negotiated")
    if welcome.payload.get("engine_id") != credentials.engine_id or proto != config.proto_version:
        return _fail("wss_handshake", "welcome frame did not negotiate this engine/protocol")
    return _ok("wss_handshake", "welcome frame received", {"proto_negotiated": proto})


def _check_image_pull(config: EngineConfig, *, timeout_seconds: float) -> DoctorCheck:
    image = config.images[0] if config.images else "node:20"
    try:
        pull_image(image, timeout_seconds=timeout_seconds)
    except (DockerError, subprocess.TimeoutExpired, OSError) as exc:
        return _fail("image_pull", str(exc), {"image": image})
    return _ok("image_pull", f"pulled {image}", {"image": image})


async def _receive_welcome(
    config: EngineConfig,
    credentials: EngineCredentials,
    *,
    timeout_seconds: float,
) -> Envelope:
    url = websocket_url(credentials.backend_url or config.backend_url or "")
    async with websockets.connect(
        url,
        additional_headers=uplink_headers(config, credentials),
        max_size=1_048_576,
        open_timeout=timeout_seconds,
    ) as websocket:
        frame = await asyncio.wait_for(websocket.recv(), timeout=timeout_seconds)
    return decode_frame(frame)


def _join_backend_path(backend_url: str, path: str) -> str:
    return f"{backend_url.rstrip('/')}{path}"


def _backend_url(config: EngineConfig, credentials: EngineCredentials | None) -> str | None:
    if credentials is not None and credentials.backend_url:
        return credentials.backend_url
    return config.backend_url


def _clock_skew_seconds(date_header: str | None, *, now: datetime) -> float | None:
    if date_header is None:
        return None
    try:
        backend_time = parsedate_to_datetime(date_header)
    except TypeError, ValueError:
        return None
    if backend_time.tzinfo is None:
        backend_time = backend_time.replace(tzinfo=UTC)
    return (now.astimezone(UTC) - backend_time.astimezone(UTC)).total_seconds()


def _nearest_existing_path(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _format_gib(value: int) -> str:
    return f"{value / 1024**3:.1f} GiB"


def _ok(name: str, detail: str, values: dict[str, object] | None = None) -> DoctorCheck:
    return DoctorCheck(name=name, status="ok", detail=detail, values=values)


def _fail(name: str, detail: str, values: dict[str, object] | None = None) -> DoctorCheck:
    return DoctorCheck(name=name, status="fail", detail=detail, values=values)


def _skip(name: str, detail: str) -> DoctorCheck:
    return DoctorCheck(name=name, status="skip", detail=detail)

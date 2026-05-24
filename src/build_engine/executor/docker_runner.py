"""Docker image pulling and hardened container execution."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess  # nosec B404
import tempfile
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from build_engine.config import EngineConfig
from build_engine.executor.network import DockerNetworkGuard
from build_engine.executor.stream import SecretRedactor, pump_stream


class DockerError(RuntimeError):
    """Raised when Docker cannot run a build container."""


type LogCallback = Callable[[str, str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class CacheMount:
    """Host-to-container cache mount."""

    host_path: Path
    container_path: str


@dataclass(frozen=True, slots=True)
class ImageManifestEntry:
    """One builder image entry resolved from the manifest contract."""

    tag: str
    digest: str
    frameworks: tuple[str, ...]

    @property
    def reference(self) -> str:
        """Return a pinned Docker reference."""

        return f"{self.tag}@{self.digest}"


@dataclass(frozen=True, slots=True)
class DockerRunSpec:
    """Inputs for one hardened `docker run` invocation."""

    image: str
    project_root: Path
    command: str
    config: EngineConfig
    network_guard: DockerNetworkGuard
    source_root: Path | None = None
    output_root: Path | None = None
    build_manifest: Mapping[str, object] | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    cache_mounts: Sequence[CacheMount] = ()
    secret_env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ContainerResult:
    """Completed Docker process result."""

    exit_code: int
    timed_out: bool = False
    cancelled: bool = False


def load_image_manifest(path: Path | str) -> dict[str, ImageManifestEntry]:
    """Load and minimally validate a builder image manifest."""

    with Path(path).open(encoding="utf-8") as handle:
        decoded = json.load(handle)
    if not isinstance(decoded, dict):
        raise DockerError("Image manifest must be a JSON object")
    images = decoded.get("images")
    if not isinstance(images, dict) or not images:
        raise DockerError("Image manifest must contain images")

    result: dict[str, ImageManifestEntry] = {}
    for key, raw_entry in images.items():
        if not isinstance(key, str) or not isinstance(raw_entry, dict):
            raise DockerError("Image manifest entries must be objects")
        tag = raw_entry.get("tag")
        digest = raw_entry.get("digest")
        frameworks = raw_entry.get("frameworks")
        if not isinstance(tag, str) or not tag:
            raise DockerError(f"Image manifest entry {key} is missing tag")
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise DockerError(f"Image manifest entry {key} is missing digest")
        if not isinstance(frameworks, list) or not all(
            isinstance(item, str) and item for item in frameworks
        ):
            raise DockerError(f"Image manifest entry {key} is missing frameworks")
        result[key] = ImageManifestEntry(tag=tag, digest=digest, frameworks=tuple(frameworks))
    return result


def resolve_image_reference(
    image: str,
    *,
    manifest: Mapping[str, ImageManifestEntry] | None = None,
) -> str:
    """Resolve a configured image to a pinned manifest reference when available."""

    if manifest is None:
        return image
    if image in manifest:
        return manifest[image].reference
    for entry in manifest.values():
        if image in {entry.tag, entry.reference}:
            return entry.reference
    raise DockerError(f"Image {image} is not present in the builder manifest")


def pull_image(image: str, *, docker_bin: str = "docker", timeout_seconds: float = 300.0) -> None:
    """Pull the selected builder image by tag or digest."""

    result = subprocess.run(  # nosec B603
        [docker_bin, "pull", image],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise DockerError(f"Docker image pull failed for {image}: {detail}")


def docker_run_args(
    spec: DockerRunSpec,
    *,
    docker_bin: str = "docker",
    env_file_path: Path | None = None,
    build_manifest_path: Path | None = None,
) -> list[str]:
    """Build the hardened `docker run` argv for a build command."""

    tmpfs_mount = "/tmp" + ":rw,noexec,nosuid,size=512m"  # nosec B108
    args = [
        docker_bin,
        "run",
        "--rm",
        "--memory",
        spec.config.container_memory,
        "--memory-swap",
        spec.config.container_memory,
        "--cpus",
        str(spec.config.container_cpus),
        "--pids-limit",
        "1024",
        "--read-only",
        "--user",
        "1000:1000",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        tmpfs_mount,
        *spec.network_guard.docker_args(),
        "--workdir",
        "/workspace/src" if spec.build_manifest is not None else "/workspace",
    ]
    if spec.build_manifest is not None:
        if build_manifest_path is None:
            raise DockerError("build_manifest_path is required when build_manifest is set")
        output_root = spec.output_root or spec.project_root.parent / "out"
        output_root.mkdir(parents=True, exist_ok=True)
        args.extend(
            [
                "--volume",
                f"{build_manifest_path.resolve()}:/build/manifest.json:ro",
                "--volume",
                f"{(spec.source_root or spec.project_root).resolve()}:/workspace/src:rw",
                "--volume",
                f"{output_root.resolve()}:/workspace/out:rw",
            ]
        )
    else:
        args.extend(["--volume", f"{spec.project_root.resolve()}:/workspace:rw"])
    for mount in spec.cache_mounts:
        mount.host_path.mkdir(parents=True, exist_ok=True)
        args.extend(["--volume", f"{mount.host_path.resolve()}:{mount.container_path}:rw"])
    for key, value in sorted(spec.environment.items()):
        args.extend(["--env", f"{key}={value}"])
    if env_file_path is not None:
        args.extend(["--env-file", str(env_file_path)])
    if spec.build_manifest is not None:
        args.append(spec.image)
    else:
        args.extend([spec.image, "sh", "-c", spec.command])
    return args


def _validate_secret_env_key(key: str) -> None:
    if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
        raise DockerError(f"Invalid secret env var name: {key!r}")


def _format_env_file(secret_env: Mapping[str, str]) -> str:
    lines: list[str] = []
    for key, value in sorted(secret_env.items()):
        _validate_secret_env_key(key)
        if "\n" in value or "\r" in value:
            raise DockerError(f"Secret env var {key!r} contains a newline")
        lines.append(f"{key}={value}")
    return "\n".join(lines) + ("\n" if lines else "")


@contextlib.contextmanager
def _materialize_env_file(secret_env: Mapping[str, str]) -> Iterator[Path | None]:
    if not secret_env:
        yield None
        return
    fd, raw_path = tempfile.mkstemp(prefix="build-engine-env-", suffix=".env")
    path = Path(raw_path)
    try:
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_format_env_file(secret_env))
        yield path
    finally:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


@contextlib.contextmanager
def _materialize_build_manifest(
    build_manifest: Mapping[str, object] | None,
) -> Iterator[Path | None]:
    if build_manifest is None:
        yield None
        return
    fd, raw_path = tempfile.mkstemp(prefix="build-engine-manifest-", suffix=".json")
    path = Path(raw_path)
    try:
        os.chmod(path, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(build_manifest, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
        yield path
    finally:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


async def run_container(
    spec: DockerRunSpec,
    *,
    publish_log: LogCallback,
    cancel_event: asyncio.Event | None = None,
    docker_bin: str = "docker",
) -> ContainerResult:
    """Run a Docker container with timeout and SIGTERM->SIGKILL cancellation."""

    with (
        _materialize_env_file(spec.secret_env) as env_file_path,
        _materialize_build_manifest(spec.build_manifest) as build_manifest_path,
    ):
        process = await asyncio.create_subprocess_exec(
            *docker_run_args(
                spec,
                docker_bin=docker_bin,
                env_file_path=env_file_path,
                build_manifest_path=build_manifest_path,
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if process.stdout is None or process.stderr is None:
            raise DockerError("Docker subprocess did not expose stdout/stderr")

        redactor = SecretRedactor(spec.secret_env.values())
        stdout_task = asyncio.create_task(
            pump_stream(process.stdout, stream="stdout", redactor=redactor, publish=publish_log)
        )
        stderr_task = asyncio.create_task(
            pump_stream(process.stderr, stream="stderr", redactor=redactor, publish=publish_log)
        )
        try:
            result = await _wait_for_process(process, spec=spec, cancel_event=cancel_event)
        finally:
            await asyncio.gather(stdout_task, stderr_task)
    return result


async def _wait_for_process(
    process: asyncio.subprocess.Process,
    *,
    spec: DockerRunSpec,
    cancel_event: asyncio.Event | None,
) -> ContainerResult:
    deadline = asyncio.get_running_loop().time() + spec.config.build_timeout_seconds
    while True:
        if cancel_event is not None and cancel_event.is_set():
            await _terminate_then_kill(process, grace_seconds=spec.config.sigterm_grace_seconds)
            return ContainerResult(exit_code=process.returncode or -1, cancelled=True)
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            await _terminate_then_kill(process, grace_seconds=spec.config.sigterm_grace_seconds)
            return ContainerResult(exit_code=process.returncode or -1, timed_out=True)
        try:
            exit_code = await asyncio.wait_for(process.wait(), timeout=min(0.25, remaining))
        except TimeoutError:
            continue
        return ContainerResult(exit_code=exit_code)


async def _terminate_then_kill(
    process: asyncio.subprocess.Process,
    *,
    grace_seconds: float,
) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
    except TimeoutError:
        process.kill()
        await process.wait()

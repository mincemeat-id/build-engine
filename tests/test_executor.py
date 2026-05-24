"""Docker executor component tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from build_engine.config import EngineConfig
from build_engine.executor.artifact import ArtifactUploadClient, package_output
from build_engine.executor.cache import reset_cache, site_cache
from build_engine.executor.docker_runner import (
    CacheMount,
    DockerError,
    DockerRunSpec,
    _format_env_file,
    _materialize_env_file,
    docker_run_args,
    load_image_manifest,
    pull_image,
    resolve_image_reference,
    run_container,
)
from build_engine.executor.network import (
    DockerNetworkGuard,
    ensure_network_guard,
    normalize_blocklist,
)
from build_engine.executor.stream import SecretRedactor, _frame_chunks
from build_engine.executor.workspace import (
    WorkspaceError,
    cleanup_workspace,
    create_workspace,
    download_source,
    extract_source,
)


def test_source_download_verifies_sha256_and_extracts_safely(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "package.json").write_text('{"scripts":{"build":"vite build"}}')
    (source_dir / "index.html").write_text("hello")
    archive = tmp_path / "source.tar.gz"
    _create_archive(archive, source_dir)
    expected = _sha256(archive)
    destination = tmp_path / "downloaded.tar.gz"
    extracted = tmp_path / "extracted"

    download_source(archive.as_uri(), destination, expected_sha256=expected)
    extract_source(destination, extracted)

    assert (extracted / "index.html").read_text() == "hello"


def test_extract_source_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, mode="w:gz") as tar:
        info = tarfile.TarInfo("../evil.txt")
        data = b"bad"
        info.size = len(data)
        tar.addfile(info, fileobj=_BytesReader(data))

    with pytest.raises(WorkspaceError, match="unsafe path"):
        extract_source(archive, tmp_path / "out")


def test_package_output_validates_and_hashes_artifact(tmp_path: Path) -> None:
    project = tmp_path / "project"
    output = project / "dist"
    output.mkdir(parents=True)
    (output / "index.html").write_text("<h1>Mincemeat</h1>")

    artifact = package_output(
        project_root=project,
        output_dir="dist",
        destination=tmp_path / "artifact.tar.gz",
        max_bytes=1_000_000,
    )

    assert artifact.size_bytes > 0
    assert artifact.sha256 == _sha256(artifact.path)
    with tarfile.open(artifact.path, mode="r:gz") as tar:
        assert "index.html" in tar.getnames()


def test_docker_run_args_include_resource_and_hardening_flags(tmp_path: Path) -> None:
    spec = DockerRunSpec(
        image="node:22",
        project_root=tmp_path,
        command="npm ci && npm run build",
        config=EngineConfig(state_dir=tmp_path),
        network_guard=DockerNetworkGuard(name="build-engine-guard"),
    )

    args = docker_run_args(spec)

    assert "--memory" in args
    assert "--cpus" in args
    assert "--pids-limit" in args
    assert "--read-only" in args
    assert "--cap-drop" in args
    assert "--security-opt" in args
    assert "no-new-privileges" in args
    assert "--network" in args
    assert args[-4:] == ["node:22", "sh", "-c", "npm ci && npm run build"]
    assert "-lc" not in args
    assert "/var/run/docker.sock" not in " ".join(args)


def test_docker_run_args_mount_final_image_entrypoint_contract(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    output_root = tmp_path / "out"
    manifest_path = tmp_path / "manifest.json"
    spec = DockerRunSpec(
        image="node:22@sha256:" + ("a" * 64),
        project_root=source_root,
        command="npm ci && npm run build",
        config=EngineConfig(state_dir=tmp_path),
        network_guard=DockerNetworkGuard(name="none"),
        source_root=source_root,
        output_root=output_root,
        build_manifest={
            "framework": "vite",
            "package_manager": "npm",
            "build_command": "npm ci && npm run build",
            "output_dir": "dist",
        },
        cache_mounts=(CacheMount(host_path=tmp_path / "cache", container_path="/cache"),),
    )

    args = docker_run_args(spec, build_manifest_path=manifest_path)

    assert f"{manifest_path.resolve()}:/build/manifest.json:ro" in args
    assert f"{source_root.resolve()}:/workspace/src:rw" in args
    assert f"{output_root.resolve()}:/workspace/out:rw" in args
    assert f"{(tmp_path / 'cache').resolve()}:/cache:rw" in args
    assert args[-1] == spec.image
    assert "sh" not in args[-4:]


def test_network_guard_creates_bridge_and_installs_drop_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    inspect_payload = json.dumps(
        [
            {
                "Name": "build-engine-guard",
                "Id": "abcdef1234567890",
                "Options": {"com.docker.network.bridge.name": "be-guard0"},
                "IPAM": {"Config": [{"Gateway": "172.31.255.1"}]},
            }
        ]
    )
    inspect_calls = 0

    def fake_which(binary: str) -> str:
        return f"/usr/sbin/{binary}"

    def fake_run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal inspect_calls
        commands.append(command)
        if command[:3] == ["docker", "network", "inspect"]:
            inspect_calls += 1
            if inspect_calls == 1:
                return subprocess.CompletedProcess(command, 1, "", "not found")
            return subprocess.CompletedProcess(command, 0, inspect_payload, "")
        if command[:3] == ["iptables", "-C", "DOCKER-USER"]:
            return subprocess.CompletedProcess(command, 1, "", "missing")
        if command[:3] == ["iptables", "-C", "BUILD_ENGINE_GUARD"]:
            return subprocess.CompletedProcess(command, 1, "", "missing")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("build_engine.executor.network.shutil.which", fake_which)
    monkeypatch.setattr("build_engine.executor.network.subprocess.run", fake_run)

    guard = ensure_network_guard(blocklist=("203.0.113.0/24",))

    assert guard.name == "build-engine-guard"
    assert guard.bridge_name == "be-guard0"
    assert guard.gateway == "172.31.255.1"
    assert commands[1][:3] == ["docker", "network", "create"]
    expected_commands = (
        ["iptables", "-I", "DOCKER-USER", "1", "-i", "be-guard0", "-j", "BUILD_ENGINE_GUARD"],
        ["iptables", "-I", "BUILD_ENGINE_GUARD", "1", "-d", "169.254.0.0/16", "-j", "DROP"],
        ["iptables", "-I", "BUILD_ENGINE_GUARD", "1", "-d", "203.0.113.0/24", "-j", "DROP"],
        ["iptables", "-I", "BUILD_ENGINE_GUARD", "1", "-d", "172.31.255.1/32", "-j", "DROP"],
    )
    for expected in expected_commands:
        assert expected in commands


def test_network_blocklist_normalization_rejects_invalid_entries() -> None:
    assert normalize_blocklist(("203.0.113.7", "203.0.113.0/24")) == (
        "203.0.113.7/32",
        "203.0.113.0/24",
    )
    with pytest.raises(RuntimeError, match="Invalid network blocklist"):
        normalize_blocklist(("not-a-cidr",))


def test_image_manifest_resolves_tag_to_digest_reference(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    digest = "sha256:" + "a" * 64
    manifest_path.write_text(
        json.dumps(
            {
                "version": "1.0.0",
                "generated_at": "2026-05-21T00:00:00Z",
                "engine_compat": {"proto_min": 1, "proto_max": 1, "engine_min": "0.1.0"},
                "images": {
                    "node22": {
                        "tag": "node:22",
                        "digest": digest,
                        "frameworks": ["vite"],
                    }
                },
            }
        )
    )

    manifest = load_image_manifest(manifest_path)

    assert resolve_image_reference("node:22", manifest=manifest) == f"node:22@{digest}"


def test_secret_redactor_replaces_exact_secret_values() -> None:
    redactor = SecretRedactor(["abc123", "token"])

    assert redactor.redact("token=abc123") == "[REDACTED]=[REDACTED]"


def test_docker_run_args_uses_env_file_and_keeps_secrets_off_argv(tmp_path: Path) -> None:
    spec = DockerRunSpec(
        image="node:22",
        project_root=tmp_path,
        command="env",
        config=EngineConfig(state_dir=tmp_path),
        network_guard=DockerNetworkGuard(name="build-engine-guard"),
        secret_env={"MY_SECRET": "s3cret-value", "API_TOKEN": "tok-abc"},
    )
    env_file_path = tmp_path / "env-file"

    args = docker_run_args(spec, env_file_path=env_file_path)

    joined = " ".join(args)
    assert "--env-file" in args
    assert str(env_file_path) in args
    assert "s3cret-value" not in joined
    assert "tok-abc" not in joined


def test_format_env_file_writes_sorted_key_equals_value_lines() -> None:
    contents = _format_env_file({"B_TOKEN": "two", "A_TOKEN": "one"})

    assert contents == "A_TOKEN=one\nB_TOKEN=two\n"


def test_format_env_file_rejects_invalid_keys_and_newlines() -> None:
    with pytest.raises(DockerError, match="Invalid secret env var name"):
        _format_env_file({"1BAD": "value"})
    with pytest.raises(DockerError, match="contains a newline"):
        _format_env_file({"OK": "first\nsecond"})


def test_materialize_env_file_writes_secure_temp_and_cleans_up() -> None:
    with _materialize_env_file({"FOO": "bar"}) as path:
        assert path is not None
        assert path.exists()
        assert path.read_text(encoding="utf-8") == "FOO=bar\n"
        assert (path.stat().st_mode & 0o777) == 0o600
    assert path is not None
    assert not path.exists()


def test_materialize_env_file_yields_none_when_empty() -> None:
    with _materialize_env_file({}) as path:
        assert path is None


def test_run_container_injects_secret_env_and_redacts_log_stream(tmp_path: Path) -> None:
    asyncio.run(_run_container_with_fake_docker(tmp_path))


async def _run_container_with_fake_docker(tmp_path: Path) -> None:
    fake_docker = tmp_path / "fake-docker"
    fake_docker.write_text(
        "#!/bin/sh\n"
        "# Parse out --env-file path then cat it so the secret reaches the\n"
        "# captured stdout, exercising the redactor end-to-end.\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in\n'
        '    --env-file) shift; cat "$1"; shift ;;\n'
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        "exit 0\n"
    )
    fake_docker.chmod(0o755)
    logs: list[tuple[str, str]] = []

    async def publish(stream: str, data: str) -> None:
        logs.append((stream, data))

    spec = DockerRunSpec(
        image="busybox:latest",
        project_root=tmp_path,
        command="env",
        config=EngineConfig(
            state_dir=tmp_path / "state",
            build_timeout_seconds=10,
            sigterm_grace_seconds=1,
        ),
        network_guard=DockerNetworkGuard(name="none"),
        secret_env={"MY_SECRET": "super-secret-value"},
    )

    result = await run_container(spec, publish_log=publish, docker_bin=str(fake_docker))

    assert result.exit_code == 0
    stdout = "".join(data for stream, data in logs if stream == "stdout")
    assert "MY_SECRET=" in stdout
    assert "super-secret-value" not in stdout
    assert "[REDACTED]" in stdout


def test_log_frame_chunks_stay_under_protocol_byte_limit() -> None:
    frames = _frame_chunks(("é" * 40_000) + ("x" * 70_000))

    assert len(frames) > 1
    assert all(len(frame.encode("utf-8")) <= 65_536 for frame in frames)


def test_workspace_cleanup_removes_success_and_prunes_failed_retention(tmp_path: Path) -> None:
    successful = create_workspace(tmp_path, "success")
    (successful.source_root / "index.html").write_text("ok", encoding="utf-8")

    cleanup_workspace(successful)

    assert not successful.attempt_dir.exists()

    for index in range(3):
        failed = create_workspace(tmp_path, f"failed-{index}")
        cleanup_workspace(failed, retain_failed=True, failed_keep=2)

    retained = sorted(path.name for path in (tmp_path / "jobs").iterdir())
    assert len(retained) == 2
    assert "failed-0" not in retained


def test_workspace_prune_uses_marker_mtime_not_attempt_dir_mtime(tmp_path: Path) -> None:
    """Retain the most recently *failed* workspaces even when their attempt dirs
    were touched out of order (e.g. by late nested writes)."""

    failures: list[Path] = []
    for index in range(3):
        failed = create_workspace(tmp_path, f"failed-{index}")
        cleanup_workspace(failed, retain_failed=True, failed_keep=10)
        failures.append(failed.attempt_dir)

    # Make the FAILED marker mtimes reflect the failure order:
    # failed-0 oldest, failed-2 newest.
    for offset, attempt_dir in enumerate(failures):
        marker = attempt_dir / "FAILED"
        ts = 1_700_000_000 + offset * 100
        os.utime(marker, (ts, ts))

    # Now simulate a stale nested write into the *oldest* attempt dir that
    # would bump its directory mtime above the newer attempts'. The marker
    # mtime must still drive pruning order.
    stale_child = failures[0] / "workspace" / "stale.txt"
    stale_child.parent.mkdir(parents=True, exist_ok=True)
    stale_child.write_text("noise", encoding="utf-8")
    now = 1_800_000_000
    os.utime(failures[0], (now, now))

    # Trigger a prune that keeps only the two newest failures by marker mtime.
    trigger = create_workspace(tmp_path, "failed-trigger")
    trigger_marker_ts = 1_700_000_000 + 3 * 100
    cleanup_workspace(trigger, retain_failed=True, failed_keep=2)
    os.utime(trigger.attempt_dir / "FAILED", (trigger_marker_ts, trigger_marker_ts))
    # Re-run prune with the marker timestamp set so ordering is deterministic.
    cleanup_workspace(trigger, retain_failed=True, failed_keep=2)

    retained = {path.name for path in (tmp_path / "jobs").iterdir()}
    assert "failed-0" not in retained
    assert "failed-1" not in retained
    assert "failed-2" in retained
    assert "failed-trigger" in retained


def test_site_cache_maps_package_manager_mounts_and_reset(tmp_path: Path) -> None:
    cache = site_cache(tmp_path, "site-a", "pnpm")

    mounts = cache.mounts()

    assert mounts[0].host_path == tmp_path / "cache" / "site-a"
    assert mounts[0].container_path == "/cache"
    assert mounts[0].host_path.exists()

    reset_cache(tmp_path, site_id="site-a")

    assert not (tmp_path / "cache" / "site-a").exists()


def test_artifact_upload_client_requests_url_and_puts_bytes(tmp_path: Path) -> None:
    artifact_path = tmp_path / "artifact.tar.gz"
    artifact_path.write_bytes(b"artifact-bytes")
    server = _UploadServer()
    server.start()
    try:
        client = ArtifactUploadClient(
            backend_url=server.base_url,
            session_jwt="session-token",
            timeout_seconds=5,
        )
        artifact = package_output(
            project_root=_project_with_output(tmp_path),
            output_dir="dist",
            destination=artifact_path,
            max_bytes=1_000_000,
        )

        ticket = client.request_upload_url(
            build_job_id="22222222-2222-2222-2222-222222222222",
            attempt_id="33333333-3333-3333-3333-333333333333",
            artifact=artifact,
        )
        client.upload(ticket, artifact)

        assert server.upload_request["sha256"] == artifact.sha256
        assert server.upload_request["size_bytes"] == artifact.size_bytes
        assert server.put_body == artifact.path.read_bytes()
        assert server.auth_header == "Bearer session-token"
    finally:
        server.stop()


@pytest.mark.skipif(
    os.environ.get("BUILD_ENGINE_DOCKER_TESTS") != "1",
    reason="real Docker smoke is opt-in to avoid pulling images during default verify",
)
def test_docker_runner_integration_smoke(tmp_path: Path) -> None:
    asyncio.run(_docker_runner_integration_smoke(tmp_path))


async def _docker_runner_integration_smoke(tmp_path: Path) -> None:
    tmp_path.chmod(0o777)
    logs: list[tuple[str, str]] = []

    async def publish(stream: str, data: str) -> None:
        logs.append((stream, data))

    await asyncio.to_thread(pull_image, "busybox:latest", timeout_seconds=120)
    result = await run_container(
        DockerRunSpec(
            image="busybox:latest",
            project_root=tmp_path,
            command="mkdir -p dist && echo ok > dist/index.html",
            config=EngineConfig(
                state_dir=tmp_path / "state",
                build_timeout_seconds=30,
                sigterm_grace_seconds=1,
            ),
            network_guard=DockerNetworkGuard(name="none"),
        ),
        publish_log=publish,
    )

    assert result.exit_code == 0
    assert (tmp_path / "dist" / "index.html").read_text().strip() == "ok"


@pytest.mark.skipif(
    os.environ.get("BUILD_ENGINE_DOCKER_TESTS") != "1",
    reason="real Docker smoke is opt-in to avoid pulling images during default verify",
)
def test_network_guard_blocks_metadata_endpoint_with_real_docker(tmp_path: Path) -> None:
    del tmp_path
    guard = ensure_network_guard()
    image = "curlimages/curl:8.10.1"
    pull_image(image, timeout_seconds=120)

    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            guard.name,
            "--entrypoint",
            "curl",
            image,
            "-fsS",
            "--connect-timeout",
            "1",
            "--max-time",
            "2",
            "http://169.254.169.254/",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0


class _BytesReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.position = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.data) - self.position
        chunk = self.data[self.position : self.position + size]
        self.position += len(chunk)
        return chunk


class _UploadServer:
    def __init__(self) -> None:
        handler = _handler_for(self)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.upload_request: dict[str, Any] = {}
        self.put_body = b""
        self.auth_header: str | None = None

    @property
    def base_url(self) -> str:
        address = self.httpd.server_address
        host = str(address[0])
        port = int(address[1])
        return f"http://{host}:{port}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()


def _handler_for(server: _UploadServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            server.auth_header = self.headers.get("Authorization")
            server.upload_request = json.loads(self.rfile.read(length).decode("utf-8"))
            body = json.dumps(
                {
                    "upload_url": f"{server.base_url}/upload",
                    "expires_at": "2026-05-21T00:00:00Z",
                    "storage_key": "artifacts/test.tar.gz",
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_PUT(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            server.put_body = self.rfile.read(length)
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return Handler


def _create_archive(destination: Path, source_dir: Path) -> None:
    with tarfile.open(destination, mode="w:gz") as tar:
        for path in sorted(source_dir.rglob("*")):
            tar.add(path, arcname=path.relative_to(source_dir))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _project_with_output(tmp_path: Path) -> Path:
    project = tmp_path / "upload-project"
    output = project / "dist"
    output.mkdir(parents=True)
    (output / "index.html").write_text("ok")
    return project

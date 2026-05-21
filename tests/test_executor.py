"""Docker executor component tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
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
    DockerRunSpec,
    docker_run_args,
    load_image_manifest,
    pull_image,
    resolve_image_reference,
    run_container,
)
from build_engine.executor.network import DockerNetworkGuard
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
    assert "/var/run/docker.sock" not in " ".join(args)


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


def test_site_cache_maps_package_manager_mounts_and_reset(tmp_path: Path) -> None:
    cache = site_cache(tmp_path, "site-a", "pnpm")

    mounts = cache.mounts()

    assert mounts[0].host_path == tmp_path / "cache" / "site-a" / "pnpm" / "store"
    assert mounts[0].container_path == "/home/node/.local/share/pnpm/store"
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

"""Stage 8 end-to-end hardening drills."""

from __future__ import annotations

import asyncio
import hashlib
import json
import tarfile
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from build_engine.agent import job_loop
from build_engine.agent.job_loop import BuildExecutionError, execute_job
from build_engine.config import EngineConfig, EngineCredentials
from build_engine.executor.cache import site_cache
from build_engine.executor.docker_runner import ContainerResult, DockerRunSpec
from build_engine.executor.network import DockerNetworkGuard
from build_engine.queue.handlers import SQLiteCommandHandlers
from build_engine.queue.leases import acquire_queue_lease
from build_engine.queue.store import SQLiteQueueStore

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sites"
BUILD_JOB_ID = "22222222-2222-2222-2222-222222222222"
ATTEMPT_ID = "33333333-3333-3333-3333-333333333333"


@pytest.mark.parametrize(
    ("fixture", "output_dir"),
    (
        ("astro-blog", "dist"),
        ("vite-vanilla", "dist"),
        ("eleventy-blog", "_site"),
        ("docusaurus-docs", "build"),
        ("vitepress-docs", ".vitepress/dist"),
        ("vuepress-docs", "dist"),
        ("gatsby-blog", "public"),
        ("hugo-quickstart", "public"),
        ("nextjs-export", "out"),
        ("nuxt-generate", ".output/public"),
        ("sveltekit-static", "build"),
        ("generic-static", "dist"),
    ),
)
def test_v1_ga_fixtures_execute_end_to_end_against_local_coreapp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fixture: str,
    output_dir: str,
) -> None:
    asyncio.run(_run_successful_fixture(monkeypatch, tmp_path, fixture, output_dir))


async def _run_successful_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fixture: str,
    output_dir: str,
) -> None:
    server = _CoreappServer()
    server.start()
    try:
        store, job, publisher, credentials = _prepare_job(
            tmp_path,
            fixture=fixture,
            backend_url=server.base_url,
        )
        _patch_executor(monkeypatch, output_dir=output_dir)

        await execute_job(
            job,
            store=store,
            publisher=publisher,
            config=EngineConfig(state_dir=tmp_path, build_timeout_seconds=10),
            credentials=credentials,
        )

        finished = store.get_job(job.build_job_id, job.attempt_id)
        assert finished is not None
        assert finished.state == "SUCCEEDED"
        assert server.upload_request["sha256"] == server.received_sha256
        assert server.put_body
        assert not (tmp_path / "jobs" / job.attempt_id).exists()
        if fixture == "generic-static":
            assert ("status", {"phase": "OUTPUT_DETECTED", "output_dir": "dist"}) in (
                (event.message_type, event.payload) for event in publisher.events
            )
    finally:
        server.stop()


def test_failure_drills_cover_cancel_timeout_oom_stale_storage_engine_lost_and_cache_reset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asyncio.run(_failure_drills(monkeypatch, tmp_path))


async def _failure_drills(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    await _assert_container_result_maps_to_error(
        monkeypatch,
        tmp_path / "cancel",
        ContainerResult(exit_code=-1, cancelled=True),
        "CANCELLED",
    )
    await _assert_container_result_maps_to_error(
        monkeypatch,
        tmp_path / "timeout",
        ContainerResult(exit_code=-1, timed_out=True),
        "TIMEOUT",
    )
    await _assert_container_result_maps_to_error(
        monkeypatch,
        tmp_path / "oom",
        ContainerResult(exit_code=137),
        "EXEC_OOM",
    )
    await _assert_storage_failure_maps_to_infra(monkeypatch, tmp_path / "storage")
    await _assert_stale_attempt_cannot_succeed(monkeypatch, tmp_path / "stale")
    _assert_engine_lost_lease_recovers(tmp_path / "engine-lost")
    await _assert_cache_reset_drill_wipes_scope(tmp_path / "cache-reset")


async def _assert_container_result_maps_to_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    result: ContainerResult,
    error_code: str,
) -> None:
    server = _CoreappServer()
    server.start()
    try:
        store, job, publisher, credentials = _prepare_job(
            tmp_path,
            fixture="vite-vanilla",
            backend_url=server.base_url,
        )
        _patch_executor(monkeypatch, output_dir="dist", result=result)

        with pytest.raises(BuildExecutionError) as raised:
            await execute_job(
                job,
                store=store,
                publisher=publisher,
                config=EngineConfig(state_dir=tmp_path, build_timeout_seconds=10),
                credentials=credentials,
            )

        assert raised.value.error_code == error_code
        assert not server.put_body
    finally:
        server.stop()


async def _assert_storage_failure_maps_to_infra(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    server = _CoreappServer(fail_put=True)
    server.start()
    try:
        store, job, publisher, credentials = _prepare_job(
            tmp_path,
            fixture="vite-vanilla",
            backend_url=server.base_url,
        )
        _patch_executor(monkeypatch, output_dir="dist")

        with pytest.raises(BuildExecutionError) as raised:
            await execute_job(
                job,
                store=store,
                publisher=publisher,
                config=EngineConfig(state_dir=tmp_path, build_timeout_seconds=10),
                credentials=credentials,
            )

        assert raised.value.error_class == "EXEC_INFRA"
        assert raised.value.error_code == "STORAGE_FAILURE"
    finally:
        server.stop()


async def _assert_stale_attempt_cannot_succeed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    server = _CoreappServer()
    server.start()
    try:
        store, job, publisher, credentials = _prepare_job(
            tmp_path,
            fixture="vite-vanilla",
            backend_url=server.base_url,
        )
        newer = job.payload | {"attempt_id": "44444444-4444-4444-4444-444444444444"}
        store.enqueue(newer)
        _patch_executor(monkeypatch, output_dir="dist")

        with pytest.raises(BuildExecutionError) as raised:
            await execute_job(
                job,
                store=store,
                publisher=publisher,
                config=EngineConfig(state_dir=tmp_path, build_timeout_seconds=10),
                credentials=credentials,
            )

        assert raised.value.error_code == "STALE_ATTEMPT"
        assert not server.put_body
    finally:
        server.stop()


def _assert_engine_lost_lease_recovers(tmp_path: Path) -> None:
    store = SQLiteQueueStore(tmp_path / "queue.sqlite")
    store.initialize()
    payload = _payload(tmp_path, fixture="vite-vanilla", backend_url="http://127.0.0.1")
    store.enqueue(payload)

    first = acquire_queue_lease(store, owner="engine-a", visibility_timeout_seconds=-1)
    recovered = acquire_queue_lease(store, owner="engine-b", visibility_timeout_seconds=30)

    assert first is not None
    assert recovered is not None
    assert recovered.job.lease_owner == "engine-b"
    assert recovered.job.attempts == 2


async def _assert_cache_reset_drill_wipes_scope(tmp_path: Path) -> None:
    store = SQLiteQueueStore(tmp_path / "queue.sqlite")
    store.initialize()
    cache = site_cache(tmp_path, "site-a", "npm")
    cache.mounts()[0].host_path.joinpath("entry").write_text("cached", encoding="utf-8")
    handlers = SQLiteCommandHandlers(store)

    result = await handlers.cache_reset({"site_id": "site-a"})

    assert result.state == "WIPED"
    assert not cache.root.exists()


def _patch_executor(
    monkeypatch: pytest.MonkeyPatch,
    *,
    output_dir: str,
    result: ContainerResult | None = None,
) -> None:
    def fake_pull_image(image: str) -> None:
        assert image

    def fake_network_guard() -> DockerNetworkGuard:
        return DockerNetworkGuard(name="none")

    async def fake_run_container(
        spec: DockerRunSpec,
        *,
        publish_log: Callable[[str, str], Awaitable[None]],
        cancel_event: asyncio.Event | None = None,
    ) -> ContainerResult:
        del cancel_event
        await publish_log("stdout", "fixture build complete")
        if result is not None and result.exit_code != 0:
            return result
        output = spec.project_root / output_dir
        output.mkdir(parents=True, exist_ok=True)
        (output / "index.html").write_text("<!doctype html><title>ok</title>", encoding="utf-8")
        return result or ContainerResult(exit_code=0)

    monkeypatch.setattr(job_loop, "pull_image", fake_pull_image)
    monkeypatch.setattr(job_loop, "ensure_network_guard", fake_network_guard)
    monkeypatch.setattr(job_loop, "run_container", fake_run_container)


def _prepare_job(
    tmp_path: Path,
    *,
    fixture: str,
    backend_url: str,
) -> tuple[SQLiteQueueStore, Any, _CapturePublisher, EngineCredentials]:
    store = SQLiteQueueStore(tmp_path / "queue.sqlite")
    store.initialize()
    payload = _payload(tmp_path, fixture=fixture, backend_url=backend_url)
    job = store.enqueue(payload).job
    credentials = EngineCredentials(
        engine_id="11111111-1111-1111-1111-111111111111",
        engine_secret="secret",
        session_jwt="session-token",
        session_jwt_expires_at="2030-01-01T00:00:00+00:00",
        backend_url=backend_url,
        name="test-engine",
    )
    return store, job, _CapturePublisher(), credentials


def _payload(tmp_path: Path, *, fixture: str, backend_url: str) -> dict[str, str]:
    archive = tmp_path / f"{fixture}.tar.gz"
    _create_archive(archive, FIXTURES / fixture)
    return {
        "build_job_id": BUILD_JOB_ID,
        "attempt_id": ATTEMPT_ID,
        "site_id": "site-a",
        "source_download_url": archive.as_uri(),
        "source_sha256": _sha256(archive),
        "backend_url": backend_url,
    }


def _create_archive(destination: Path, source_dir: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, mode="w:gz") as tar:
        for path in sorted(source_dir.rglob("*")):
            tar.add(path, arcname=path.relative_to(source_dir))


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


@dataclass(frozen=True, slots=True)
class _PublishedEvent:
    message_type: str
    payload: dict[str, Any]
    build_job_id: str
    attempt_id: str


@dataclass(slots=True)
class _CapturePublisher:
    events: list[_PublishedEvent]

    def __init__(self) -> None:
        self.events = []

    async def publish_attempt_event(
        self,
        message_type: str,
        payload: dict[str, Any],
        *,
        build_job_id: str,
        attempt_id: str,
    ) -> object:
        self.events.append(
            _PublishedEvent(
                message_type=message_type,
                payload=payload,
                build_job_id=build_job_id,
                attempt_id=attempt_id,
            )
        )
        return None


class _CoreappServer:
    def __init__(self, *, fail_put: bool = False) -> None:
        handler = _handler_for(self)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.fail_put = fail_put
        self.upload_request: dict[str, Any] = {}
        self.put_body = b""
        self.received_sha256 = ""

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


def _handler_for(server: _CoreappServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            server.upload_request = json.loads(self.rfile.read(length).decode("utf-8"))
            body = json.dumps(
                {
                    "upload_url": f"{server.base_url}/upload",
                    "expires_at": "2030-01-01T00:00:00Z",
                    "storage_key": "stage8/artifact.tar.gz",
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_PUT(self) -> None:
            if server.fail_put:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b"storage unavailable")
                return
            length = int(self.headers.get("Content-Length", "0"))
            server.put_body = self.rfile.read(length)
            server.received_sha256 = hashlib.sha256(server.put_body).hexdigest()
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return Handler

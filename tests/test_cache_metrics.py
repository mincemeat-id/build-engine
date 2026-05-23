"""Cache lifecycle and metrics tests."""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from build_engine.config import EngineCredentials
from build_engine.executor.cache import (
    cache_size_bytes,
    prepare_site_cache,
    prune_cache,
)
from build_engine.metrics.collector import MetricsCollector
from build_engine.metrics.reporter import MetricsReporter, write_textfile_metrics


def test_prepare_site_cache_reports_hit_and_invalidates_changed_lockfile(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    lockfile = project / "package-lock.json"
    lockfile.write_text('{"lockfileVersion":3}', encoding="utf-8")

    first = prepare_site_cache(
        state_dir=tmp_path,
        site_id="site-a",
        package_manager="npm",
        project_root=project,
        enabled=True,
    )
    first.mounts[0].host_path.joinpath("entry").write_text("cached", encoding="utf-8")
    second = prepare_site_cache(
        state_dir=tmp_path,
        site_id="site-a",
        package_manager="npm",
        project_root=project,
        enabled=True,
    )

    lockfile.write_text('{"lockfileVersion":4}', encoding="utf-8")
    third = prepare_site_cache(
        state_dir=tmp_path,
        site_id="site-a",
        package_manager="npm",
        project_root=project,
        enabled=True,
    )

    assert first.event == "MISS"
    assert second.event == "HIT"
    assert third.event == "WIPED"
    assert not (tmp_path / "cache" / "site-a" / "npm" / "_cacache" / "entry").exists()


def test_cache_disable_reenable_wipes_before_reuse(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'", encoding="utf-8")
    enabled = prepare_site_cache(
        state_dir=tmp_path,
        site_id="site-a",
        package_manager="pnpm",
        project_root=project,
        enabled=True,
    )
    enabled.mounts[0].host_path.joinpath("entry").write_text("cached", encoding="utf-8")

    disabled = prepare_site_cache(
        state_dir=tmp_path,
        site_id="site-a",
        package_manager="pnpm",
        project_root=project,
        enabled=False,
    )
    reenabled = prepare_site_cache(
        state_dir=tmp_path,
        site_id="site-a",
        package_manager="pnpm",
        project_root=project,
        enabled=True,
    )

    assert disabled.mounts == ()
    assert disabled.event is None
    assert reenabled.event == "WIPED"
    assert not (tmp_path / "cache" / "site-a" / "pnpm" / "store" / "entry").exists()


def test_prune_cache_removes_expired_sites_and_lru_files(tmp_path: Path) -> None:
    expired = tmp_path / "cache" / "expired"
    oversized = tmp_path / "cache" / "oversized"
    expired.mkdir(parents=True)
    oversized.mkdir(parents=True)
    (expired / "entry").write_bytes(b"x")
    old_file = oversized / "old"
    new_file = oversized / "new"
    old_file.write_bytes(b"x" * 20)
    new_file.write_bytes(b"y" * 20)
    old_time = (datetime.now(UTC) - timedelta(days=40)).timestamp()
    recent_time = datetime.now(UTC).timestamp()
    os.utime(expired, (old_time, old_time))
    os.utime(old_file, (old_time, old_time))
    os.utime(new_file, (recent_time, recent_time))

    pruned = prune_cache(tmp_path, site_max_bytes=25, ttl_days=30)

    assert expired in pruned
    assert not expired.exists()
    assert not old_file.exists()
    assert new_file.exists()
    assert cache_size_bytes(tmp_path) <= 25


def test_metrics_collector_rolls_up_cache_ratio_and_heartbeat() -> None:
    collector = MetricsCollector(workers_total=2)

    collector.job_started()
    collector.cache_event("HIT")
    collector.cache_event("MISS")
    collector.docker_error()
    collector.uplink_reconnect()
    snapshot = collector.snapshot(queue_depth=3, cache_size_bytes=42)
    collector.job_finished(completed=True)

    assert snapshot.workers_busy == 1
    assert snapshot.jobs_running == 1
    assert snapshot.cache_hit_ratio == 0.5
    assert snapshot.docker_errors_total == 1
    assert snapshot.uplink_reconnects_total == 1
    heartbeat = snapshot.to_heartbeat(disk_free_bytes=99)
    assert heartbeat.to_payload()["disk_free_bytes"] == 99


def test_metrics_reporter_posts_openapi_rollup(tmp_path: Path) -> None:
    del tmp_path
    credentials = EngineCredentials(
        engine_id="11111111-1111-1111-1111-111111111111",
        engine_secret="secret",
        session_jwt="session-token",
        session_jwt_expires_at="2030-01-01T00:00:00+00:00",
        backend_url=None,
        name="metrics-test",
    )
    collector = MetricsCollector(workers_total=2)
    snapshot = collector.snapshot(queue_depth=1, cache_size_bytes=123)
    server = _MetricsServer()
    server.start()
    try:
        MetricsReporter(backend_url=server.base_url, credentials=credentials).report(snapshot)
    finally:
        server.stop()

    assert server.body["workers_total"] == 2
    assert server.body["queue_depth"] == 1
    assert server.body["cache_size_bytes"] == 123
    assert server.auth_header == "Bearer session-token"


def test_textfile_metrics_writer_outputs_prometheus_format(tmp_path: Path) -> None:
    collector = MetricsCollector(workers_total=2)
    collector.job_started()
    collector.cache_event("HIT")
    snapshot = collector.snapshot(queue_depth=4, cache_size_bytes=512)

    metrics_path = tmp_path / "metrics.prom"
    write_textfile_metrics(metrics_path, snapshot)

    content = metrics_path.read_text(encoding="utf-8")
    assert "# TYPE build_engine_workers_busy gauge" in content
    assert "build_engine_workers_busy 1" in content
    assert "build_engine_queue_depth 4" in content
    assert "build_engine_cache_size_bytes 512" in content


class _MetricsServer:
    def __init__(self) -> None:
        handler = _handler_for(self)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.body: dict[str, Any] = {}
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


def _handler_for(server: _MetricsServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            server.auth_header = self.headers.get("Authorization")
            server.body = json.loads(self.rfile.read(length).decode("utf-8"))
            self.send_response(202)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return Handler

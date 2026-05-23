"""Metrics rollup reporting to coreapp."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib import error, request

from build_engine.agent.auth import client_headers_for_credentials
from build_engine.config import EngineConfig, EngineCredentials
from build_engine.executor.cache import cache_size_bytes
from build_engine.metrics.collector import MetricsCollector, MetricsSnapshot
from build_engine.queue.store import SQLiteQueueStore


class MetricsReportError(RuntimeError):
    """Raised when a metrics rollup cannot be pushed."""


type Sleep = Callable[[float], Awaitable[object]]

LOG = logging.getLogger(__name__)
METRICS_WARNING_INTERVAL_SECONDS = 60.0


class MetricsReporter:
    """Small stdlib HTTP client for the metrics rollup endpoint."""

    def __init__(
        self,
        *,
        backend_url: str,
        credentials: EngineCredentials,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.backend_url = backend_url.rstrip("/")
        self.credentials = credentials
        self.timeout_seconds = timeout_seconds

    def report(self, snapshot: MetricsSnapshot) -> None:
        """POST one metrics rollup to coreapp."""

        body = json.dumps(snapshot.to_payload(), separators=(",", ":")).encode("utf-8")
        headers = client_headers_for_credentials(self.credentials)
        headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        req = request.Request(
            f"{self.backend_url}/api/v1/build-engines/agent/metrics",
            data=body,
            method="POST",
            headers=headers,
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:  # nosec B310
                response.read()
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise MetricsReportError(f"Metrics report failed: HTTP {exc.code} {detail}") from exc
        except OSError as exc:
            raise MetricsReportError(f"Metrics report failed: {exc}") from exc


async def run_metrics_reporter(
    *,
    config: EngineConfig,
    credentials: EngineCredentials,
    store: SQLiteQueueStore,
    collector: MetricsCollector,
    sleep: Sleep = asyncio.sleep,
) -> None:
    """Push metrics rollups until cancelled."""

    backend_url = credentials.backend_url or config.backend_url
    if backend_url is None:
        raise MetricsReportError("backend_url is missing")
    reporter = MetricsReporter(backend_url=backend_url, credentials=credentials)
    last_warning_at = 0.0
    while True:
        snapshot = collector.snapshot(
            queue_depth=store.queue_depth(),
            cache_size_bytes=cache_size_bytes(config.state_dir),
        )
        write_textfile_metrics(config.state_dir / "metrics.prom", snapshot)
        try:
            await asyncio.to_thread(reporter.report, snapshot)
        except MetricsReportError as exc:
            now = time.monotonic()
            if now - last_warning_at >= METRICS_WARNING_INTERVAL_SECONDS:
                LOG.warning("metrics report failed; will retry", exc_info=exc)
                last_warning_at = now
        await sleep(config.heartbeat_interval_seconds)


def write_textfile_metrics(path: Path, snapshot: MetricsSnapshot) -> None:
    """Write a Prometheus textfile collector snapshot atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# HELP build_engine_workers_busy Build engine workers currently busy.",
        "# TYPE build_engine_workers_busy gauge",
        f"build_engine_workers_busy {snapshot.workers_busy}",
        "# HELP build_engine_workers_total Build engine configured worker count.",
        "# TYPE build_engine_workers_total gauge",
        f"build_engine_workers_total {snapshot.workers_total}",
        "# HELP build_engine_queue_depth Local queued build attempts.",
        "# TYPE build_engine_queue_depth gauge",
        f"build_engine_queue_depth {snapshot.queue_depth}",
        "# HELP build_engine_cache_size_bytes Build cache size in bytes.",
        "# TYPE build_engine_cache_size_bytes gauge",
        f"build_engine_cache_size_bytes {snapshot.cache_size_bytes}",
        "# HELP build_engine_cache_hit_ratio Build cache hit ratio since process start.",
        "# TYPE build_engine_cache_hit_ratio gauge",
        f"build_engine_cache_hit_ratio {snapshot.cache_hit_ratio}",
        "# HELP build_engine_jobs_running Local build attempts currently running.",
        "# TYPE build_engine_jobs_running gauge",
        f"build_engine_jobs_running {snapshot.jobs_running}",
        "# HELP build_engine_jobs_completed_total Completed local build attempts.",
        "# TYPE build_engine_jobs_completed_total counter",
        f"build_engine_jobs_completed_total {snapshot.jobs_completed_total}",
        "# HELP build_engine_docker_errors_total Docker infrastructure errors.",
        "# TYPE build_engine_docker_errors_total counter",
        f"build_engine_docker_errors_total {snapshot.docker_errors_total}",
        "# HELP build_engine_uplink_reconnects_total WSS reconnect attempts.",
        "# TYPE build_engine_uplink_reconnects_total counter",
        f"build_engine_uplink_reconnects_total {snapshot.uplink_reconnects_total}",
    ]
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(path)

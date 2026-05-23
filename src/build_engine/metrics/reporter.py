"""Metrics rollup reporting to coreapp."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
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
        try:
            await asyncio.to_thread(reporter.report, snapshot)
        except MetricsReportError as exc:
            now = time.monotonic()
            if now - last_warning_at >= METRICS_WARNING_INTERVAL_SECONDS:
                LOG.warning("metrics report failed; will retry", exc_info=exc)
                last_warning_at = now
        await sleep(config.heartbeat_interval_seconds)

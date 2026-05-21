"""In-process metrics collection for the build engine."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from threading import Lock

from build_engine.agent.heartbeat import HeartbeatSnapshot


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    """One rollup payload sent to coreapp."""

    recorded_at: str
    workers_busy: int
    workers_total: int
    queue_depth: int
    cache_size_bytes: int
    cache_hit_ratio: float
    jobs_running: int
    jobs_completed_total: int
    docker_errors_total: int
    uplink_reconnects_total: int

    def to_payload(self) -> dict[str, object]:
        """Return the OpenAPI metrics rollup payload."""

        return asdict(self)

    def to_heartbeat(self, *, disk_free_bytes: int) -> HeartbeatSnapshot:
        """Return the subset used by WSS heartbeats."""

        return HeartbeatSnapshot(
            workers_busy=self.workers_busy,
            workers_total=self.workers_total,
            queue_depth=self.queue_depth,
            cache_size_bytes=self.cache_size_bytes,
            disk_free_bytes=disk_free_bytes,
        )


class MetricsCollector:
    """Small thread-safe collector for gauges and counters."""

    def __init__(self, *, workers_total: int) -> None:
        self.workers_total = workers_total
        self._lock = Lock()
        self._jobs_running = 0
        self._jobs_completed_total = 0
        self._docker_errors_total = 0
        self._uplink_reconnects_total = 0
        self._cache_hits = 0
        self._cache_misses = 0

    def job_started(self) -> None:
        """Record that one worker started a job."""

        with self._lock:
            self._jobs_running += 1

    def job_finished(self, *, completed: bool = True) -> None:
        """Record that one worker finished a job attempt."""

        with self._lock:
            self._jobs_running = max(0, self._jobs_running - 1)
            if completed:
                self._jobs_completed_total += 1

    def docker_error(self) -> None:
        """Increment Docker-related infrastructure errors."""

        with self._lock:
            self._docker_errors_total += 1

    def uplink_reconnect(self) -> None:
        """Increment WSS reconnect attempts after connection failure."""

        with self._lock:
            self._uplink_reconnects_total += 1

    def cache_event(self, event: str | None) -> None:
        """Record cache HIT/MISS/WIPED events for hit-ratio rollups."""

        if event is None:
            return
        with self._lock:
            if event == "HIT":
                self._cache_hits += 1
            elif event in {"MISS", "WIPED"}:
                self._cache_misses += 1

    def snapshot(self, *, queue_depth: int, cache_size_bytes: int) -> MetricsSnapshot:
        """Return a consistent metrics rollup."""

        with self._lock:
            cache_total = self._cache_hits + self._cache_misses
            hit_ratio = self._cache_hits / cache_total if cache_total else 0.0
            jobs_running = self._jobs_running
            jobs_completed_total = self._jobs_completed_total
            docker_errors_total = self._docker_errors_total
            uplink_reconnects_total = self._uplink_reconnects_total
        return MetricsSnapshot(
            recorded_at=_utcnow(),
            workers_busy=jobs_running,
            workers_total=self.workers_total,
            queue_depth=queue_depth,
            cache_size_bytes=cache_size_bytes,
            cache_hit_ratio=hit_ratio,
            jobs_running=jobs_running,
            jobs_completed_total=jobs_completed_total,
            docker_errors_total=docker_errors_total,
            uplink_reconnects_total=uplink_reconnects_total,
        )


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

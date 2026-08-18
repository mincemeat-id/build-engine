"""Local Prometheus textfile metrics for the build engine."""

from __future__ import annotations

from pathlib import Path

from build_engine.metrics.collector import MetricsSnapshot


def write_textfile_metrics(path: Path, snapshot: MetricsSnapshot) -> None:
    """Write a Prometheus textfile collector snapshot atomically.

    Runtime metrics travel in the authenticated WSS heartbeat. The textfile is
    the only local reporting surface; keeping HTTP rollup code out of the
    agent avoids a second liveness and retry path.
    """

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

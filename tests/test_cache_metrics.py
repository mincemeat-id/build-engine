"""Cache lifecycle and metrics tests."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from build_engine.executor.cache import (
    cache_size_bytes,
    prepare_site_cache,
    prune_cache,
)
from build_engine.metrics.collector import MetricsCollector
from build_engine.metrics.reporter import write_textfile_metrics


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
    assert not (tmp_path / "cache" / "site-a" / "entry").exists()


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

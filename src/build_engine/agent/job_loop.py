"""Build job scheduling loop and executor orchestration."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from build_engine.config import EngineConfig, EngineCredentials
from build_engine.detect.framework import DetectionError, plan_build
from build_engine.executor.artifact import ArtifactError, ArtifactUploadClient, package_output
from build_engine.executor.cache import CacheError, prepare_site_cache, prune_cache
from build_engine.executor.docker_runner import (
    DockerError,
    DockerRunSpec,
    load_image_manifest,
    pull_image,
    resolve_image_reference,
    run_container,
)
from build_engine.executor.network import NetworkGuardError, ensure_network_guard
from build_engine.executor.workspace import (
    WorkspaceError,
    cleanup_workspace,
    create_workspace,
    download_source,
    extract_source,
    resolve_project_root,
)
from build_engine.metrics.collector import MetricsCollector
from build_engine.queue.dlq import record_executor_crash
from build_engine.queue.leases import acquire_queue_lease
from build_engine.queue.store import JobRecord, SQLiteQueueStore


class EventPublisher(Protocol):
    """Attempt event publisher shared with the WSS uplink."""

    async def publish_attempt_event(
        self,
        message_type: str,
        payload: dict[str, Any],
        *,
        build_job_id: str,
        attempt_id: str,
    ) -> object:
        """Publish one attempt-scoped event."""


type Sleep = Callable[[float], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class BuildExecutionError(Exception):
    """Structured build failure that should be reported to coreapp."""

    error_class: str
    error_code: str
    message: str


@dataclass(frozen=True, slots=True)
class JobLoopOptions:
    """Tunable queue worker options."""

    lease_seconds: int = 900
    idle_sleep_seconds: float = 1.0
    retain_failed_workspaces: bool = False
    failed_workspace_keep: int = 5


async def run_worker_pool(
    *,
    store: SQLiteQueueStore,
    publisher: EventPublisher,
    config: EngineConfig,
    credentials: EngineCredentials,
    options: JobLoopOptions | None = None,
    metrics: MetricsCollector | None = None,
) -> None:
    """Run queue workers until cancelled."""

    selected_options = options or JobLoopOptions()
    workers = [
        asyncio.create_task(
            _worker(
                store=store,
                publisher=publisher,
                config=config,
                credentials=credentials,
                owner=f"worker-{index}",
                options=selected_options,
                metrics=metrics,
            )
        )
        for index in range(config.max_concurrency)
    ]
    await asyncio.gather(*workers)


async def _worker(
    *,
    store: SQLiteQueueStore,
    publisher: EventPublisher,
    config: EngineConfig,
    credentials: EngineCredentials,
    owner: str,
    options: JobLoopOptions,
    metrics: MetricsCollector | None = None,
    sleep: Sleep = asyncio.sleep,
) -> None:
    while True:
        lease = acquire_queue_lease(
            store,
            owner=owner,
            visibility_timeout_seconds=options.lease_seconds,
        )
        if lease is None:
            await sleep(options.idle_sleep_seconds)
            continue
        store.transition(attempt_id=lease.job.attempt_id, state="RUNNING")
        if metrics is not None:
            metrics.job_started()
        completed = False
        try:
            await execute_job(
                lease.job,
                store=store,
                publisher=publisher,
                config=config,
                credentials=credentials,
                options=options,
                metrics=metrics,
            )
            completed = True
        except BuildExecutionError as exc:
            completed = True
            store.transition(attempt_id=lease.job.attempt_id, state="FAILED", error=exc.message)
            await _publish_error(publisher, lease.job, exc)
            await _ack(
                publisher,
                lease.job,
                state="FAILED",
                error_class=exc.error_class,
                error_code=exc.error_code,
                error_message=exc.message,
            )
        except Exception as exc:
            dead_lettered = record_executor_crash(
                store,
                attempt_id=lease.job.attempt_id,
                error=str(exc),
            )
            completed = dead_lettered
            if dead_lettered:
                await _ack(
                    publisher,
                    lease.job,
                    state="FAILED",
                    error_class="EXEC_INFRA",
                    error_code="EXECUTOR_CRASH",
                    error_message=str(exc),
                )
        finally:
            if metrics is not None:
                metrics.job_finished(completed=completed)


async def execute_job(
    job: JobRecord,
    *,
    store: SQLiteQueueStore,
    publisher: EventPublisher,
    config: EngineConfig,
    credentials: EngineCredentials,
    options: JobLoopOptions | None = None,
    metrics: MetricsCollector | None = None,
) -> None:
    """Execute one leased build attempt end to end."""

    selected_options = options or JobLoopOptions()
    workspace = create_workspace(config.state_dir, job.attempt_id)
    retain_failed = False
    cancel_monitor: asyncio.Task[None] | None = None
    job_started_at = time.perf_counter()
    try:
        await _status(publisher, job, "PREPARING")
        payload = job.payload
        source_url = _required_str(payload, "source_download_url")
        source_sha256 = _required_str(payload, "source_sha256")
        download_source(
            source_url,
            workspace.source_archive,
            expected_sha256=source_sha256,
            max_bytes=config.artifact_max_bytes,
        )
        extract_source(workspace.source_archive, workspace.source_root)
        project_root = resolve_project_root(
            workspace.source_root,
            _optional_str(payload, "root_directory"),
        )
        plan = plan_build(
            project_root,
            framework_override=_optional_str(payload, "framework_id"),
            build_command=_optional_str(payload, "build_command"),
            output_dir=_optional_str(payload, "output_dir"),
            node_version=payload.get("node_version"),
        )

        image = _resolve_builder_image(plan.image, payload)
        await _status(publisher, job, "PULLING_IMAGE", {"image": image})
        image_pull_started_at = time.perf_counter()
        await asyncio.to_thread(pull_image, image)
        await _metric(
            publisher,
            job,
            "docker.image_pull_seconds",
            time.perf_counter() - image_pull_started_at,
        )
        network_guard = await asyncio.to_thread(ensure_network_guard)
        cancel_event = asyncio.Event()
        cancel_monitor = asyncio.create_task(_monitor_cancel(store, job, cancel_event))
        command = _combined_command(plan.install_command, plan.build_command)
        await asyncio.to_thread(
            prune_cache,
            config.state_dir,
            site_max_bytes=config.cache_site_max_bytes,
            ttl_days=config.cache_ttl_days,
        )
        cache = prepare_site_cache(
            state_dir=config.state_dir,
            site_id=_required_str(payload, "site_id"),
            package_manager=plan.package_manager,
            project_root=project_root,
            enabled=_cache_enabled(payload),
        )
        if metrics is not None:
            metrics.cache_event(cache.event)
        if cache.event is not None:
            await publisher.publish_attempt_event(
                "cache.event",
                {"build_job_id": job.build_job_id, "event": cache.event},
                build_job_id=job.build_job_id,
                attempt_id=job.attempt_id,
            )
        await _status(publisher, job, "BUILDING", plan.to_job_payload_fields())
        build_started_at = time.perf_counter()
        result = await run_container(
            DockerRunSpec(
                image=image,
                project_root=project_root,
                command=command,
                config=config,
                network_guard=network_guard,
                cache_mounts=cache.mounts,
                secrets=_secret_values(payload),
            ),
            publish_log=lambda stream, data: _log(publisher, job, stream, data),
            cancel_event=cancel_event,
        )
        await _metric(publisher, job, "build.seconds", time.perf_counter() - build_started_at)
        if result.cancelled:
            raise BuildExecutionError("CANCELLED", "CANCELLED", "Build was cancelled")
        if result.timed_out:
            raise BuildExecutionError("EXEC_TIMEOUT", "TIMEOUT", "Build exceeded timeout")
        if result.exit_code != 0:
            raise BuildExecutionError(
                "USER_BUILD_FAILED",
                "CONTAINER_EXIT",
                f"Build container exited with code {result.exit_code}",
            )

        await _status(publisher, job, "PACKAGING")
        package_started_at = time.perf_counter()
        artifact = await asyncio.to_thread(
            package_output,
            project_root=project_root,
            output_dir=plan.output_dir,
            destination=workspace.artifact_dir / "artifact.tar.gz",
            max_bytes=config.artifact_max_bytes,
        )
        await _metric(publisher, job, "package.seconds", time.perf_counter() - package_started_at)
        await _metric(publisher, job, "artifact.bytes", artifact.size_bytes)
        await publisher.publish_attempt_event(
            "artifact.ready",
            {"sha256": artifact.sha256, "size_bytes": artifact.size_bytes},
            build_job_id=job.build_job_id,
            attempt_id=job.attempt_id,
        )
        await _status(publisher, job, "UPLOADING")
        backend_url = credentials.backend_url or config.backend_url
        if backend_url is None:
            raise BuildExecutionError(
                "PLATFORM_ERROR",
                "BACKEND_URL_MISSING",
                "backend_url is missing",
            )
        upload_client = ArtifactUploadClient(
            backend_url=backend_url,
            session_jwt=credentials.session_jwt,
        )
        ticket = await asyncio.to_thread(
            upload_client.request_upload_url,
            build_job_id=job.build_job_id,
            attempt_id=job.attempt_id,
            artifact=artifact,
        )
        upload_started_at = time.perf_counter()
        await asyncio.to_thread(upload_client.upload, ticket, artifact)
        await _metric(publisher, job, "upload.seconds", time.perf_counter() - upload_started_at)
        store.transition(attempt_id=job.attempt_id, state="SUCCEEDED")
        await _metric(publisher, job, "jobs.duration_seconds", time.perf_counter() - job_started_at)
        await _ack(publisher, job, state="SUCCEEDED")
    except (ArtifactError, DetectionError, WorkspaceError) as exc:
        retain_failed = True
        raise BuildExecutionError("USER_CONFIG_INVALID", type(exc).__name__, str(exc)) from exc
    except CacheError as exc:
        retain_failed = True
        raise BuildExecutionError("EXEC_INFRA", type(exc).__name__, str(exc)) from exc
    except DockerError as exc:
        if metrics is not None:
            metrics.docker_error()
        retain_failed = True
        raise BuildExecutionError("EXEC_INFRA", type(exc).__name__, str(exc)) from exc
    except NetworkGuardError as exc:
        retain_failed = True
        raise BuildExecutionError("EXEC_INFRA", type(exc).__name__, str(exc)) from exc
    finally:
        if cancel_monitor is not None:
            cancel_monitor.cancel()
        cleanup_workspace(
            workspace,
            retain_failed=retain_failed and selected_options.retain_failed_workspaces,
            failed_keep=selected_options.failed_workspace_keep,
        )


async def _monitor_cancel(
    store: SQLiteQueueStore,
    job: JobRecord,
    event: asyncio.Event,
) -> None:
    while not event.is_set():
        current = store.get_job(job.build_job_id, job.attempt_id)
        if current is not None and current.state == "CANCELLED":
            event.set()
            return
        await asyncio.sleep(0.5)


async def _status(
    publisher: EventPublisher,
    job: JobRecord,
    phase: str,
    extra: dict[str, object] | None = None,
) -> None:
    payload: dict[str, object] = {"phase": phase}
    if extra is not None:
        payload.update(extra)
    await publisher.publish_attempt_event(
        "status",
        payload,
        build_job_id=job.build_job_id,
        attempt_id=job.attempt_id,
    )


async def _log(publisher: EventPublisher, job: JobRecord, stream: str, data: str) -> None:
    await publisher.publish_attempt_event(
        "log",
        {"stream": stream, "data": data},
        build_job_id=job.build_job_id,
        attempt_id=job.attempt_id,
    )


async def _metric(
    publisher: EventPublisher,
    job: JobRecord,
    name: str,
    value: float | int,
) -> None:
    await publisher.publish_attempt_event(
        "metric",
        {"name": name, "value": value},
        build_job_id=job.build_job_id,
        attempt_id=job.attempt_id,
    )


async def _ack(
    publisher: EventPublisher,
    job: JobRecord,
    *,
    state: str,
    error_class: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    payload: dict[str, object] = {
        "build_job_id": job.build_job_id,
        "attempt_id": job.attempt_id,
        "state": state,
    }
    if error_class is not None:
        payload["error_class"] = error_class
    if error_code is not None:
        payload["error_code"] = error_code
    if error_message is not None:
        payload["error_message"] = error_message
    await publisher.publish_attempt_event(
        "job.ack",
        payload,
        build_job_id=job.build_job_id,
        attempt_id=job.attempt_id,
    )


async def _publish_error(
    publisher: EventPublisher,
    job: JobRecord,
    error: BuildExecutionError,
) -> None:
    await publisher.publish_attempt_event(
        "error",
        {
            "error_class": error.error_class,
            "error_code": error.error_code,
            "message": error.message,
        },
        build_job_id=job.build_job_id,
        attempt_id=job.attempt_id,
    )


def _combined_command(install_command: str, build_command: str) -> str:
    if install_command:
        return f"{install_command} && {build_command}"
    return build_command


def _resolve_builder_image(image: str, payload: dict[str, Any]) -> str:
    manifest_path = _optional_str(payload, "image_manifest_path")
    if manifest_path is None:
        return image
    return resolve_image_reference(image, manifest=load_image_manifest(manifest_path))


def _secret_values(payload: dict[str, Any]) -> tuple[str, ...]:
    raw = payload.get("secrets", {})
    if not isinstance(raw, dict):
        return ()
    return tuple(str(value) for value in raw.values() if isinstance(value, str) and value)


def _cache_enabled(payload: dict[str, Any]) -> bool:
    for key in ("cache_enabled", "build_cache_enabled"):
        value = payload.get(key)
        if isinstance(value, bool):
            return value
        if value is not None:
            raise WorkspaceError(f"payload {key} must be a boolean")
    return True


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = _optional_str(payload, key)
    if value is None:
        raise WorkspaceError(f"payload {key} is required")
    return value


def _optional_str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise WorkspaceError(f"payload {key} must be a non-empty string")
    return value

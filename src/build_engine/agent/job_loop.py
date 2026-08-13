"""Build job scheduling loop and executor orchestration."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from build_engine.agent.auth import BuildEngineAuthClient
from build_engine.config import EngineConfig, EngineCredentials
from build_engine.detect.framework import (
    BuildPlan,
    DetectionError,
    plan_build,
)
from build_engine.executor.artifact import (
    ArtifactError,
    ArtifactUploadClient,
    ArtifactUploadError,
    package_output,
)
from build_engine.executor.cache import CacheError, prepare_site_cache, prune_cache
from build_engine.executor.docker_runner import (
    DockerError,
    DockerRunSpec,
    load_bundled_image_manifest,
    pull_image,
    resolve_image_reference,
    run_container,
)
from build_engine.executor.workspace import (
    WorkspaceError,
    cleanup_workspace,
    create_workspace,
    download_source,
    extract_source,
    resolve_project_root,
)
from build_engine.metrics.collector import MetricsCollector
from build_engine.queue.leases import acquire_queue_lease
from build_engine.queue.store import JobRecord, QueueError, SQLiteQueueStore


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

    lease_seconds: int = 120
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
    stop_event: asyncio.Event | None = None,
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
                stop_event=stop_event,
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
    stop_event: asyncio.Event | None = None,
    sleep: Sleep = asyncio.sleep,
) -> None:
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        lease = acquire_queue_lease(
            store,
            owner=owner,
            visibility_timeout_seconds=options.lease_seconds,
        )
        if lease is None:
            await sleep(options.idle_sleep_seconds)
            continue
        try:
            store.transition(
                attempt_id=lease.job.attempt_id,
                state="RUNNING",
                expected_state="LEASED",
                lease_owner=lease.owner,
                lease_token=lease.token,
            )
        except QueueError:
            continue
        lease_refresh = asyncio.create_task(_refresh_lease(store, lease))
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
                lease_owner=lease.owner,
                lease_token=lease.token,
            )
            completed = True
        except BuildExecutionError as exc:
            current = store.get_job(lease.job.build_job_id, lease.job.attempt_id)
            if current is not None and current.state not in _TERMINAL_STATES:
                target = (
                    "TIMED_OUT"
                    if exc.error_class == "EXEC_TIMEOUT"
                    else ("CANCELLED" if exc.error_class == "CANCELLED" else "FAILED")
                )
                with contextlib.suppress(QueueError):
                    store.transition(
                        attempt_id=lease.job.attempt_id,
                        state=target,
                        error=exc.message,
                        expected_state=current.state,
                        lease_owner=lease.owner,
                        lease_token=lease.token,
                    )
            completed = True
            await _publish_safely(publisher, lease.job, exc)
            await _ack_safely(
                publisher,
                lease.job,
                state="TIMED_OUT"
                if exc.error_class == "EXEC_TIMEOUT"
                else ("CANCELLED" if exc.error_class == "CANCELLED" else "FAILED"),
                error_class=exc.error_class,
                error_code=exc.error_code,
                error_message=exc.message,
            )
        except Exception as exc:
            current = store.get_job(lease.job.build_job_id, lease.job.attempt_id)
            if current is not None and current.state in _TERMINAL_STATES:
                # The build already reached a durable terminal state. A
                # transport/logging failure must not run it again.
                completed = True
            else:
                try:
                    recovered = store.record_executor_crash(
                        attempt_id=lease.job.attempt_id,
                        error=str(exc),
                        lease_owner=lease.owner,
                        lease_token=lease.token,
                    )
                except QueueError:
                    recovered = None
                completed = recovered is not None and recovered.state == "FAILED"
                await _publish_safely(
                    publisher,
                    lease.job,
                    BuildExecutionError("EXEC_INFRA", "EXECUTOR_CRASH", str(exc)),
                )
                if completed:
                    await _ack_safely(
                        publisher,
                        lease.job,
                        state="FAILED",
                        error_class="EXEC_INFRA",
                        error_code="EXECUTOR_CRASH",
                        error_message=str(exc),
                    )
        finally:
            lease_refresh.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await lease_refresh
            if metrics is not None:
                metrics.job_finished(completed=completed)


async def _refresh_lease(store: SQLiteQueueStore, lease) -> None:  # noqa: ANN001
    """Refresh a worker lease until the attempt exits or ownership is lost."""

    interval = max(1.0, min(30.0, lease.visibility_timeout_seconds / 3))
    while True:
        await asyncio.sleep(interval)
        try:
            store.refresh_lease(
                attempt_id=lease.job.attempt_id,
                lease_owner=lease.owner,
                lease_token=lease.token,
                visibility_timeout_seconds=lease.visibility_timeout_seconds,
            )
        except QueueError:
            return


_TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"})


async def execute_job(
    job: JobRecord,
    *,
    store: SQLiteQueueStore,
    publisher: EventPublisher,
    config: EngineConfig,
    credentials: EngineCredentials,
    options: JobLoopOptions | None = None,
    metrics: MetricsCollector | None = None,
    lease_owner: str | None = None,
    lease_token: str | None = None,
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
        source_size = _optional_int(payload, "source_size_bytes")
        download_source(
            source_url,
            workspace.source_archive,
            expected_sha256=source_sha256,
            expected_size=source_size,
            max_bytes=config.artifact_max_bytes,
            allowed_origins=config.storage_origins,
            allow_file=config.allow_local_source_urls,
        )
        extract_source(workspace.source_archive, workspace.source_root)
        project_root = resolve_project_root(
            workspace.source_root,
            _optional_str(payload, "root_directory"),
        )
        plan = plan_build(
            project_root,
            framework_override=_optional_str(payload, "framework_id"),
        )
        if not plan.compatibility.compatible:
            guidance = [item.to_dict() for item in plan.compatibility.guidance]
            raise BuildExecutionError(
                "USER_CONFIG_INVALID",
                "BUILD_INCOMPATIBLE",
                json.dumps(guidance, sort_keys=True),
            )

        image = _resolve_builder_image(plan.image)
        await _status(publisher, job, "PULLING_IMAGE", {"image": image})
        image_pull_started_at = time.perf_counter()
        await asyncio.to_thread(pull_image, image)
        await _metric(
            publisher,
            job,
            "docker.image_pull_seconds",
            time.perf_counter() - image_pull_started_at,
        )
        timeout_seconds, memory_limit, cpu_limit, pids_limit = _resource_limits(payload, config)
        cancel_event = asyncio.Event()
        cancel_monitor = asyncio.create_task(_monitor_cancel(store, job, cancel_event))
        command = _combined_command(plan.install_command, plan.build_command)
        site_id = _required_str(payload, "site_id")
        cache = prepare_site_cache(
            state_dir=config.state_dir,
            site_id=site_id,
            package_manager=plan.package_manager,
            project_root=project_root,
            enabled=_cache_enabled(payload) and plan.package_manager != "none",
        )
        await asyncio.to_thread(
            prune_cache,
            config.state_dir,
            site_max_bytes=config.cache_site_max_bytes,
            ttl_days=config.cache_ttl_days,
            exclude_site_ids=(site_id,),
        )
        if metrics is not None:
            metrics.cache_event(cache.event)
        if cache.event is not None:
            await publisher.publish_attempt_event(
                "attempt.status",
                {"phase": "CACHE", "event": cache.event},
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
                source_root=workspace.source_root,
                output_root=workspace.output_root,
                build_manifest=_build_manifest(
                    plan=plan,
                    source_root=workspace.source_root,
                    project_root=project_root,
                ),
                cache_mounts=cache.mounts,
                timeout_seconds=timeout_seconds,
                memory_limit=memory_limit,
                cpu_limit=cpu_limit,
                pids_limit=pids_limit,
                secret_env=await _fetch_secret_env(
                    credentials,
                    backend_url=credentials.backend_url or config.backend_url,
                    build_job_id=job.build_job_id,
                    attempt_id=job.attempt_id,
                ),
            ),
            publish_log=lambda stream, data: _log(publisher, job, stream, data),
            cancel_event=cancel_event,
        )
        await _metric(publisher, job, "build.seconds", time.perf_counter() - build_started_at)
        if result.cancelled:
            raise BuildExecutionError("CANCELLED", "CANCELLED", "Build was cancelled")
        if result.timed_out:
            raise BuildExecutionError("EXEC_TIMEOUT", "TIMEOUT", "Build exceeded timeout")
        if result.exit_code == 137:
            raise BuildExecutionError(
                "EXEC_OOM",
                "EXEC_OOM",
                "Build container was killed after exceeding its memory limit",
            )
        if result.exit_code != 0:
            raise BuildExecutionError(
                "USER_BUILD_FAILED",
                "CONTAINER_EXIT",
                f"Build container exited with code {result.exit_code}",
            )
        _ensure_current_attempt(store, job, lease_owner=lease_owner, lease_token=lease_token)
        await _status(publisher, job, "PACKAGING")
        package_started_at = time.perf_counter()
        artifact = await asyncio.to_thread(
            package_output,
            project_root=workspace.attempt_dir,
            output_dir="out",
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
        store.transition(
            attempt_id=job.attempt_id,
            state="SUCCEEDED",
            expected_state="RUNNING",
            lease_owner=lease_owner,
            lease_token=lease_token,
        )
        await _metric(publisher, job, "jobs.duration_seconds", time.perf_counter() - job_started_at)
        await _ack(publisher, job, state="SUCCEEDED")
    except ArtifactUploadError as exc:
        retain_failed = True
        raise BuildExecutionError("EXEC_INFRA", "STORAGE_FAILURE", str(exc)) from exc
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
        if current is not None and (current.cancel_requested or current.state == "CANCELLED"):
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
        "attempt.status",
        payload,
        build_job_id=job.build_job_id,
        attempt_id=job.attempt_id,
    )


async def _log(publisher: EventPublisher, job: JobRecord, stream: str, data: str) -> None:
    with contextlib.suppress(Exception):
        await publisher.publish_attempt_event(
            "attempt.log",
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
        "attempt.status",
        {"phase": "METRIC", "name": name, "value": value},
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
        "attempt.status",
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


async def _publish_safely(
    publisher: EventPublisher,
    job: JobRecord,
    error: BuildExecutionError,
) -> None:
    """Best-effort error reporting that cannot kill the worker loop."""

    with contextlib.suppress(Exception):
        await _publish_error(publisher, job, error)


async def _ack_safely(
    publisher: EventPublisher,
    job: JobRecord,
    **kwargs: str,
) -> None:
    """Best-effort terminal acknowledgement after durable local state wins."""

    with contextlib.suppress(Exception):
        await _ack(publisher, job, **kwargs)


def _combined_command(install_command: str, build_command: str) -> str:
    if install_command:
        return f"{install_command} && {build_command}"
    return build_command


def _ensure_current_attempt(
    store: SQLiteQueueStore,
    job: JobRecord,
    *,
    lease_owner: str | None,
    lease_token: str | None,
) -> None:
    current = store.get_job(job.build_job_id, job.attempt_id)
    if not store.is_current_attempt(build_job_id=job.build_job_id, attempt_id=job.attempt_id):
        raise BuildExecutionError(
            "STALE_ATTEMPT",
            "STALE_ATTEMPT",
            "Build attempt was superseded by a newer assignment",
        )
    if current is None or current.state != "RUNNING":
        raise BuildExecutionError(
            "STALE_ATTEMPT",
            "STALE_ATTEMPT",
            "Build attempt is no longer running",
        )
    if lease_owner is not None and current.lease_owner != lease_owner:
        raise BuildExecutionError(
            "STALE_ATTEMPT",
            "STALE_ATTEMPT",
            "Build lease ownership was lost",
        )
    if lease_token is not None and current.lease_token != lease_token:
        raise BuildExecutionError(
            "STALE_ATTEMPT",
            "STALE_ATTEMPT",
            "Build lease token was replaced",
        )
    if current.cancel_requested:
        raise BuildExecutionError("CANCELLED", "CANCELLED", "Build cancellation was requested")


def _resolve_builder_image(image: str) -> str:
    return resolve_image_reference(image, manifest=load_bundled_image_manifest())


def _build_manifest(
    *,
    plan: BuildPlan,
    source_root: Path,
    project_root: Path,
) -> dict[str, object]:
    try:
        root = project_root.relative_to(source_root).as_posix()
    except ValueError:
        root = "."
    return {
        "framework": plan.framework_id,
        "package_manager": plan.package_manager,
        "root": root or ".",
        "install_command": plan.install_command,
        "build_command": plan.build_command,
        "output_dir": plan.output_dir or "",
        "env": {},
    }


async def _fetch_secret_env(
    credentials: EngineCredentials,
    *,
    backend_url: str | None,
    build_job_id: str,
    attempt_id: str,
) -> dict[str, str]:
    """Fetch transient secrets without putting them in the queue payload."""

    if backend_url is None:
        raise BuildExecutionError("PLATFORM_ERROR", "BACKEND_URL_MISSING", "backend_url is missing")
    client = BuildEngineAuthClient(backend_url)
    try:
        return await asyncio.to_thread(
            client.fetch_attempt_secrets,
            build_job_id=build_job_id,
            attempt_id=attempt_id,
            credentials=credentials,
        )
    except Exception as exc:
        raise BuildExecutionError(
            "EXEC_INFRA",
            "SECRET_FETCH_FAILED",
            "Build secret fetch failed",
        ) from exc


def _cache_enabled(payload: dict[str, Any]) -> bool:
    policy = payload.get("cache_policy")
    if policy is not None:
        if not isinstance(policy, dict):
            raise WorkspaceError("payload cache_policy must be an object")
        value = policy.get("enabled", True)
        if not isinstance(value, bool):
            raise WorkspaceError("payload cache_policy.enabled must be a boolean")
        return value
    for key in ("cache_enabled", "build_cache_enabled"):
        value = payload.get(key)
        if isinstance(value, bool):
            return value
        if value is not None:
            raise WorkspaceError(f"payload {key} must be a boolean")
    return True


def _resource_limits(
    payload: dict[str, Any],
    config: EngineConfig,
) -> tuple[int, str | None, float | None, int]:
    timeout = payload.get("timeout_seconds", config.build_timeout_seconds)
    if (
        not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or not 1 <= timeout <= config.build_timeout_seconds
    ):
        raise WorkspaceError("payload timeout_seconds exceeds the local bounded limit")
    raw = payload.get("resource_limits", {})
    if not isinstance(raw, dict):
        raise WorkspaceError("payload resource_limits must be an object")
    memory = raw.get("memory")
    if memory is not None and (
        not isinstance(memory, str)
        or re.fullmatch(r"[1-9][0-9]*(?:[bkmg])", memory.lower()) is None
    ):
        raise WorkspaceError("payload resource_limits.memory is invalid")
    cpus = raw.get("cpus")
    if cpus is not None and (
        not isinstance(cpus, (int, float))
        or isinstance(cpus, bool)
        or not 0 < float(cpus) <= config.container_cpus
    ):
        raise WorkspaceError("payload resource_limits.cpus exceeds the local limit")
    pids = raw.get("pids_limit", 1024)
    if not isinstance(pids, int) or isinstance(pids, bool) or not 1 <= pids <= 4096:
        raise WorkspaceError("payload resource_limits.pids_limit is invalid")
    return timeout, memory, float(cpus) if cpus is not None else None, pids


def _optional_int(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise WorkspaceError(f"payload {key} must be a positive integer")
    return value


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

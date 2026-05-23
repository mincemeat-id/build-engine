"""Bootstrap CLI commands for the build engine."""

import argparse
import asyncio
import contextlib
import logging
import shutil
import signal
import sys
from collections.abc import Callable, Sequence
from typing import cast

from build_engine import __version__
from build_engine.agent.auth import (
    SERVICE_GROUP,
    SERVICE_USER,
    AuthError,
    refresh_session,
    register_engine,
    validate_credentials_file,
)
from build_engine.agent.heartbeat import HeartbeatSnapshot
from build_engine.agent.job_loop import run_worker_pool
from build_engine.agent.uplink import BuildEngineUplink
from build_engine.cli.doctor import render_human, render_json, run_doctor
from build_engine.cli.logging import configure_logging
from build_engine.config import DEFAULTS, EngineConfig, EngineCredentials, load_config
from build_engine.executor.cache import cache_size_bytes, reset_cache
from build_engine.metrics.collector import MetricsCollector
from build_engine.metrics.reporter import run_metrics_reporter, write_textfile_metrics
from build_engine.queue.handlers import SQLiteCommandHandlers
from build_engine.queue.store import SQLiteEventOutbox, SQLiteQueueStore

LOG = logging.getLogger(__name__)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the build-engine command line interface."""

    parser = _build_parser()
    args = parser.parse_args(argv)
    configure_logging(log_format=args.log_format, level=args.log_level)
    handler = cast("Callable[[argparse.Namespace], int]", args.handler)
    return handler(args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="build-engine")
    parser.add_argument("--version", action="version", version=f"build-engine {__version__}")
    parser.add_argument(
        "--log-format",
        choices=("human", "json"),
        default="human",
        help="log format for operational messages",
    )
    parser.add_argument("--log-level", default="INFO", help="logging level")

    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="run the agent service")
    serve.add_argument("--config", default="/etc/mincemeat/build-engine/config.toml")
    serve.add_argument("--credentials", default=None)
    serve.add_argument("--state-dir", default=None, help="override runtime state directory")
    serve.add_argument(
        "--network-blocklist",
        action="append",
        default=None,
        help="comma-separated CIDR/IP deny-list entries to add to the build network guard",
    )
    serve.add_argument(
        "--no-network-guard",
        action="store_true",
        help="run containers with Docker --network none instead of the guarded bridge",
    )
    serve.set_defaults(handler=_serve)

    register = subparsers.add_parser("register", help="register this engine with coreapp")
    register.add_argument("--config", default="/etc/mincemeat/build-engine/config.toml")
    register.add_argument("--credentials", default=None)
    register.add_argument("--backend-url", required=True)
    register.add_argument("--token", required=True)
    register.add_argument("--name", required=True)
    register.add_argument("--max-concurrency", type=int, default=DEFAULTS.max_concurrency)
    register.set_defaults(handler=_register)

    status = subparsers.add_parser("status", help="show local status")
    status.add_argument("--config", default="/etc/mincemeat/build-engine/config.toml")
    status.add_argument("--credentials", default=None)
    status.set_defaults(handler=_status)

    doctor = subparsers.add_parser("doctor", help="run local diagnostics")
    doctor.add_argument("--config", default="/etc/mincemeat/build-engine/config.toml")
    doctor.add_argument("--credentials", default=None)
    doctor.add_argument(
        "--network-blocklist",
        action="append",
        default=None,
        help="comma-separated CIDR/IP deny-list entries to add to the network guard check",
    )
    doctor.add_argument(
        "--skip",
        action="append",
        default=None,
        help="comma-separated doctor check names to skip",
    )
    doctor.add_argument("--json", action="store_true", dest="as_json")
    doctor.set_defaults(handler=_doctor)

    session = subparsers.add_parser("session", help="session operations")
    session_subparsers = session.add_subparsers(dest="session_command")
    session_refresh = session_subparsers.add_parser(
        "refresh",
        help="refresh the backend session JWT",
    )
    session_refresh.add_argument("--config", default="/etc/mincemeat/build-engine/config.toml")
    session_refresh.add_argument("--credentials", default=None)
    session_refresh.add_argument("--backend-url", default=None)
    session_refresh.set_defaults(handler=_session_refresh)

    cache = subparsers.add_parser("cache", help="local cache operations")
    cache_subparsers = cache.add_subparsers(dest="cache_command")
    cache_reset = cache_subparsers.add_parser("reset", help="reset local build cache")
    cache_reset.add_argument("--config", default="/etc/mincemeat/build-engine/config.toml")
    cache_reset.add_argument("--credentials", default=None)
    cache_reset.add_argument("--site-id")
    cache_reset.set_defaults(handler=_cache_reset)

    drain = subparsers.add_parser("drain", help="request local drain mode")
    drain.add_argument("--config", default="/etc/mincemeat/build-engine/config.toml")
    drain.add_argument("--credentials", default=None)
    drain.set_defaults(handler=_drain)

    parser.set_defaults(handler=_help)
    return parser


def _help(args: argparse.Namespace) -> int:
    del args
    _build_parser().print_help()
    return 0


def _serve(args: argparse.Namespace) -> int:
    overrides = _network_blocklist_override(args.network_blocklist)
    if args.state_dir is not None:
        overrides["state_dir"] = args.state_dir
    if args.no_network_guard:
        overrides["network_guard_enabled"] = False
    config = load_config(
        config_path=args.config,
        credentials_path=args.credentials,
        overrides=overrides,
    )
    dev_identity = bool(args.no_network_guard and args.state_dir is not None)
    try:
        credentials = validate_credentials_file(
            config.credentials_path,
            service_user=None if dev_identity else SERVICE_USER,
            service_group=None if dev_identity else SERVICE_GROUP,
        )
    except (AuthError, ValueError, FileNotFoundError) as exc:
        LOG.error("serve failed: not registered (%s)", exc)
        return 1
    startup_report = run_doctor(
        config,
        skip_checks=("image_pull", "wss_handshake"),
        credential_service_user=None if dev_identity else SERVICE_USER,
        credential_service_group=None if dev_identity else SERVICE_GROUP,
    )
    if startup_report.status != "ok":
        LOG.error("serve failed: startup self-test did not pass")
        sys.stderr.write(render_human(startup_report) + "\n")
        return 1
    LOG.info(
        "build-engine serve: starting uplink engine_id=%s max_concurrency=%s",
        credentials.engine_id,
        config.max_concurrency,
    )
    store = SQLiteQueueStore(config.state_dir / "queue.sqlite")
    store.initialize()
    metrics = MetricsCollector(workers_total=config.max_concurrency)

    def heartbeat_snapshot() -> HeartbeatSnapshot:
        disk = shutil.disk_usage(config.state_dir)
        snapshot = metrics.snapshot(
            queue_depth=store.queue_depth(),
            cache_size_bytes=cache_size_bytes(config.state_dir),
        )
        return snapshot.to_heartbeat(disk_free_bytes=disk.free)

    try:
        asyncio.run(_run_service(config, credentials, store, heartbeat_snapshot, metrics))
    except KeyboardInterrupt:
        LOG.info("build-engine serve: stopped")
    return 0


def _register(args: argparse.Namespace) -> int:
    config = load_config(
        config_path=args.config,
        credentials_path=args.credentials,
        overrides={
            "backend_url": args.backend_url,
            "name": args.name,
            "max_concurrency": args.max_concurrency,
        },
    )
    try:
        result = register_engine(config, registration_token=args.token)
    except AuthError as exc:
        LOG.error("registration failed: %s", exc)
        return 1
    LOG.info(
        "registered build engine %s; credentials=%s", result.engine_id, result.credentials_path
    )
    return 0


def _status(args: argparse.Namespace) -> int:
    config = load_config(config_path=args.config, credentials_path=args.credentials)
    try:
        credentials = validate_credentials_file(
            config.credentials_path,
            service_user=SERVICE_USER,
            service_group=SERVICE_GROUP,
        )
    except (AuthError, ValueError, FileNotFoundError) as exc:
        LOG.error("build-engine status: not registered (%s)", exc)
        return 1
    LOG.info(
        "build-engine status: registered engine_id=%s backend=%s",
        credentials.engine_id,
        credentials.backend_url or config.backend_url,
    )
    return 0


def _doctor(args: argparse.Namespace) -> int:
    config = load_config(
        config_path=args.config,
        credentials_path=args.credentials,
        overrides=_network_blocklist_override(args.network_blocklist),
    )
    report = run_doctor(config, skip_checks=_skip_checks(args.skip))
    if args.as_json:
        _write_stdout(render_json(report))
    else:
        _write_stdout(render_human(report))
    return 0 if report.status == "ok" else 1


def _cache_reset(args: argparse.Namespace) -> int:
    config = load_config(config_path=args.config, credentials_path=args.credentials)
    scope = args.site_id or "all-sites"
    reset_cache(config.state_dir, site_id=args.site_id)
    LOG.info("cache reset complete; scope=%s", scope)
    return 0


def _drain(args: argparse.Namespace) -> int:
    config = load_config(config_path=args.config, credentials_path=args.credentials)
    store = SQLiteQueueStore(config.state_dir / "queue.sqlite")
    result = asyncio.run(SQLiteCommandHandlers(store).drain({}))
    LOG.info("drain mode enabled; state=%s", result.state)
    return 0


def _session_refresh(args: argparse.Namespace) -> int:
    config = load_config(config_path=args.config, credentials_path=args.credentials)
    try:
        credentials = refresh_session(
            config.credentials_path,
            backend_url=args.backend_url or config.backend_url,
        )
    except (AuthError, ValueError, FileNotFoundError) as exc:
        LOG.error("session refresh failed: %s", exc)
        return 1
    LOG.info(
        "session refreshed engine_id=%s expires_at=%s",
        credentials.engine_id,
        credentials.session_jwt_expires_at,
    )
    return 0


def _network_blocklist_override(values: list[str] | None) -> dict[str, object | None]:
    if values is None:
        return {}
    entries = tuple(part.strip() for value in values for part in value.split(",") if part.strip())
    return {"network_blocklist": entries}


def _skip_checks(values: list[str] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    return tuple(part.strip() for value in values for part in value.split(",") if part.strip())


def _write_stdout(value: str) -> None:
    sys.stdout.write(value)
    sys.stdout.write("\n")


async def _run_service(
    config: EngineConfig,
    credentials: EngineCredentials,
    store: SQLiteQueueStore,
    heartbeat_snapshot: Callable[[], HeartbeatSnapshot],
    metrics: MetricsCollector,
) -> None:
    command_handlers = SQLiteCommandHandlers(store)
    uplink = BuildEngineUplink(
        config,
        credentials,
        event_spool=SQLiteEventOutbox(store),
        command_handlers=command_handlers,
        heartbeat_provider=heartbeat_snapshot,
        metrics=metrics,
    )
    stop_workers = asyncio.Event()
    worker_task = asyncio.create_task(
        run_worker_pool(
            store=store,
            publisher=uplink,
            config=config,
            credentials=credentials,
            metrics=metrics,
            stop_event=stop_workers,
        )
    )
    metrics_task = asyncio.create_task(
        run_metrics_reporter(
            config=config,
            credentials=credentials,
            store=store,
            collector=metrics,
        )
    )
    loop = asyncio.get_running_loop()
    drain_task: asyncio.Task[None] | None = None

    async def request_drain() -> None:
        nonlocal drain_task
        if stop_workers.is_set():
            return
        LOG.info("build-engine serve: entering drain mode")
        await command_handlers.drain({})
        stop_workers.set()
        await uplink.stop(code=1001, reason="engine_drain")

    def schedule_drain() -> None:
        nonlocal drain_task
        if drain_task is None or drain_task.done():
            drain_task = asyncio.create_task(request_drain())

    installed_signals: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, schedule_drain)
            installed_signals.append(sig)

    try:
        await uplink.run_forever()
    finally:
        for sig in installed_signals:
            loop.remove_signal_handler(sig)
        if drain_task is not None:
            await asyncio.gather(drain_task, return_exceptions=True)
        stop_workers.set()
        metrics_task.cancel()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(worker_task, timeout=config.sigterm_grace_seconds)
        if not worker_task.done():
            worker_task.cancel()
        await asyncio.gather(worker_task, metrics_task, return_exceptions=True)
        write_textfile_metrics(
            config.state_dir / "metrics.prom",
            metrics.snapshot(
                queue_depth=store.queue_depth(),
                cache_size_bytes=cache_size_bytes(config.state_dir),
            ),
        )

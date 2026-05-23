"""Outbound websocket uplink client for the build-engine agent."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse, urlunparse

import websockets

from build_engine import __version__
from build_engine.agent.auth import client_headers_for_credentials
from build_engine.agent.heartbeat import HeartbeatSnapshot, idle_heartbeat
from build_engine.agent.protocol import (
    INBOUND_MESSAGE_TYPES,
    PROTOCOL_VERSION,
    Envelope,
    ProtocolError,
    attempt_fields,
    decode_frame,
    last_sequences,
    new_envelope,
)
from build_engine.config import EngineConfig, EngineCredentials, config_capabilities
from build_engine.metrics.collector import MetricsCollector

AGENT_WS_PATH = "/api/v1/build-engines/agent/ws"


class WebSocketLike(Protocol):
    """Small subset shared by real and test websocket connections."""

    async def send(self, message: str) -> None:
        """Send one text frame."""

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        """Yield inbound frames."""

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """Close the connection."""


type Connector = Callable[
    [str, Mapping[str, str]],
    Awaitable[WebSocketLike],
]
type HeartbeatProvider = Callable[[], HeartbeatSnapshot]


class EventSpoolLike(Protocol):
    """Outbound event persistence used by reconnect replay."""

    async def append(self, envelope: Envelope) -> None:
        """Persist one outbound attempt event."""

    async def replay_after(self, cursors: Mapping[str, int]) -> list[Envelope]:
        """Return events newer than backend cursors."""

    async def next_seq(self, attempt_id: str) -> int:
        """Return the next sequence number for an attempt."""


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """Exponential reconnect delays for the persistent uplink."""

    initial_seconds: float = 1.0
    multiplier: float = 2.0
    max_seconds: float = 30.0

    def delay(self, failures: int) -> float:
        """Return the sleep interval for a one-based failure count."""

        if failures <= 1:
            return self.initial_seconds
        return min(self.initial_seconds * self.multiplier ** (failures - 1), self.max_seconds)


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Result returned by a command handler after local state is updated."""

    accepted: bool = True
    state: str | None = None
    detail: str | None = None
    affected_attempt_ids: tuple[str, ...] = ()


class CommandHandlers(Protocol):
    """Async hooks for backend commands received over the uplink."""

    async def assign(self, payload: dict[str, Any]) -> CommandResult:
        """Accept or deduplicate a build assignment."""

    async def cancel(self, payload: dict[str, Any]) -> CommandResult:
        """Cancel a local build attempt when present."""

    async def drain(self, payload: dict[str, Any]) -> CommandResult:
        """Enter local drain mode."""

    async def cache_reset(self, payload: dict[str, Any]) -> CommandResult:
        """Reset local build cache scope."""


class BuildEngineUplink:
    """Persistent WSS client that negotiates, heartbeats, handles commands, and replays."""

    def __init__(
        self,
        config: EngineConfig,
        credentials: EngineCredentials,
        *,
        event_spool: EventSpoolLike | None = None,
        command_handlers: CommandHandlers | None = None,
        heartbeat_provider: HeartbeatProvider | None = None,
        metrics: MetricsCollector | None = None,
        connector: Connector | None = None,
        backoff: BackoffPolicy | None = None,
    ) -> None:
        self.config = config
        self.credentials = credentials
        self.event_spool = event_spool or _default_event_spool(config)
        self.command_handlers = command_handlers or _default_command_handlers(config)
        self.metrics = metrics
        self.heartbeat_provider = heartbeat_provider or (
            lambda: idle_heartbeat(workers_total=config.max_concurrency)
        )
        self.connector = connector or _websockets_connector
        self.backoff = backoff or BackoffPolicy()
        self._ws: WebSocketLike | None = None
        self._stop = asyncio.Event()
        self._heartbeat_interval_seconds = config.heartbeat_interval_seconds
        self._publish_lock = asyncio.Lock()

    async def run_forever(self) -> None:
        """Keep the backend WSS uplink connected until :meth:`stop` is called."""

        failures = 0
        while not self._stop.is_set():
            try:
                await self.connect_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                if self.metrics is not None:
                    self.metrics.uplink_reconnect()
                await self._sleep_backoff(failures)
            else:
                failures = 0

    async def stop(self) -> None:
        """Request shutdown and close the current websocket if connected."""

        self._stop.set()
        if self._ws is not None:
            await self._ws.close(code=1000, reason="engine shutdown")

    async def connect_once(self) -> None:
        """Open one websocket connection and run it until closed."""

        if not self.credentials.backend_url and not self.config.backend_url:
            raise ProtocolError("backend_url is required for uplink")
        backend_url = self.credentials.backend_url or self.config.backend_url
        assert backend_url is not None
        url = websocket_url(backend_url)
        headers = uplink_headers(self.config, self.credentials)
        websocket = await self.connector(url, headers)
        self._ws = websocket
        heartbeat_task: asyncio.Task[None] | None = None
        try:
            welcome = await self._receive_welcome(websocket)
            await self._send_hello(websocket)
            heartbeat_task = asyncio.create_task(self._heartbeat_loop(websocket))
            await self._replay_spool(websocket, welcome)
            async for frame in websocket:
                await self._handle_frame(websocket, frame)
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task
            self._ws = None

    async def publish_attempt_event(
        self,
        message_type: str,
        payload: dict[str, Any],
        *,
        build_job_id: str,
        attempt_id: str,
    ) -> Envelope:
        """Create, spool, and send an attempt-scoped outbound event."""

        async with self._publish_lock:
            seq = await self.event_spool.next_seq(attempt_id)
            envelope = new_envelope(
                message_type,
                payload,
                build_job_id=build_job_id,
                attempt_id=attempt_id,
                seq=seq,
            )
            await self.event_spool.append(envelope)
            if self._ws is not None:
                await self._ws.send(envelope.to_json())
            return envelope

    async def _receive_welcome(self, websocket: WebSocketLike) -> Envelope:
        iterator = websocket.__aiter__()
        try:
            frame = await anext(iterator)
        except StopAsyncIteration as exc:
            raise ProtocolError("Backend closed before welcome") from exc
        welcome = decode_frame(frame, allowed_types=INBOUND_MESSAGE_TYPES)
        if welcome.type != "welcome":
            raise ProtocolError("First backend frame must be welcome")
        payload = welcome.payload
        if payload.get("engine_id") != self.credentials.engine_id:
            raise ProtocolError("welcome engine_id does not match credentials")
        negotiated = payload.get("proto_negotiated")
        if negotiated != PROTOCOL_VERSION:
            raise ProtocolError("welcome negotiated an unsupported protocol version")
        interval = payload.get("heartbeat_interval_seconds")
        if isinstance(interval, int) and interval > 0:
            self._heartbeat_interval_seconds = interval
        return welcome

    async def _send_hello(self, websocket: WebSocketLike) -> None:
        payload = {
            "version": __version__,
            "proto_version": PROTOCOL_VERSION,
            "image_manifest_version": self.config.image_manifest_version,
            "capabilities": config_capabilities(self.config),
            "max_concurrency": self.config.max_concurrency,
        }
        await websocket.send(new_envelope("hello", payload).to_json())

    async def _replay_spool(self, websocket: WebSocketLike, welcome: Envelope) -> None:
        cursors = last_sequences(welcome.payload)
        for envelope in await self.event_spool.replay_after(cursors):
            await websocket.send(envelope.to_json())

    async def _heartbeat_loop(self, websocket: WebSocketLike) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval_seconds)
            await websocket.send(
                new_envelope("heartbeat", self.heartbeat_provider().to_payload()).to_json(),
            )

    async def _handle_frame(self, websocket: WebSocketLike, frame: str | bytes) -> None:
        envelope = decode_frame(frame, allowed_types=INBOUND_MESSAGE_TYPES)
        match envelope.type:
            case "ping":
                await websocket.send(new_envelope("pong").to_json())
            case "job.assign":
                await self._handle_assign(websocket, envelope)
            case "cancel":
                await self._handle_cancel(websocket, envelope)
            case "drain":
                await self.command_handlers.drain(envelope.payload)
            case "cache.reset":
                await self.command_handlers.cache_reset(envelope.payload)
            case "welcome":
                raise ProtocolError("welcome is only valid as the first backend frame")
            case _:
                raise ProtocolError(f"Unhandled inbound message type: {envelope.type}")

    async def _handle_assign(self, websocket: WebSocketLike, envelope: Envelope) -> None:
        build_job_id, attempt_id = attempt_fields(envelope)
        payload = envelope.payload | {"build_job_id": build_job_id, "attempt_id": attempt_id}
        result = await self.command_handlers.assign(payload)
        state = result.state or ("ASSIGNED" if result.accepted else "FAILED")
        await self.publish_attempt_event(
            "job.ack",
            {"build_job_id": build_job_id, "attempt_id": attempt_id, "state": state},
            build_job_id=build_job_id,
            attempt_id=attempt_id,
        )
        del websocket

    async def _handle_cancel(self, websocket: WebSocketLike, envelope: Envelope) -> None:
        result = await self.command_handlers.cancel(envelope.payload)
        if result.state != "CANCELLED":
            return
        build_job_id = _required_payload_str(envelope.payload, "build_job_id")
        explicit_attempt_id = envelope.attempt_id or _optional_payload_str(
            envelope.payload,
            "attempt_id",
        )
        attempt_ids = result.affected_attempt_ids
        if not attempt_ids and explicit_attempt_id is not None:
            attempt_ids = (explicit_attempt_id,)
        for attempt_id in attempt_ids:
            await self.publish_attempt_event(
                "job.ack",
                {"build_job_id": build_job_id, "attempt_id": attempt_id, "state": "CANCELLED"},
                build_job_id=build_job_id,
                attempt_id=attempt_id,
            )
        del websocket

    async def _sleep_backoff(self, failures: int) -> None:
        with suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=self.backoff.delay(failures))


def websocket_url(backend_url: str) -> str:
    """Return the agent WSS URL for a configured backend base URL."""

    parsed = urlparse(backend_url)
    if parsed.scheme not in {"http", "https", "ws", "wss"}:
        raise ProtocolError("backend_url must use http(s) or ws(s)")
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
    base_path = parsed.path.rstrip("/")
    path = base_path if base_path.endswith("/ws") else f"{base_path}{AGENT_WS_PATH}"
    return urlunparse((scheme, parsed.netloc, path, "", "", ""))


def uplink_headers(config: EngineConfig, credentials: EngineCredentials) -> dict[str, str]:
    """Return WSS upgrade headers required by the protocol contract."""

    headers = client_headers_for_credentials(credentials)
    headers.update(
        {
            "X-Build-Engine-Proto": str(PROTOCOL_VERSION),
            "X-Build-Engine-Version": __version__,
            "X-Image-Manifest-Version": config.image_manifest_version,
        }
    )
    return headers


def _default_event_spool(config: EngineConfig) -> EventSpoolLike:
    from build_engine.queue.store import SQLiteEventOutbox, SQLiteQueueStore

    return SQLiteEventOutbox(SQLiteQueueStore(config.state_dir / "queue.sqlite"))


def _default_command_handlers(config: EngineConfig) -> CommandHandlers:
    from build_engine.queue.handlers import SQLiteCommandHandlers
    from build_engine.queue.store import SQLiteQueueStore

    return SQLiteCommandHandlers(SQLiteQueueStore(config.state_dir / "queue.sqlite"))


async def _websockets_connector(
    url: str,
    headers: Mapping[str, str],
) -> WebSocketLike:
    return await websockets.connect(
        url,
        additional_headers=dict(headers),
        max_size=1_048_576,
    )


def _required_payload_str(payload: dict[str, Any], key: str) -> str:
    value = _optional_payload_str(payload, key)
    if value is None:
        raise ProtocolError(f"payload {key} is required")
    return value


def _optional_payload_str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"payload {key} must be a non-empty string")
    return value

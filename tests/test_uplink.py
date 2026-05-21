"""Uplink lifecycle tests with a mock backend websocket."""

from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from build_engine.agent.auth import ensure_engine_certificate
from build_engine.agent.protocol import Envelope, new_envelope
from build_engine.agent.uplink import (
    BackoffPolicy,
    BuildEngineUplink,
    EventSpool,
    InMemoryCommandHandlers,
    WebSocketLike,
    uplink_headers,
    websocket_url,
)
from build_engine.config import EngineCredentials, load_config


class FakeWebSocket(WebSocketLike):
    """Queue-backed websocket used by uplink tests."""

    def __init__(self, inbound: list[str | bytes | None]) -> None:
        self._inbound = asyncio.Queue[str | bytes | None]()
        for frame in inbound:
            self._inbound.put_nowait(frame)
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        return self

    async def __anext__(self) -> str | bytes:
        frame = await self._inbound.get()
        if frame is None:
            raise StopAsyncIteration
        return frame

    async def close(self, code: int = 1000, reason: str = "") -> None:
        del code, reason
        self.closed = True
        self._inbound.put_nowait(None)


@pytest.fixture
def engine_material(tmp_path: Path) -> tuple[Any, EngineCredentials]:
    cert_path = tmp_path / "engine.crt"
    key_path = tmp_path / "engine.key"
    ensure_engine_certificate(cert_path, key_path, common_name="test-engine")
    config = load_config(
        config_path=tmp_path / "missing.toml",
        credentials_path=tmp_path / "credentials.toml",
        overrides={
            "backend_url": "https://agent.example",
            "name": "test-engine",
            "cert_path": cert_path,
            "key_path": key_path,
            "state_dir": tmp_path,
        },
    )
    credentials = EngineCredentials(
        engine_id="11111111-1111-1111-1111-111111111111",
        engine_secret="secret",
        backend_cert_fingerprint="a" * 64,
        session_jwt="session-token",
        session_jwt_expires_at="2030-01-01T00:00:00+00:00",
        cert_path=cert_path,
        key_path=key_path,
        backend_url="https://agent.example",
        name="test-engine",
    )
    return config, credentials


def test_websocket_url_converts_backend_base_url() -> None:
    assert (
        websocket_url("https://agent.example")
        == "wss://agent.example/api/v1/build-engines/agent/ws"
    )
    assert websocket_url("http://localhost:8000/base") == (
        "ws://localhost:8000/base/api/v1/build-engines/agent/ws"
    )


def test_uplink_headers_include_auth_and_protocol(
    engine_material: tuple[Any, EngineCredentials],
) -> None:
    config, credentials = engine_material

    headers = uplink_headers(config, credentials)

    assert headers["Authorization"] == "Bearer session-token"
    assert headers["X-Build-Engine-Proto"] == "1"
    assert headers["X-Image-Manifest-Version"] == "1.0.0"


def test_backoff_policy_caps_delay() -> None:
    policy = BackoffPolicy(initial_seconds=1, multiplier=2, max_seconds=5)

    assert [policy.delay(value) for value in range(1, 6)] == [1, 2, 4, 5, 5]


def test_connect_once_negotiates_hello_handles_ping_and_assignment(
    monkeypatch: pytest.MonkeyPatch,
    engine_material: tuple[Any, EngineCredentials],
    tmp_path: Path,
) -> None:
    asyncio.run(
        _connect_once_negotiates_hello_handles_ping_and_assignment(
            monkeypatch,
            engine_material,
            tmp_path,
        )
    )


async def _connect_once_negotiates_hello_handles_ping_and_assignment(
    monkeypatch: pytest.MonkeyPatch,
    engine_material: tuple[Any, EngineCredentials],
    tmp_path: Path,
) -> None:
    config, credentials = engine_material
    build_job_id = "22222222-2222-2222-2222-222222222222"
    attempt_id = "33333333-3333-3333-3333-333333333333"
    welcome = _welcome(credentials.engine_id, heartbeat_interval_seconds=60)
    websocket = FakeWebSocket(
        [
            welcome.to_json(),
            new_envelope("ping").to_json(),
            new_envelope(
                "job.assign",
                {"build_job_id": build_job_id, "attempt_id": attempt_id},
                build_job_id=build_job_id,
                attempt_id=attempt_id,
            ).to_json(),
            new_envelope("cancel", {"build_job_id": build_job_id}).to_json(),
            new_envelope("cache.reset", {"site_id": None}).to_json(),
            new_envelope("drain").to_json(),
            None,
        ]
    )
    handlers = InMemoryCommandHandlers()

    async def connector(
        url: str,
        headers: Mapping[str, str],
        ssl_context: ssl.SSLContext | None,
    ) -> WebSocketLike:
        assert url == "wss://agent.example/api/v1/build-engines/agent/ws"
        assert headers["Authorization"] == "Bearer session-token"
        assert ssl_context is not None
        return websocket

    monkeypatch.setattr(
        "build_engine.agent.auth.BuildEngineAuthClient.verify_backend_tls_pin",
        lambda self: None,
    )
    uplink = BuildEngineUplink(
        config,
        credentials,
        event_spool=EventSpool(tmp_path / "events.jsonl"),
        command_handlers=handlers,
        connector=connector,
    )

    await uplink.connect_once()

    sent_types = [message["type"] for message in websocket.sent]
    assert sent_types == ["hello", "pong", "job.ack", "job.ack"]
    assert websocket.sent[-2]["payload"]["state"] == "ASSIGNED"
    assert websocket.sent[-1]["payload"]["state"] == "CANCELLED"
    assert handlers.draining is True
    assert handlers.cache_reset_scopes == [None]


def test_event_spool_replays_from_backend_last_seq(
    monkeypatch: pytest.MonkeyPatch,
    engine_material: tuple[Any, EngineCredentials],
    tmp_path: Path,
) -> None:
    asyncio.run(_event_spool_replays_from_backend_last_seq(monkeypatch, engine_material, tmp_path))


async def _event_spool_replays_from_backend_last_seq(
    monkeypatch: pytest.MonkeyPatch,
    engine_material: tuple[Any, EngineCredentials],
    tmp_path: Path,
) -> None:
    config, credentials = engine_material
    build_job_id = "22222222-2222-2222-2222-222222222222"
    attempt_id = "33333333-3333-3333-3333-333333333333"
    spool = EventSpool(tmp_path / "events.jsonl")
    await spool.append(
        new_envelope(
            "status",
            {"phase": "PREPARING"},
            build_job_id=build_job_id,
            attempt_id=attempt_id,
            seq=1,
        )
    )
    await spool.append(
        new_envelope(
            "status",
            {"phase": "BUILDING"},
            build_job_id=build_job_id,
            attempt_id=attempt_id,
            seq=2,
        )
    )
    websocket = FakeWebSocket(
        [
            _welcome(credentials.engine_id, last_seq={attempt_id: 1}).to_json(),
            None,
        ]
    )

    async def connector(
        url: str,
        headers: Mapping[str, str],
        ssl_context: ssl.SSLContext | None,
    ) -> WebSocketLike:
        del url, headers, ssl_context
        return websocket

    monkeypatch.setattr(
        "build_engine.agent.auth.BuildEngineAuthClient.verify_backend_tls_pin",
        lambda self: None,
    )
    uplink = BuildEngineUplink(
        config,
        credentials,
        event_spool=spool,
        connector=connector,
    )

    await uplink.connect_once()

    assert [message["type"] for message in websocket.sent] == ["hello", "status"]
    assert websocket.sent[-1]["seq"] == 2
    assert websocket.sent[-1]["payload"]["phase"] == "BUILDING"


def _welcome(
    engine_id: str,
    *,
    heartbeat_interval_seconds: int = 15,
    last_seq: dict[str, int] | None = None,
) -> Envelope:
    return new_envelope(
        "welcome",
        {
            "engine_id": engine_id,
            "server_time": "2030-01-01T00:00:00Z",
            "proto_negotiated": 1,
            "heartbeat_interval_seconds": heartbeat_interval_seconds,
            "last_seq": last_seq or {},
        },
    )

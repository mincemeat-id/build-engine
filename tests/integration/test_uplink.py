"""In-process websocket integration tests for reconnect replay."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import websockets
from _fakes import EventSpool, InMemoryCommandHandlers

from build_engine.agent.protocol import Envelope, new_envelope
from build_engine.agent.uplink import BuildEngineUplink, WebSocketLike
from build_engine.config import EngineConfig, EngineCredentials


def test_uplink_reconnect_replays_attempt_events_to_backend_cursor(
    engine_config_builder: Any,
    fake_credentials: EngineCredentials,
    tmp_path: Path,
) -> None:
    asyncio.run(
        _uplink_reconnect_replays_attempt_events_to_backend_cursor(
            engine_config_builder(),
            fake_credentials,
            tmp_path,
        )
    )


async def _uplink_reconnect_replays_attempt_events_to_backend_cursor(
    config: EngineConfig,
    credentials: EngineCredentials,
    tmp_path: Path,
) -> None:
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
    server = _ReplayServer(credentials.engine_id, attempt_id)
    async with websockets.serve(server.handle, "127.0.0.1", 0) as ws_server:
        socket = next(iter(ws_server.sockets))
        host, port = socket.getsockname()[:2]
        url = f"ws://{host}:{port}/api/v1/build-engines/agent/ws"

        async def connector(
            connect_url: str,
            headers: Mapping[str, str],
        ) -> WebSocketLike:
            assert connect_url == "wss://agent.example/api/v1/build-engines/agent/ws"
            assert headers["Authorization"] == "Bearer session-token"
            return await websockets.connect(
                url, additional_headers=dict(headers), max_size=1_048_576
            )

        uplink = BuildEngineUplink(
            config,
            credentials,
            event_spool=spool,
            command_handlers=InMemoryCommandHandlers(),
            connector=connector,
        )

        await uplink.connect_once()

    assert [message["type"] for message in server.messages] == ["hello", "status", "job.ack"]
    assert server.messages[1]["seq"] == 2
    assert server.messages[2]["payload"]["state"] == "ASSIGNED"


class _ReplayServer:
    def __init__(self, engine_id: str, attempt_id: str) -> None:
        self.engine_id = engine_id
        self.attempt_id = attempt_id
        self.messages: list[dict[str, Any]] = []

    async def handle(self, websocket: Any) -> None:
        await websocket.send(_welcome(self.engine_id, last_seq={self.attempt_id: 1}).to_json())
        await websocket.send(
            new_envelope(
                "job.assign",
                {
                    "build_job_id": "44444444-4444-4444-4444-444444444444",
                    "attempt_id": "55555555-5555-5555-5555-555555555555",
                },
                build_job_id="44444444-4444-4444-4444-444444444444",
                attempt_id="55555555-5555-5555-5555-555555555555",
            ).to_json()
        )
        for _ in range(3):
            self.messages.append(json.loads(await websocket.recv()))


def _welcome(
    engine_id: str,
    *,
    last_seq: dict[str, int] | None = None,
) -> Envelope:
    return new_envelope(
        "welcome",
        {
            "engine_id": engine_id,
            "server_time": "2030-01-01T00:00:00Z",
            "proto_negotiated": 1,
            "heartbeat_interval_seconds": 60,
            "last_seq": last_seq or {},
        },
    )

"""Shared test doubles for build-engine tests."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from build_engine.agent.protocol import (
    OUTBOUND_MESSAGE_TYPES,
    Envelope,
    ProtocolError,
    decode_frame,
)
from build_engine.agent.uplink import CommandResult


@dataclass(slots=True)
class InMemoryCommandHandlers:
    attempts: dict[tuple[str, str], str] = field(default_factory=dict)
    cancelled: set[str] = field(default_factory=set)
    cache_reset_scopes: list[str | None] = field(default_factory=list)
    draining: bool = False

    async def assign(self, payload: dict[str, Any]) -> CommandResult:
        build_job_id = _required_payload_str(payload, "build_job_id")
        attempt_id = _required_payload_str(payload, "attempt_id")
        key = (build_job_id, attempt_id)
        state = self.attempts.setdefault(key, "ASSIGNED")
        return CommandResult(state=state)

    async def cancel(self, payload: dict[str, Any]) -> CommandResult:
        build_job_id = _required_payload_str(payload, "build_job_id")
        self.cancelled.add(build_job_id)
        affected: list[str] = []
        for key in tuple(self.attempts):
            if key[0] == build_job_id:
                self.attempts[key] = "CANCELLED"
                affected.append(key[1])
        return CommandResult(state="CANCELLED", affected_attempt_ids=tuple(affected))

    async def drain(self, payload: dict[str, Any]) -> CommandResult:
        del payload
        self.draining = True
        return CommandResult(state="DRAINING")

    async def cache_reset(self, payload: dict[str, Any]) -> CommandResult:
        site_id = payload.get("site_id")
        if site_id is not None and not isinstance(site_id, str):
            raise ProtocolError("cache.reset site_id must be a string or null")
        self.cache_reset_scopes.append(site_id)
        return CommandResult(state="WIPED")


class EventSpool:
    """Durable JSONL spool retained as a lightweight test fake."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def append(self, envelope: Envelope) -> None:
        if envelope.attempt_id is None or envelope.seq is None:
            raise ProtocolError("Spool events require attempt_id and seq")
        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(envelope.to_json())
                handle.write("\n")

    async def replay_after(self, cursors: Mapping[str, int]) -> list[Envelope]:
        if not self.path.exists():
            return []
        events: list[Envelope] = []
        async with self._lock:
            with self.path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    envelope = decode_frame(line, allowed_types=OUTBOUND_MESSAGE_TYPES)
                    if envelope.attempt_id is None or envelope.seq is None:
                        continue
                    if envelope.seq > cursors.get(envelope.attempt_id, 0):
                        events.append(envelope)
        return events

    async def next_seq(self, attempt_id: str) -> int:
        highest = 0
        if self.path.exists():
            async with self._lock:
                with self.path.open(encoding="utf-8") as handle:
                    for line in handle:
                        if not line.strip():
                            continue
                        envelope = decode_frame(line, allowed_types=OUTBOUND_MESSAGE_TYPES)
                        if envelope.attempt_id == attempt_id and envelope.seq is not None:
                            highest = max(highest, envelope.seq)
        return highest + 1


def _required_payload_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"payload {key} is required")
    return value

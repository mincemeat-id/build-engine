"""Uplink command handlers backed by the durable SQLite queue."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from build_engine.agent.protocol import ProtocolError
from build_engine.agent.uplink import CommandResult
from build_engine.executor.cache import reset_cache
from build_engine.queue.store import SQLiteQueueStore

DRAIN_MARKER_FILENAME = "drain.json"


@dataclass(slots=True)
class SQLiteCommandHandlers:
    """Persist backend commands into the local durable queue."""

    store: SQLiteQueueStore
    draining: bool = False
    cache_reset_scopes: list[str | None] | None = None

    def __post_init__(self) -> None:
        self.draining = self.draining or _drain_marker_path(self.store).exists()

    async def assign(self, payload: dict[str, Any]) -> CommandResult:
        """Persist an idempotent assignment."""

        self.draining = _drain_marker_path(self.store).exists()
        if self.draining:
            return CommandResult(accepted=False, state="FAILED", detail="Engine is draining")
        result = self.store.enqueue(payload)
        return CommandResult(state=result.job.state)

    async def cancel(self, payload: dict[str, Any]) -> CommandResult:
        """Mark all local attempts for a build job cancelled."""

        build_job_id = _required_payload_str(payload, "build_job_id")
        attempt_id = payload.get("attempt_id")
        if attempt_id is not None and not isinstance(attempt_id, str):
            raise ProtocolError("cancel attempt_id must be a string")
        affected = self.store.request_cancel(
            build_job_id=build_job_id,
            attempt_id=attempt_id,
        )
        return CommandResult(state="CANCELLING", affected_attempt_ids=affected)

    async def drain(self, payload: dict[str, Any]) -> CommandResult:
        """Stop accepting new assignments."""

        del payload
        self.draining = True
        _write_drain_marker(_drain_marker_path(self.store))
        return CommandResult(state="DRAINING")

    async def resume(self, payload: dict[str, Any]) -> CommandResult:
        """Resume accepting assignments after a local drain."""

        del payload
        self.draining = False
        _drain_marker_path(self.store).unlink(missing_ok=True)
        return CommandResult(state="READY")

    async def cache_reset(self, payload: dict[str, Any]) -> CommandResult:
        """Reset the requested local cache scope."""

        site_id = payload.get("site_id")
        if site_id is not None and not isinstance(site_id, str):
            raise ProtocolError("cache.reset site_id must be a string or null")
        reset_cache(self.store.path.parent, site_id=site_id)
        if self.cache_reset_scopes is None:
            self.cache_reset_scopes = []
        self.cache_reset_scopes.append(site_id)
        return CommandResult(state="WIPED")


def _required_payload_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"payload {key} is required")
    return value


def _drain_marker_path(store: SQLiteQueueStore) -> Path:
    return store.path.parent / DRAIN_MARKER_FILENAME


def _write_drain_marker(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "draining": True,
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    temporary.replace(path)

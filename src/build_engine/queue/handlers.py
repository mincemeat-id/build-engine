"""Uplink command handlers backed by the durable SQLite queue."""

from dataclasses import dataclass
from typing import Any

from build_engine.agent.protocol import ProtocolError
from build_engine.agent.uplink import CommandResult
from build_engine.queue.store import SQLiteQueueStore


@dataclass(slots=True)
class SQLiteCommandHandlers:
    """Persist backend commands into the local durable queue."""

    store: SQLiteQueueStore
    draining: bool = False
    cache_reset_scopes: list[str | None] | None = None

    async def assign(self, payload: dict[str, Any]) -> CommandResult:
        """Persist an idempotent assignment."""

        if self.draining:
            return CommandResult(accepted=False, state="FAILED", detail="Engine is draining")
        result = self.store.enqueue(payload)
        return CommandResult(state=result.job.state)

    async def cancel(self, payload: dict[str, Any]) -> CommandResult:
        """Mark all local attempts for a build job cancelled."""

        build_job_id = _required_payload_str(payload, "build_job_id")
        affected: list[str] = []
        for job in self.store.jobs_for_build(build_job_id):
            if job.state not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                self.store.transition(attempt_id=job.attempt_id, state="CANCELLED")
                affected.append(job.attempt_id)
        return CommandResult(state="CANCELLED", affected_attempt_ids=tuple(affected))

    async def drain(self, payload: dict[str, Any]) -> CommandResult:
        """Stop accepting new assignments."""

        del payload
        self.draining = True
        return CommandResult(state="DRAINING")

    async def cache_reset(self, payload: dict[str, Any]) -> CommandResult:
        """Record the requested cache reset scope for later cache implementation."""

        site_id = payload.get("site_id")
        if site_id is not None and not isinstance(site_id, str):
            raise ProtocolError("cache.reset site_id must be a string or null")
        if self.cache_reset_scopes is None:
            self.cache_reset_scopes = []
        self.cache_reset_scopes.append(site_id)
        return CommandResult(state="WIPED")


def _required_payload_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"payload {key} is required")
    return value

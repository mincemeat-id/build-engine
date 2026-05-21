"""Heartbeat payload helpers for the build-engine uplink."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class HeartbeatSnapshot:
    """Capacity and health values sent on the WSS heartbeat loop."""

    workers_busy: int
    workers_total: int
    queue_depth: int
    cache_size_bytes: int
    disk_free_bytes: int

    def to_payload(self) -> dict[str, int]:
        """Return the protocol payload."""

        return asdict(self)


def idle_heartbeat(*, workers_total: int) -> HeartbeatSnapshot:
    """Return a conservative default heartbeat before Stage 3 collectors exist."""

    return HeartbeatSnapshot(
        workers_busy=0,
        workers_total=workers_total,
        queue_depth=0,
        cache_size_bytes=0,
        disk_free_bytes=0,
    )

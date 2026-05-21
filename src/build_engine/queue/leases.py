"""Lease helpers for durable build-attempt queue workers."""

from dataclasses import dataclass

from build_engine.queue.store import JobRecord, SQLiteQueueStore


@dataclass(frozen=True, slots=True)
class QueueLease:
    """A worker-owned queue lease."""

    owner: str
    job: JobRecord
    visibility_timeout_seconds: int

    def refresh(self, store: SQLiteQueueStore) -> JobRecord:
        """Extend this lease and return the refreshed job record."""

        return store.refresh_lease(
            attempt_id=self.job.attempt_id,
            lease_owner=self.owner,
            visibility_timeout_seconds=self.visibility_timeout_seconds,
        )


def acquire_queue_lease(
    store: SQLiteQueueStore,
    *,
    owner: str,
    visibility_timeout_seconds: int,
) -> QueueLease | None:
    """Lease the next available attempt for a worker."""

    job = store.acquire_lease(
        lease_owner=owner,
        visibility_timeout_seconds=visibility_timeout_seconds,
    )
    if job is None:
        return None
    return QueueLease(
        owner=owner,
        job=job,
        visibility_timeout_seconds=visibility_timeout_seconds,
    )

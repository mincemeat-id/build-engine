"""Dead-letter queue helpers for poison build attempts."""

from build_engine.queue.store import DeadLetterRecord, SQLiteQueueStore


def record_executor_crash(
    store: SQLiteQueueStore,
    *,
    attempt_id: str,
    error: str,
) -> bool:
    """Record a local crash and return whether the attempt is now dead-lettered."""

    job = store.record_executor_crash(attempt_id=attempt_id, error=error)
    return job.state == "FAILED"


def list_dead_letters(store: SQLiteQueueStore) -> tuple[DeadLetterRecord, ...]:
    """Return dead-lettered attempts."""

    return store.dlq_entries()

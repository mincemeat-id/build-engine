"""Durable SQLite queue tests."""

import asyncio
import sqlite3
from pathlib import Path

from build_engine.agent.protocol import new_envelope
from build_engine.queue.dlq import list_dead_letters, record_executor_crash
from build_engine.queue.handlers import SQLiteCommandHandlers
from build_engine.queue.leases import acquire_queue_lease
from build_engine.queue.store import SQLiteEventOutbox, SQLiteQueueStore


def test_initialize_creates_wal_schema(tmp_path: Path) -> None:
    store = SQLiteQueueStore(tmp_path / "queue.sqlite")

    store.initialize()

    with sqlite3.connect(tmp_path / "queue.sqlite") as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        journal_mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'",
            ).fetchall()
        }
    assert version == 1
    assert journal_mode == "wal"
    assert {"jobs", "events", "dlq"} <= tables


def test_enqueue_is_idempotent_by_build_job_and_attempt(tmp_path: Path) -> None:
    store = SQLiteQueueStore(tmp_path / "queue.sqlite")
    store.initialize()
    payload = _payload()

    first = store.enqueue(payload)
    second = store.enqueue(payload | {"root_directory": "ignored-on-duplicate"})

    assert first.inserted is True
    assert second.inserted is False
    assert second.job.payload == payload
    assert store.queue_depth() == 1


def test_lease_can_be_recovered_after_restart_when_expired(tmp_path: Path) -> None:
    db_path = tmp_path / "queue.sqlite"
    store = SQLiteQueueStore(db_path)
    store.initialize()
    store.enqueue(_payload())

    lease = acquire_queue_lease(
        store,
        owner="worker-a",
        visibility_timeout_seconds=-1,
    )
    assert lease is not None
    assert lease.job.state == "LEASED"

    restarted_store = SQLiteQueueStore(db_path)
    restarted_store.initialize()
    recovered = acquire_queue_lease(
        restarted_store,
        owner="worker-b",
        visibility_timeout_seconds=30,
    )

    assert recovered is not None
    assert recovered.job.lease_owner == "worker-b"
    assert recovered.job.attempts == 2


def test_lease_refresh_requires_owner(tmp_path: Path) -> None:
    store = SQLiteQueueStore(tmp_path / "queue.sqlite")
    store.initialize()
    store.enqueue(_payload())
    lease = acquire_queue_lease(store, owner="worker-a", visibility_timeout_seconds=30)
    assert lease is not None

    refreshed = lease.refresh(store)

    assert refreshed.lease_owner == "worker-a"


def test_sqlite_event_outbox_replays_after_backend_cursor(tmp_path: Path) -> None:
    asyncio.run(_sqlite_event_outbox_replays_after_backend_cursor(tmp_path))


async def _sqlite_event_outbox_replays_after_backend_cursor(tmp_path: Path) -> None:
    store = SQLiteQueueStore(tmp_path / "queue.sqlite")
    store.initialize()
    payload = _payload()
    store.enqueue(payload)
    outbox = SQLiteEventOutbox(store)
    await outbox.append(
        new_envelope(
            "status",
            {"phase": "PREPARING"},
            build_job_id=payload["build_job_id"],
            attempt_id=payload["attempt_id"],
            seq=1,
        )
    )
    await outbox.append(
        new_envelope(
            "status",
            {"phase": "BUILDING"},
            build_job_id=payload["build_job_id"],
            attempt_id=payload["attempt_id"],
            seq=2,
        )
    )

    replay = await outbox.replay_after({payload["attempt_id"]: 1})

    assert [event.seq for event in replay] == [2]
    assert replay[0].payload == {"phase": "BUILDING"}
    assert await outbox.next_seq(payload["attempt_id"]) == 3


def test_executor_crashes_dead_letter_after_three_local_attempts(tmp_path: Path) -> None:
    store = SQLiteQueueStore(tmp_path / "queue.sqlite")
    store.initialize()
    payload = _payload()
    store.enqueue(payload)

    for _ in range(2):
        lease = acquire_queue_lease(store, owner="worker-a", visibility_timeout_seconds=30)
        assert lease is not None
        assert record_executor_crash(store, attempt_id=payload["attempt_id"], error="boom") is False

    lease = acquire_queue_lease(store, owner="worker-a", visibility_timeout_seconds=30)
    assert lease is not None
    assert record_executor_crash(store, attempt_id=payload["attempt_id"], error="boom") is True

    entries = list_dead_letters(store)
    assert len(entries) == 1
    assert entries[0].attempt_id == payload["attempt_id"]
    assert entries[0].attempts == 3
    failed = store.get_job(payload["build_job_id"], payload["attempt_id"])
    assert failed is not None
    assert failed.state == "FAILED"


def test_sqlite_command_handlers_enqueue_cancel_and_drain(tmp_path: Path) -> None:
    asyncio.run(_sqlite_command_handlers_enqueue_cancel_and_drain(tmp_path))


async def _sqlite_command_handlers_enqueue_cancel_and_drain(tmp_path: Path) -> None:
    store = SQLiteQueueStore(tmp_path / "queue.sqlite")
    store.initialize()
    payload = _payload()
    handlers = SQLiteCommandHandlers(store)

    assigned = await handlers.assign(payload)
    duplicate = await handlers.assign(payload)
    cancelled = await handlers.cancel({"build_job_id": payload["build_job_id"]})
    drained = await handlers.drain({})
    rejected = await handlers.assign(_payload(attempt_id="44444444-4444-4444-4444-444444444444"))

    assert assigned.state == "QUEUED"
    assert duplicate.state == "QUEUED"
    assert cancelled.affected_attempt_ids == (payload["attempt_id"],)
    cancelled_job = store.get_job(payload["build_job_id"], payload["attempt_id"])
    assert cancelled_job is not None
    assert cancelled_job.state == "CANCELLED"
    assert drained.state == "DRAINING"
    assert rejected.accepted is False
    assert rejected.state == "FAILED"


def _payload(
    *,
    attempt_id: str = "33333333-3333-3333-3333-333333333333",
) -> dict[str, str]:
    return {
        "build_job_id": "22222222-2222-2222-2222-222222222222",
        "attempt_id": attempt_id,
        "site_id": "site-a",
        "source_download_url": "https://storage.example/source.tar.gz",
        "source_sha256": "a" * 64,
    }

"""Transactional SQLite queue, lease, cancellation, and outbox tests."""

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from build_engine.agent.protocol import new_envelope
from build_engine.queue.dlq import list_dead_letters, record_executor_crash
from build_engine.queue.handlers import DRAIN_MARKER_FILENAME, SQLiteCommandHandlers
from build_engine.queue.leases import acquire_queue_lease
from build_engine.queue.store import QueueError, SQLiteEventOutbox, SQLiteQueueStore


def test_initialize_creates_wal_schema(tmp_path: Path) -> None:
    store = SQLiteQueueStore(tmp_path / "queue.sqlite")

    with sqlite3.connect(tmp_path / "queue.sqlite") as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        journal_mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }

    assert version == 2
    assert journal_mode == "wal"
    assert {"jobs", "events", "dlq"} <= tables
    store.close()


def test_enqueue_is_idempotent_and_keeps_transient_assignment_only_in_memory(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "queue.sqlite"
    store = SQLiteQueueStore(db_path)
    payload = _payload() | {
        "source_download_url": "https://storage.example/one-shot",
        "secret_env": {"TOKEN": "never-write"},
    }

    first = store.enqueue(payload)
    second = store.enqueue(payload | {"root_directory": "ignored-on-duplicate"})

    assert first.inserted is True
    assert second.inserted is False
    assert second.job.payload == payload
    store.close()

    reopened = SQLiteQueueStore(db_path)
    persisted = reopened.get_job(
        "22222222-2222-2222-2222-222222222222",
        "33333333-3333-3333-3333-333333333333",
    )
    assert persisted is not None
    assert "source_download_url" not in persisted.payload
    assert "secret_env" not in persisted.payload


def test_current_attempt_tracks_newer_assignment_for_same_build(tmp_path: Path) -> None:
    store = SQLiteQueueStore(tmp_path / "queue.sqlite")
    first = _payload(attempt_id="33333333-3333-3333-3333-333333333333")
    second = _payload(attempt_id="44444444-4444-4444-4444-444444444444")

    store.enqueue(first)
    store.enqueue(second)

    assert not store.is_current_attempt(
        build_job_id=first["build_job_id"], attempt_id=first["attempt_id"]
    )
    assert store.is_current_attempt(
        build_job_id=second["build_job_id"], attempt_id=second["attempt_id"]
    )


def test_lease_can_be_recovered_after_restart_when_expired(tmp_path: Path) -> None:
    db_path = tmp_path / "queue.sqlite"
    store = SQLiteQueueStore(db_path)
    store.enqueue(_payload())

    lease = acquire_queue_lease(store, owner="worker-a", visibility_timeout_seconds=-1)
    assert lease is not None
    assert lease.job.state == "LEASED"
    store.close()

    restarted_store = SQLiteQueueStore(db_path)
    recovered = acquire_queue_lease(
        restarted_store,
        owner="worker-b",
        visibility_timeout_seconds=30,
    )

    assert recovered is not None
    assert recovered.job.lease_owner == "worker-b"
    assert recovered.job.attempts == 2


def test_lease_refresh_requires_owner_and_token(tmp_path: Path) -> None:
    store = SQLiteQueueStore(tmp_path / "queue.sqlite")
    store.enqueue(_payload())
    lease = acquire_queue_lease(store, owner="worker-a", visibility_timeout_seconds=30)
    assert lease is not None

    refreshed = lease.refresh(store)
    assert refreshed.lease_owner == "worker-a"

    with pytest.raises(QueueError, match="unknown or unowned"):
        store.refresh_lease(
            attempt_id=lease.job.attempt_id,
            lease_owner="worker-b",
            lease_token=lease.token,
            visibility_timeout_seconds=30,
        )


def test_stale_worker_cannot_terminalize_reassigned_attempt(tmp_path: Path) -> None:
    store = SQLiteQueueStore(tmp_path / "queue.sqlite")
    payload = _payload()
    store.enqueue(payload)
    old_lease = acquire_queue_lease(store, owner="worker-a", visibility_timeout_seconds=-1)
    assert old_lease is not None
    store.transition(
        attempt_id=payload["attempt_id"],
        state="RUNNING",
        expected_state="LEASED",
        lease_owner=old_lease.owner,
        lease_token=old_lease.token,
    )
    new_lease = acquire_queue_lease(store, owner="worker-b", visibility_timeout_seconds=30)
    assert new_lease is not None

    with pytest.raises(QueueError, match="stale lease token|owned by another|unknown attempt"):
        store.transition(
            attempt_id=payload["attempt_id"],
            state="SUCCEEDED",
            expected_state="LEASED",
            lease_owner=old_lease.owner,
            lease_token=old_lease.token,
        )


def test_cancel_request_is_separate_until_worker_observes_it(tmp_path: Path) -> None:
    store = SQLiteQueueStore(tmp_path / "queue.sqlite")
    payload = _payload()
    store.enqueue(payload)

    assert store.request_cancel(build_job_id=payload["build_job_id"]) == (payload["attempt_id"],)
    requested = store.get_job(payload["build_job_id"], payload["attempt_id"])
    assert requested is not None
    assert requested.state == "QUEUED"
    assert requested.cancel_requested is True

    assert store.acquire_lease(lease_owner="worker-a", visibility_timeout_seconds=30) is None
    cancelled = store.get_job(payload["build_job_id"], payload["attempt_id"])
    assert cancelled is not None
    assert cancelled.state == "CANCELLED"


def test_sqlite_event_outbox_deduplicates_identical_sequences_and_replays() -> None:
    asyncio.run(_outbox_test())


async def _outbox_test() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as directory:
        root = Path(directory)
        store = SQLiteQueueStore(root / "queue.sqlite")
        payload = _payload()
        store.enqueue(payload)
        outbox = SQLiteEventOutbox(store)
        event = new_envelope(
            "attempt.status",
            {"phase": "PREPARING"},
            build_job_id=payload["build_job_id"],
            attempt_id=payload["attempt_id"],
            seq=1,
        )
        await outbox.append(event)
        await outbox.append(event)
        replay = await outbox.replay_after({payload["attempt_id"]: 0})

        assert [item.seq for item in replay] == [1]
        assert await outbox.next_seq(payload["attempt_id"]) == 2
        await outbox.ack_through(payload["attempt_id"], 1)
        assert await outbox.replay_after({}) == []
        outbox.close()
        store.close()


def test_outbox_rejects_conflicting_duplicate_sequence(tmp_path: Path) -> None:
    async def run() -> None:
        store = SQLiteQueueStore(tmp_path / "queue.sqlite")
        payload = _payload()
        store.enqueue(payload)
        outbox = SQLiteEventOutbox(store)
        event = new_envelope(
            "attempt.status",
            {"phase": "PREPARING"},
            build_job_id=payload["build_job_id"],
            attempt_id=payload["attempt_id"],
            seq=1,
        )
        conflicting = new_envelope(
            "attempt.status",
            {"phase": "BUILDING"},
            build_job_id=payload["build_job_id"],
            attempt_id=payload["attempt_id"],
            seq=1,
        )
        await outbox.append(event)
        with pytest.raises(QueueError, match="Conflicting event"):
            await outbox.append(conflicting)
        outbox.close()
        store.close()

    asyncio.run(run())


def test_executor_crashes_dead_letter_after_three_local_attempts(tmp_path: Path) -> None:
    store = SQLiteQueueStore(tmp_path / "queue.sqlite")
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
    async def run() -> None:
        store = SQLiteQueueStore(tmp_path / "queue.sqlite")
        payload = _payload()
        handlers = SQLiteCommandHandlers(store)

        assigned = await handlers.assign(payload)
        duplicate = await handlers.assign(payload)
        cancelled = await handlers.cancel({"build_job_id": payload["build_job_id"]})
        drained = await handlers.drain({})
        resumed = await handlers.resume({})

        assert assigned.state == "QUEUED"
        assert duplicate.state == "QUEUED"
        assert cancelled.affected_attempt_ids == (payload["attempt_id"],)
        cancelled_job = store.get_job(payload["build_job_id"], payload["attempt_id"])
        assert cancelled_job is not None
        assert cancelled_job.state == "QUEUED"
        assert cancelled_job.cancel_requested is True
        assert drained.state == "DRAINING"
        assert resumed.state == "READY"
        assert (tmp_path / DRAIN_MARKER_FILENAME).exists() is False

    asyncio.run(run())


def _payload(*, attempt_id: str = "33333333-3333-3333-3333-333333333333") -> dict[str, Any]:
    return {
        "build_job_id": "22222222-2222-2222-2222-222222222222",
        "attempt_id": attempt_id,
        "site_id": "site-a",
        "source_sha256": "a" * 64,
    }

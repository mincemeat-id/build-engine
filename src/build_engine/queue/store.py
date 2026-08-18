"""Durable SQLite queue and event outbox for build attempts."""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from build_engine.agent.protocol import (
    OUTBOUND_MESSAGE_TYPES,
    ZERO_ENGINE_ID,
    Envelope,
    ProtocolError,
    decode_frame,
)

SCHEMA_VERSION = 2
MAX_LOCAL_CRASHES = 3


class QueueError(RuntimeError):
    """Raised when durable queue operations cannot be completed."""


@dataclass(frozen=True, slots=True)
class JobRecord:
    """One persisted build attempt."""

    build_job_id: str
    attempt_id: str
    payload: dict[str, Any]
    state: str
    attempts: int
    sequence_cursor: int
    lease_owner: str | None
    lease_expires_at: str | None
    error: str | None
    created_at: str
    updated_at: str
    lease_token: str | None = None
    cancel_requested: bool = False


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    """Result from idempotent enqueue."""

    job: JobRecord
    inserted: bool


@dataclass(frozen=True, slots=True)
class DeadLetterRecord:
    """Attempt parked after repeated local executor crashes."""

    build_job_id: str
    attempt_id: str
    payload: dict[str, Any]
    error: str
    attempts: int
    created_at: str


class SQLiteQueueStore:
    """SQLite WAL-backed local queue for build-engine attempts."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._db: sqlite3.Connection | None = None
        self._transient_payloads: dict[str, dict[str, Any]] = {}
        self.initialize()

    def initialize(self) -> None:
        """Create or migrate the SQLite schema."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version < 1:
                _create_v1_schema(db)
                _migrate_to_v2(db)
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version == 1:
                _migrate_to_v2(db)
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version > SCHEMA_VERSION:
                raise QueueError(
                    f"Queue schema version {version} is newer than supported {SCHEMA_VERSION}"
                )

    def enqueue(self, payload: dict[str, Any]) -> EnqueueResult:
        """Idempotently enqueue one attempt by `(build_job_id, attempt_id)`."""

        build_job_id = _required_payload_uuid(payload, "build_job_id")
        attempt_id = _required_payload_uuid(payload, "attempt_id")
        now = _utcnow()
        durable_payload = _durable_payload(payload)
        payload_json = _json_dumps(durable_payload)
        self._transient_payloads.setdefault(attempt_id, dict(payload))
        with self._connect() as db:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO jobs (
                    build_job_id, attempt_id, payload_json, state, attempts,
                    sequence_cursor, created_at, updated_at
                )
                VALUES (?, ?, ?, 'QUEUED', 0, 0, ?, ?)
                """,
                (build_job_id, attempt_id, payload_json, now, now),
            )
            inserted = cursor.rowcount == 1
            row = _job_row(db, build_job_id, attempt_id)
            if row is None:
                raise QueueError("Enqueued job could not be reloaded")
            return EnqueueResult(
                job=self._with_transient_payload(_job_from_row(row)),
                inserted=inserted,
            )

    def get_job(self, build_job_id: str, attempt_id: str) -> JobRecord | None:
        """Return one queued job, if present."""

        with self._connect() as db:
            row = _job_row(db, build_job_id, attempt_id)
        return self._with_transient_payload(_job_from_row(row)) if row is not None else None

    def jobs_for_build(self, build_job_id: str) -> tuple[JobRecord, ...]:
        """Return attempts for a backend build job."""

        with self._connect() as db:
            rows = db.execute(
                """
                SELECT * FROM jobs
                WHERE build_job_id = ?
                ORDER BY created_at, attempt_id
                """,
                (build_job_id,),
            ).fetchall()
        return tuple(self._with_transient_payload(_job_from_row(row)) for row in rows)

    def is_current_attempt(self, *, build_job_id: str, attempt_id: str) -> bool:
        """Return whether `attempt_id` is the newest locally known attempt for a build."""

        with self._connect() as db:
            row = db.execute(
                """
                SELECT rowid FROM jobs
                WHERE build_job_id = ? AND attempt_id = ?
                """,
                (build_job_id, attempt_id),
            ).fetchone()
            if row is None:
                raise QueueError("Cannot check freshness for unknown attempt")
            newer = db.execute(
                """
                SELECT 1 FROM jobs
                WHERE build_job_id = ? AND rowid > ?
                LIMIT 1
                """,
                (build_job_id, int(row["rowid"])),
            ).fetchone()
        return newer is None

    def acquire_lease(
        self,
        *,
        lease_owner: str,
        visibility_timeout_seconds: int,
    ) -> JobRecord | None:
        """Lease the oldest queued or expired attempt."""

        now_dt = datetime.now(UTC)
        now = _format_datetime(now_dt)
        expires_at = _format_datetime(now_dt + timedelta(seconds=visibility_timeout_seconds))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            while True:
                row = db.execute(
                    """
                    SELECT * FROM jobs
                    WHERE state = 'QUEUED'
                       OR (state IN ('LEASED', 'RUNNING') AND lease_expires_at <= ?)
                    ORDER BY created_at, attempt_id
                    LIMIT 1
                    """,
                    (now,),
                ).fetchone()
                if row is None:
                    db.commit()
                    return None
                if bool(row["cancel_requested"]):
                    db.execute(
                        """
                        UPDATE jobs
                        SET state = 'CANCELLED', lease_owner = NULL,
                            lease_token = NULL, lease_expires_at = NULL,
                            cancel_requested = 0, updated_at = ?
                        WHERE attempt_id = ?
                        """,
                        (now, row["attempt_id"]),
                    )
                    continue
                break
            lease_token = secrets.token_urlsafe(24)
            db.execute(
                """
                UPDATE jobs
                SET state = 'LEASED',
                    lease_owner = ?,
                    lease_token = ?,
                    lease_expires_at = ?,
                    attempts = attempts + 1,
                    updated_at = ?
                WHERE attempt_id = ?
                """,
                (lease_owner, lease_token, expires_at, now, row["attempt_id"]),
            )
            db.commit()
            refreshed = _job_row(db, row["build_job_id"], row["attempt_id"])
            if refreshed is None:
                raise QueueError("Leased job could not be reloaded")
            return self._with_transient_payload(_job_from_row(refreshed))

    def refresh_lease(
        self,
        *,
        attempt_id: str,
        lease_owner: str,
        visibility_timeout_seconds: int,
        lease_token: str | None = None,
    ) -> JobRecord:
        """Extend a live lease owned by `lease_owner`."""

        now_dt = datetime.now(UTC)
        now = _format_datetime(now_dt)
        expires_at = _format_datetime(now_dt + timedelta(seconds=visibility_timeout_seconds))
        with self._connect() as db:
            cursor = db.execute(
                """
                UPDATE jobs
                SET lease_expires_at = ?, updated_at = ?
                WHERE attempt_id = ? AND lease_owner = ?
                  AND (? IS NULL OR lease_token = ?)
                  AND state IN ('LEASED', 'RUNNING')
                """,
                (expires_at, now, attempt_id, lease_owner, lease_token, lease_token),
            )
            if cursor.rowcount != 1:
                raise QueueError("Cannot refresh lease for unknown or unowned attempt")
            row = db.execute("SELECT * FROM jobs WHERE attempt_id = ?", (attempt_id,)).fetchone()
            if row is None:
                raise QueueError("Refreshed job could not be reloaded")
            return self._with_transient_payload(_job_from_row(row))

    def transition(
        self,
        *,
        attempt_id: str,
        state: str,
        error: str | None = None,
        expected_state: str | None = None,
        lease_owner: str | None = None,
        lease_token: str | None = None,
    ) -> JobRecord:
        """Move an attempt to a new queue state."""

        if state not in {
            "QUEUED",
            "LEASED",
            "RUNNING",
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
            "TIMED_OUT",
        }:
            raise QueueError(f"Invalid queue state: {state}")
        now = _utcnow()
        terminal = state in {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"}
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT * FROM jobs WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if current is None:
                raise QueueError("Cannot transition unknown attempt")
            current_state = str(current["state"])
            if expected_state is not None and current_state != expected_state:
                raise QueueError("Cannot transition attempt from an unexpected state")
            if lease_owner is not None and current["lease_owner"] != lease_owner:
                raise QueueError("Cannot transition attempt owned by another worker")
            if lease_token is not None and current["lease_token"] != lease_token:
                raise QueueError("Cannot transition attempt with a stale lease token")
            if current_state == state:
                if expected_state is None and lease_owner is None and lease_token is None:
                    db.commit()
                    return self._with_transient_payload(_job_from_row(current))
                raise QueueError("Cannot transition attempt from the current state")
            if current_state not in _LEGAL_PREVIOUS_STATES[state]:
                raise QueueError("Cannot transition attempt from the current state")
            cursor = db.execute(
                """
                UPDATE jobs
                SET state = ?,
                    error = ?,
                    lease_owner = CASE WHEN ? THEN NULL ELSE lease_owner END,
                    lease_expires_at = CASE WHEN ? THEN NULL ELSE lease_expires_at END,
                    lease_token = CASE WHEN ? THEN NULL ELSE lease_token END,
                    cancel_requested = CASE WHEN ? THEN 0 ELSE cancel_requested END,
                    updated_at = ?
                WHERE attempt_id = ?
                  AND state = ?
                  AND (? IS NULL OR lease_owner = ?)
                  AND (? IS NULL OR lease_token = ?)
                """,
                (
                    state,
                    error,
                    terminal,
                    terminal,
                    terminal,
                    terminal,
                    now,
                    attempt_id,
                    current_state,
                    lease_owner,
                    lease_owner,
                    lease_token,
                    lease_token,
                ),
            )
            if cursor.rowcount != 1:
                raise QueueError("Cannot transition attempt after its state changed")
            row = db.execute("SELECT * FROM jobs WHERE attempt_id = ?", (attempt_id,)).fetchone()
            if row is None:
                raise QueueError("Transitioned job could not be reloaded")
            db.commit()
            return self._with_transient_payload(_job_from_row(row))

    def request_cancel(
        self,
        *,
        build_job_id: str,
        attempt_id: str | None = None,
    ) -> tuple[str, ...]:
        """Request cancellation without racing a worker's terminal transition."""

        now = _utcnow()
        with self._connect() as db:
            if attempt_id is None:
                rows = db.execute(
                    "SELECT attempt_id FROM jobs WHERE build_job_id = ? "
                    "AND state NOT IN ('SUCCEEDED', 'FAILED', 'CANCELLED', 'TIMED_OUT')",
                    (build_job_id,),
                ).fetchall()
                ids = tuple(str(row["attempt_id"]) for row in rows)
                db.execute(
                    "UPDATE jobs SET cancel_requested = 1, updated_at = ? "
                    "WHERE build_job_id = ? AND state NOT IN "
                    "('SUCCEEDED', 'FAILED', 'CANCELLED', 'TIMED_OUT')",
                    (now, build_job_id),
                )
            else:
                ids = (attempt_id,)
                db.execute(
                    "UPDATE jobs SET cancel_requested = 1, updated_at = ? "
                    "WHERE build_job_id = ? AND attempt_id = ? AND state NOT IN "
                    "('SUCCEEDED', 'FAILED', 'CANCELLED', 'TIMED_OUT')",
                    (now, build_job_id, attempt_id),
                )
        return ids

    def is_cancel_requested(self, attempt_id: str) -> bool:
        """Return the current cancellation request flag."""

        with self._connect() as db:
            row = db.execute(
                "SELECT cancel_requested FROM jobs WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def record_executor_crash(
        self,
        *,
        attempt_id: str,
        error: str,
        lease_owner: str | None = None,
        lease_token: str | None = None,
    ) -> JobRecord:
        """Requeue or dead-letter an attempt after a local executor crash."""

        now = _utcnow()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ownership_parameters = (
                attempt_id,
                lease_owner,
                lease_owner,
                lease_token,
                lease_token,
            )
            row = db.execute(
                """
                SELECT * FROM jobs
                WHERE attempt_id = ?
                  AND state IN ('LEASED', 'RUNNING')
                  AND (? IS NULL OR lease_owner = ?)
                  AND (? IS NULL OR lease_token = ?)
                """,
                ownership_parameters,
            ).fetchone()
            if row is None:
                db.rollback()
                raise QueueError("Cannot record crash for unknown attempt")
            # `attempts` is incremented when the active lease is acquired, so it already
            # includes the crash currently being recorded.
            local_crashes = int(row["attempts"])
            if local_crashes >= MAX_LOCAL_CRASHES:
                db.execute(
                    """
                    INSERT OR IGNORE INTO dlq (
                        build_job_id, attempt_id, payload_json, error, attempts, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["build_job_id"],
                        row["attempt_id"],
                        row["payload_json"],
                        error,
                        local_crashes,
                        now,
                    ),
                )
                db.execute(
                    """
                    UPDATE jobs
                    SET state = 'FAILED',
                        error = ?,
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        cancel_requested = 0,
                        updated_at = ?
                    WHERE attempt_id = ?
                      AND state IN ('LEASED', 'RUNNING')
                      AND (? IS NULL OR lease_owner = ?)
                      AND (? IS NULL OR lease_token = ?)
                    """,
                    (error, now, *ownership_parameters),
                )
            else:
                db.execute(
                    """
                    UPDATE jobs
                    SET state = 'QUEUED',
                        error = ?,
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        cancel_requested = 0,
                        updated_at = ?
                    WHERE attempt_id = ?
                      AND state IN ('LEASED', 'RUNNING')
                      AND (? IS NULL OR lease_owner = ?)
                      AND (? IS NULL OR lease_token = ?)
                    """,
                    (error, now, *ownership_parameters),
                )
            db.commit()
            refreshed = db.execute(
                "SELECT * FROM jobs WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if refreshed is None:
                raise QueueError("Crashed job could not be reloaded")
            return self._with_transient_payload(_job_from_row(refreshed))

    def dlq_entries(self) -> tuple[DeadLetterRecord, ...]:
        """Return all dead-lettered attempts."""

        with self._connect() as db:
            rows = db.execute("SELECT * FROM dlq ORDER BY created_at, attempt_id").fetchall()
        return tuple(_dlq_from_row(row) for row in rows)

    def queue_depth(self) -> int:
        """Return the number of attempts waiting for a worker."""

        with self._connect() as db:
            row = db.execute("SELECT COUNT(*) AS count FROM jobs WHERE state = 'QUEUED'").fetchone()
        return int(row["count"])

    def close(self) -> None:
        """Close the shared SQLite connection."""

        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def __enter__(self) -> SQLiteQueueStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def prune_terminal(self, *, retention_days: int) -> int:
        """Remove terminal attempts older than the configured retention window."""

        cutoff = _format_datetime(datetime.now(UTC) - timedelta(days=retention_days))
        with self._connect() as db:
            cursor = db.execute(
                """
                DELETE FROM jobs
                WHERE state IN ('SUCCEEDED', 'FAILED', 'CANCELLED', 'TIMED_OUT')
                  AND updated_at < ?
                """,
                (cutoff,),
            )
        return cursor.rowcount

    def _with_transient_payload(self, job: JobRecord) -> JobRecord:
        payload = self._transient_payloads.get(job.attempt_id)
        if payload is None:
            return job
        return JobRecord(
            build_job_id=job.build_job_id,
            attempt_id=job.attempt_id,
            payload=dict(payload),
            state=job.state,
            attempts=job.attempts,
            sequence_cursor=job.sequence_cursor,
            lease_owner=job.lease_owner,
            lease_expires_at=job.lease_expires_at,
            error=job.error,
            created_at=job.created_at,
            updated_at=job.updated_at,
            lease_token=job.lease_token,
            cancel_requested=job.cancel_requested,
        )

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._db is None:
                self._db = _open_connection(self.path)
            try:
                yield self._db
            except Exception:
                if self._db.in_transaction:
                    self._db.rollback()
                raise


class SQLiteEventOutbox:
    """SQLite-backed outbound event spool used for reconnect replay."""

    def __init__(
        self,
        store: SQLiteQueueStore,
        *,
        max_bytes: int = 268_435_456,
        retention_days: int = 7,
    ) -> None:
        self.store = store
        self._lock = asyncio.Lock()
        self._db = _open_connection(store.path)
        self.max_bytes = max_bytes
        self.retention_days = retention_days
        self._closed = False

    async def append(self, envelope: Envelope) -> None:
        """Append one outbound attempt event to the SQLite outbox."""

        if envelope.attempt_id is None or envelope.seq is None:
            raise ProtocolError("Outbox events require attempt_id and seq")
        async with self._lock:
            await asyncio.to_thread(self._append_sync, envelope)

    async def ack_through(self, attempt_id: str, last_seq: int) -> None:
        """Delete replay rows acknowledged by the backend."""

        async with self._lock:
            await asyncio.to_thread(self._ack_through_sync, attempt_id, last_seq)

    async def replay_after(self, cursors: Mapping[str, int]) -> list[Envelope]:
        """Return events whose per-attempt seq is greater than the backend cursor."""

        async with self._lock:
            rows = await asyncio.to_thread(self._replay_rows_sync, cursors)
        events: list[Envelope] = []
        for row in rows:
            raw = json.loads(str(row["envelope_json"]))
            # v1 rows predate the required engine_id. They are replayed only
            # during the local migration window and never emitted by v2.
            raw.setdefault("engine_id", ZERO_ENGINE_ID)
            try:
                envelope = decode_frame(
                    json.dumps(raw, separators=(",", ":")),
                    allowed_types=OUTBOUND_MESSAGE_TYPES,
                )
            except ProtocolError:
                # A pre-v2 row cannot be safely replayed across the clean
                # protocol break. It is intentionally ignored and will be
                # removed by retention pruning.
                continue
            if envelope.attempt_id is None or envelope.seq is None:
                continue
            if envelope.seq > cursors.get(envelope.attempt_id, 0):
                events.append(envelope)
        return events

    async def next_seq(self, attempt_id: str) -> int:
        """Return the next outbound seq for an attempt."""

        async with self._lock:
            row = await asyncio.to_thread(self._next_seq_row_sync, attempt_id)
        return int(row["next_seq"])

    async def prune(self) -> int:
        """Apply age and size limits to terminal event history."""

        async with self._lock:
            return await asyncio.to_thread(self._prune_sync)

    def _append_sync(self, envelope: Envelope) -> None:
        existing = self._db.execute(
            "SELECT envelope_json FROM events WHERE attempt_id = ? AND seq = ?",
            (envelope.attempt_id, envelope.seq),
        ).fetchone()
        if existing is not None:
            if str(existing["envelope_json"]) != envelope.to_json():
                raise QueueError("Conflicting event reused an attempt sequence")
            return
        self._db.execute(
            """
            INSERT INTO events (
                id, attempt_id, seq, type, envelope_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                envelope.id,
                envelope.attempt_id,
                envelope.seq,
                envelope.type,
                envelope.to_json(),
                _utcnow(),
            ),
        )
        self._db.execute(
            """
            UPDATE jobs
            SET sequence_cursor = MAX(sequence_cursor, ?), updated_at = ?
            WHERE attempt_id = ?
            """,
            (envelope.seq, _utcnow(), envelope.attempt_id),
        )
        self._prune_sync()
        self._db.commit()

    def _replay_rows_sync(self, cursors: Mapping[str, int]) -> list[sqlite3.Row]:
        rows = self._db.execute(
            """
            SELECT envelope_json FROM events
            ORDER BY created_at, attempt_id, seq
            """
        ).fetchall()
        return [
            row
            for row in rows
            if int(json.loads(str(row["envelope_json"])).get("seq", 0))
            > cursors.get(str(json.loads(str(row["envelope_json"])).get("attempt_id", "")), 0)
        ]

    def _ack_through_sync(self, attempt_id: str, last_seq: int) -> None:
        self._db.execute(
            "DELETE FROM events WHERE attempt_id = ? AND seq <= ?",
            (attempt_id, last_seq),
        )
        self._db.commit()

    def _prune_sync(self) -> int:
        cutoff = _format_datetime(datetime.now(UTC) - timedelta(days=self.retention_days))
        cursor = self._db.execute(
            """
            DELETE FROM events
            WHERE created_at < ?
              AND attempt_id IN (
                SELECT attempt_id FROM jobs
                WHERE state IN ('SUCCEEDED', 'FAILED', 'CANCELLED', 'TIMED_OUT')
              )
            """,
            (cutoff,),
        )
        removed = cursor.rowcount
        while self._event_bytes() > self.max_bytes:
            oldest = self._db.execute(
                """
                SELECT id FROM events
                WHERE type = 'attempt.log'
                ORDER BY created_at, attempt_id, seq
                LIMIT 1
                """
            ).fetchone()
            if oldest is None:
                oldest = self._db.execute(
                    "SELECT id FROM events ORDER BY created_at, attempt_id, seq LIMIT 1"
                ).fetchone()
            if oldest is None:
                break
            self._db.execute("DELETE FROM events WHERE id = ?", (oldest["id"],))
            removed += 1
        self._db.commit()
        return removed

    def _event_bytes(self) -> int:
        row = self._db.execute(
            "SELECT COALESCE(SUM(length(envelope_json)), 0) AS total FROM events"
        ).fetchone()
        return int(row["total"])

    def _next_seq_row_sync(self, attempt_id: str) -> sqlite3.Row:
        row = self._db.execute(
            "SELECT sequence_cursor + 1 AS next_seq FROM jobs WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise QueueError("Cannot allocate an event sequence for an unknown attempt")
        return cast("sqlite3.Row", row)

    def close(self) -> None:
        """Close the outbox connection explicitly during service shutdown."""

        if not self._closed:
            self._db.commit()
            self._db.close()
            self._closed = True

    def __enter__(self) -> SQLiteEventOutbox:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _create_v1_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            build_job_id TEXT NOT NULL,
            attempt_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK (
                state IN (
                    'QUEUED', 'LEASED', 'RUNNING', 'SUCCEEDED',
                    'FAILED', 'CANCELLED', 'TIMED_OUT'
                )
            ),
            lease_owner TEXT,
            lease_token TEXT,
            lease_expires_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            sequence_cursor INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            error TEXT,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            UNIQUE (build_job_id, attempt_id)
        );

        CREATE INDEX IF NOT EXISTS ix_jobs_state_created_at
            ON jobs (state, created_at);
        CREATE INDEX IF NOT EXISTS ix_jobs_build_job_id
            ON jobs (build_job_id);

        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL,
            seq INTEGER NOT NULL CHECK (seq >= 0),
            type TEXT NOT NULL,
            envelope_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (attempt_id, seq),
            FOREIGN KEY (attempt_id) REFERENCES jobs (attempt_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS ix_events_attempt_seq
            ON events (attempt_id, seq);

        CREATE TABLE IF NOT EXISTS dlq (
            attempt_id TEXT PRIMARY KEY,
            build_job_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            error TEXT NOT NULL,
            attempts INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )


def _migrate_to_v2(db: sqlite3.Connection) -> None:
    """Add v2 lease ownership and cancellation columns to v1 databases."""

    columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(jobs)").fetchall()}
    if "lease_token" not in columns:
        db.execute("ALTER TABLE jobs ADD COLUMN lease_token TEXT")
    if "cancel_requested" not in columns:
        db.execute("ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0")


_LEGAL_PREVIOUS_STATES: dict[str, tuple[str, ...]] = {
    "QUEUED": ("LEASED", "RUNNING"),
    "LEASED": ("QUEUED",),
    "RUNNING": ("LEASED",),
    "SUCCEEDED": ("RUNNING",),
    "FAILED": ("QUEUED", "LEASED", "RUNNING"),
    "CANCELLED": ("QUEUED", "LEASED", "RUNNING"),
    "TIMED_OUT": ("QUEUED", "LEASED", "RUNNING"),
}


def _durable_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Strip expiring URLs and secret values before writing an assignment to disk."""

    return {
        str(key): value
        for key, value in payload.items()
        if key not in {"secrets", "source_download_url", "secret_env"}
    }


def _job_row(
    db: sqlite3.Connection,
    build_job_id: str,
    attempt_id: str,
) -> sqlite3.Row | None:
    return cast(
        "sqlite3.Row | None",
        db.execute(
            "SELECT * FROM jobs WHERE build_job_id = ? AND attempt_id = ?",
            (build_job_id, attempt_id),
        ).fetchone(),
    )


def _job_from_row(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        build_job_id=str(row["build_job_id"]),
        attempt_id=str(row["attempt_id"]),
        payload=json.loads(str(row["payload_json"])),
        state=str(row["state"]),
        attempts=int(row["attempts"]),
        sequence_cursor=int(row["sequence_cursor"]),
        lease_owner=str(row["lease_owner"]) if row["lease_owner"] is not None else None,
        lease_expires_at=(
            str(row["lease_expires_at"]) if row["lease_expires_at"] is not None else None
        ),
        error=str(row["error"]) if row["error"] is not None else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        lease_token=str(row["lease_token"]) if row["lease_token"] is not None else None,
        cancel_requested=bool(row["cancel_requested"]),
    )


def _dlq_from_row(row: sqlite3.Row) -> DeadLetterRecord:
    return DeadLetterRecord(
        build_job_id=str(row["build_job_id"]),
        attempt_id=str(row["attempt_id"]),
        payload=json.loads(str(row["payload_json"])),
        error=str(row["error"]),
        attempts=int(row["attempts"]),
        created_at=str(row["created_at"]),
    )


def _required_payload_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise QueueError(f"payload {key} is required")
    return value


def _required_payload_uuid(payload: dict[str, Any], key: str) -> str:
    value = _required_payload_str(payload, key)
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise QueueError(f"payload {key} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise QueueError(f"payload {key} must be a canonical UUID")
    return value


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _utcnow() -> str:
    return _format_datetime(datetime.now(UTC))


def _format_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _open_connection(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db

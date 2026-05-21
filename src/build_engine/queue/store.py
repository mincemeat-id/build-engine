"""Durable SQLite queue and event outbox for build attempts."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from build_engine.agent.protocol import (
    OUTBOUND_MESSAGE_TYPES,
    Envelope,
    ProtocolError,
    decode_frame,
)

SCHEMA_VERSION = 1
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

    def initialize(self) -> None:
        """Create or migrate the SQLite schema."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version < 1:
                _create_v1_schema(db)
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version > SCHEMA_VERSION:
                raise QueueError(
                    f"Queue schema version {version} is newer than supported {SCHEMA_VERSION}"
                )

    def enqueue(self, payload: dict[str, Any]) -> EnqueueResult:
        """Idempotently enqueue one attempt by `(build_job_id, attempt_id)`."""

        build_job_id = _required_payload_str(payload, "build_job_id")
        attempt_id = _required_payload_str(payload, "attempt_id")
        now = _utcnow()
        payload_json = _json_dumps(payload)
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
            return EnqueueResult(job=_job_from_row(row), inserted=inserted)

    def get_job(self, build_job_id: str, attempt_id: str) -> JobRecord | None:
        """Return one queued job, if present."""

        with self._connect() as db:
            row = _job_row(db, build_job_id, attempt_id)
        return _job_from_row(row) if row is not None else None

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
        return tuple(_job_from_row(row) for row in rows)

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
            row = db.execute(
                """
                SELECT * FROM jobs
                WHERE state = 'QUEUED'
                   OR (state = 'LEASED' AND lease_expires_at <= ?)
                ORDER BY created_at, attempt_id
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                db.commit()
                return None
            db.execute(
                """
                UPDATE jobs
                SET state = 'LEASED',
                    lease_owner = ?,
                    lease_expires_at = ?,
                    attempts = attempts + 1,
                    updated_at = ?
                WHERE attempt_id = ?
                """,
                (lease_owner, expires_at, now, row["attempt_id"]),
            )
            db.commit()
            refreshed = _job_row(db, row["build_job_id"], row["attempt_id"])
            if refreshed is None:
                raise QueueError("Leased job could not be reloaded")
            return _job_from_row(refreshed)

    def refresh_lease(
        self,
        *,
        attempt_id: str,
        lease_owner: str,
        visibility_timeout_seconds: int,
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
                WHERE attempt_id = ? AND lease_owner = ? AND state IN ('LEASED', 'RUNNING')
                """,
                (expires_at, now, attempt_id, lease_owner),
            )
            if cursor.rowcount != 1:
                raise QueueError("Cannot refresh lease for unknown or unowned attempt")
            row = db.execute("SELECT * FROM jobs WHERE attempt_id = ?", (attempt_id,)).fetchone()
            if row is None:
                raise QueueError("Refreshed job could not be reloaded")
            return _job_from_row(row)

    def transition(
        self,
        *,
        attempt_id: str,
        state: str,
        error: str | None = None,
    ) -> JobRecord:
        """Move an attempt to a new queue state."""

        if state not in {"QUEUED", "LEASED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"}:
            raise QueueError(f"Invalid queue state: {state}")
        now = _utcnow()
        terminal = state in {"SUCCEEDED", "FAILED", "CANCELLED"}
        with self._connect() as db:
            cursor = db.execute(
                """
                UPDATE jobs
                SET state = ?,
                    error = ?,
                    lease_owner = CASE WHEN ? THEN NULL ELSE lease_owner END,
                    lease_expires_at = CASE WHEN ? THEN NULL ELSE lease_expires_at END,
                    updated_at = ?
                WHERE attempt_id = ?
                """,
                (state, error, terminal, terminal, now, attempt_id),
            )
            if cursor.rowcount != 1:
                raise QueueError("Cannot transition unknown attempt")
            row = db.execute("SELECT * FROM jobs WHERE attempt_id = ?", (attempt_id,)).fetchone()
            if row is None:
                raise QueueError("Transitioned job could not be reloaded")
            return _job_from_row(row)

    def record_executor_crash(self, *, attempt_id: str, error: str) -> JobRecord:
        """Requeue or dead-letter an attempt after a local executor crash."""

        now = _utcnow()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE attempt_id = ?", (attempt_id,)).fetchone()
            if row is None:
                db.rollback()
                raise QueueError("Cannot record crash for unknown attempt")
            attempts = int(row["attempts"])
            if attempts >= MAX_LOCAL_CRASHES:
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
                        attempts,
                        now,
                    ),
                )
                db.execute(
                    """
                    UPDATE jobs
                    SET state = 'FAILED',
                        error = ?,
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        updated_at = ?
                    WHERE attempt_id = ?
                    """,
                    (error, now, attempt_id),
                )
            else:
                db.execute(
                    """
                    UPDATE jobs
                    SET state = 'QUEUED',
                        error = ?,
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        updated_at = ?
                    WHERE attempt_id = ?
                    """,
                    (error, now, attempt_id),
                )
            db.commit()
            refreshed = db.execute(
                "SELECT * FROM jobs WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if refreshed is None:
                raise QueueError("Crashed job could not be reloaded")
            return _job_from_row(refreshed)

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

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db


class SQLiteEventOutbox:
    """SQLite-backed outbound event spool used for reconnect replay."""

    def __init__(self, store: SQLiteQueueStore) -> None:
        self.store = store
        self._lock = asyncio.Lock()

    async def append(self, envelope: Envelope) -> None:
        """Append one outbound attempt event to the SQLite outbox."""

        if envelope.attempt_id is None or envelope.seq is None:
            raise ProtocolError("Outbox events require attempt_id and seq")
        async with self._lock:
            self.store.initialize()
            with self.store._connect() as db:
                db.execute(
                    """
                    INSERT OR IGNORE INTO events (
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
                db.execute(
                    """
                    UPDATE jobs
                    SET sequence_cursor = MAX(sequence_cursor, ?), updated_at = ?
                    WHERE attempt_id = ?
                    """,
                    (envelope.seq, _utcnow(), envelope.attempt_id),
                )

    async def replay_after(self, cursors: Mapping[str, int]) -> list[Envelope]:
        """Return events whose per-attempt seq is greater than the backend cursor."""

        async with self._lock:
            self.store.initialize()
            with self.store._connect() as db:
                rows = db.execute(
                    """
                    SELECT envelope_json FROM events
                    ORDER BY created_at, attempt_id, seq
                    """
                ).fetchall()
        events: list[Envelope] = []
        for row in rows:
            envelope = decode_frame(row["envelope_json"], allowed_types=OUTBOUND_MESSAGE_TYPES)
            if envelope.attempt_id is None or envelope.seq is None:
                continue
            if envelope.seq > cursors.get(envelope.attempt_id, 0):
                events.append(envelope)
        return events

    async def next_seq(self, attempt_id: str) -> int:
        """Return the next outbound seq for an attempt."""

        async with self._lock:
            self.store.initialize()
            with self.store._connect() as db:
                row = db.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM events WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchone()
        return int(row["next_seq"])


def _create_v1_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            build_job_id TEXT NOT NULL,
            attempt_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK (
                state IN ('QUEUED', 'LEASED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')
            ),
            lease_owner TEXT,
            lease_expires_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            sequence_cursor INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            error TEXT,
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


def _job_row(
    db: sqlite3.Connection,
    build_job_id: str,
    attempt_id: str,
) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM jobs WHERE build_job_id = ? AND attempt_id = ?",
        (build_job_id, attempt_id),
    ).fetchone()


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


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _utcnow() -> str:
    return _format_datetime(datetime.now(UTC))


def _format_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

"""Strict JSON envelope helpers for the build-engine agent WSS protocol v2."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

PROTOCOL_VERSION = 2
MAX_FRAME_BYTES = 1_048_576
MAX_LOG_DATA_BYTES = 65_536
MAX_TIMESTAMP_SKEW = timedelta(minutes=5)
ZERO_ENGINE_ID = "00000000-0000-0000-0000-000000000000"

INBOUND_MESSAGE_TYPES = frozenset(
    {
        "welcome",
        "job.assign",
        "job.cancel",
        "cache.reset",
        "drain",
        "ping",
        "event.ack",
    },
)
OUTBOUND_MESSAGE_TYPES = frozenset(
    {
        "hello",
        "attempt.status",
        "attempt.log",
        "artifact.ready",
        "error",
        "heartbeat",
        "pong",
    },
)
ALL_MESSAGE_TYPES = INBOUND_MESSAGE_TYPES | OUTBOUND_MESSAGE_TYPES


class ProtocolError(ValueError):
    """Raised when an inbound or outbound frame violates the v2 envelope."""


@dataclass(frozen=True, slots=True)
class Envelope:
    """A validated protocol envelope."""

    v: int
    id: str
    type: str
    ts: str
    payload: dict[str, Any]
    engine_id: str = ZERO_ENGINE_ID
    build_job_id: str | None = None
    attempt_id: str | None = None
    seq: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON object form used on the wire and in the event spool."""

        data: dict[str, Any] = {
            "v": self.v,
            "id": self.id,
            "type": self.type,
            "ts": self.ts,
            "engine_id": self.engine_id,
            "payload": self.payload,
        }
        if self.build_job_id is not None:
            data["build_job_id"] = self.build_job_id
        if self.attempt_id is not None:
            data["attempt_id"] = self.attempt_id
        if self.seq is not None:
            data["seq"] = self.seq
        return data

    def to_json(self) -> str:
        """Encode the envelope as one compact JSON websocket frame."""

        encoded = json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)
        if len(encoded.encode("utf-8")) > MAX_FRAME_BYTES:
            raise ProtocolError("Frame exceeds 1 MiB")
        return encoded


def new_envelope(
    message_type: str,
    payload: dict[str, Any] | None = None,
    *,
    engine_id: str = ZERO_ENGINE_ID,
    build_job_id: str | None = None,
    attempt_id: str | None = None,
    seq: int | None = None,
    message_id: str | None = None,
    timestamp: datetime | None = None,
) -> Envelope:
    """Create and validate a v2 protocol envelope.

    The all-zero engine UUID is retained only as a convenience for isolated
    unit tests. The uplink always supplies the registered engine UUID.
    """

    envelope = Envelope(
        v=PROTOCOL_VERSION,
        id=message_id or str(uuid.uuid4()),
        type=message_type,
        ts=_format_timestamp(timestamp or datetime.now(UTC)),
        payload=payload or {},
        engine_id=engine_id,
        build_job_id=build_job_id,
        attempt_id=attempt_id,
        seq=seq,
    )
    validate_envelope(envelope.to_dict())
    return envelope


def decode_frame(
    frame: str | bytes,
    *,
    allowed_types: frozenset[str] | None = None,
) -> Envelope:
    """Decode and validate one JSON websocket frame."""

    if isinstance(frame, bytes):
        if len(frame) > MAX_FRAME_BYTES:
            raise ProtocolError("Frame exceeds 1 MiB")
        try:
            text = frame.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("Frame is not valid UTF-8") from exc
    else:
        if len(frame.encode("utf-8")) > MAX_FRAME_BYTES:
            raise ProtocolError("Frame exceeds 1 MiB")
        text = frame
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError("Frame is not valid JSON") from exc
    return envelope_from_mapping(raw, allowed_types=allowed_types)


def envelope_from_mapping(
    raw: object,
    *,
    allowed_types: frozenset[str] | None = None,
) -> Envelope:
    """Validate a decoded mapping and return an :class:`Envelope`."""

    data = validate_envelope(raw, allowed_types=allowed_types)
    return Envelope(
        v=data["v"],
        id=data["id"],
        type=data["type"],
        ts=data["ts"],
        payload=data["payload"],
        engine_id=data["engine_id"],
        build_job_id=data.get("build_job_id"),
        attempt_id=data.get("attempt_id"),
        seq=data.get("seq"),
    )


def validate_envelope(
    raw: object,
    *,
    allowed_types: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Validate the protocol shape without requiring a JSON Schema dependency."""

    if not isinstance(raw, dict):
        raise ProtocolError("Envelope must be a JSON object")
    data = dict(raw)
    unknown = set(data) - {
        "v",
        "id",
        "type",
        "ts",
        "engine_id",
        "build_job_id",
        "attempt_id",
        "seq",
        "payload",
    }
    if unknown:
        names = ", ".join(sorted(map(str, unknown)))
        raise ProtocolError(f"Envelope contains unknown fields: {names}")
    required = ("v", "id", "type", "ts", "engine_id", "payload")
    missing = [key for key in required if key not in data]
    if missing:
        raise ProtocolError(f"Envelope missing required fields: {', '.join(missing)}")
    if data["v"] != PROTOCOL_VERSION:
        raise ProtocolError(f"Unsupported protocol version: {data['v']}")
    _require_uuid(data["id"], "Envelope id")
    if not isinstance(data["type"], str):
        raise ProtocolError("Envelope type must be a string")
    allowed = allowed_types or ALL_MESSAGE_TYPES
    if data["type"] not in allowed:
        raise ProtocolError(f"Unknown message type: {data['type']}")
    if not isinstance(data["ts"], str):
        raise ProtocolError("Envelope ts must be a string")
    timestamp = _parse_timestamp(data["ts"])
    now = datetime.now(UTC)
    if timestamp < now - MAX_TIMESTAMP_SKEW or timestamp > now + MAX_TIMESTAMP_SKEW:
        raise ProtocolError("Envelope ts is outside the accepted clock-skew window")
    _require_uuid(data["engine_id"], "Envelope engine_id")
    if not isinstance(data["payload"], dict):
        raise ProtocolError("Envelope payload must be an object")
    for key in ("build_job_id", "attempt_id"):
        if key in data:
            _require_uuid(data[key], f"Envelope {key}")
    if "seq" in data:
        if not isinstance(data["seq"], int) or isinstance(data["seq"], bool) or data["seq"] < 0:
            raise ProtocolError("Envelope seq must be a non-negative integer")
        if data.get("attempt_id") is None:
            raise ProtocolError("Envelope seq requires attempt_id")
    if data["type"] == "attempt.log":
        value = data["payload"].get("data")
        if isinstance(value, str) and len(value.encode("utf-8")) > MAX_LOG_DATA_BYTES:
            raise ProtocolError("Log payload exceeds 64 KiB")
    return data


def attempt_fields(envelope: Envelope) -> tuple[str, str]:
    """Return build and attempt IDs from top-level fields or payload mirrors."""

    build_job_id = envelope.build_job_id or _string_payload(envelope.payload, "build_job_id")
    attempt_id = envelope.attempt_id or _string_payload(envelope.payload, "attempt_id")
    if build_job_id is None or attempt_id is None:
        raise ProtocolError("Attempt-scoped message requires build_job_id and attempt_id")
    return build_job_id, attempt_id


def last_sequences(payload: dict[str, Any]) -> dict[str, int]:
    """Extract reconnect replay cursors from a welcome payload."""

    raw = payload.get("last_seq", payload.get("last_sequences", {}))
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ProtocolError("welcome last_seq must be an object")
    result: dict[str, int] = {}
    for attempt_id, seq in raw.items():
        if (
            not isinstance(attempt_id, str)
            or not _is_uuid(attempt_id)
            or not isinstance(seq, int)
            or isinstance(seq, bool)
            or seq < 0
        ):
            raise ProtocolError("welcome last_seq values must map attempt IDs to integers")
        result[attempt_id] = seq
    return result


def _string_payload(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value or not _is_uuid(value):
        raise ProtocolError(f"payload {key} must be a UUID string")
    return value


def _is_uuid(value: str) -> bool:
    try:
        parsed = uuid.UUID(value)
    except ValueError, AttributeError:
        return False
    return str(parsed) == value


def _require_uuid(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise ProtocolError(f"{label} must be a UUID string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ProtocolError(f"{label} must be a UUID string") from exc
    if str(parsed) != value:
        raise ProtocolError(f"{label} must be a canonical UUID string")


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProtocolError("Envelope ts must be an ISO datetime") from exc
    if parsed.tzinfo is None:
        raise ProtocolError("Envelope ts must include timezone information")
    return parsed

"""Protocol v2 envelope boundary tests."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from build_engine.agent.protocol import (
    INBOUND_MESSAGE_TYPES,
    MAX_FRAME_BYTES,
    ProtocolError,
    decode_frame,
    last_sequences,
    new_envelope,
)

BUILD_JOB_ID = "11111111-1111-1111-1111-111111111111"
ATTEMPT_ID = "22222222-2222-2222-2222-222222222222"


def test_new_envelope_round_trips_as_compact_json() -> None:
    envelope = new_envelope(
        "attempt.status",
        {"phase": "PREPARING"},
        build_job_id=BUILD_JOB_ID,
        attempt_id=ATTEMPT_ID,
        seq=1,
    )

    decoded = decode_frame(envelope.to_json())

    assert decoded.v == 2
    assert decoded.type == "attempt.status"
    assert decoded.engine_id == "00000000-0000-0000-0000-000000000000"
    assert decoded.payload == {"phase": "PREPARING"}
    assert decoded.seq == 1


def test_decode_rejects_unknown_inbound_type() -> None:
    frame = new_envelope("attempt.status", {"phase": "PREPARING"}).to_json()

    with pytest.raises(ProtocolError, match="Unknown message type"):
        decode_frame(frame, allowed_types=INBOUND_MESSAGE_TYPES)


def test_v2_rejects_v1_and_legacy_message_names() -> None:
    with pytest.raises(ProtocolError, match="Unknown message type"):
        new_envelope("status", {"phase": "PREPARING"})


def test_log_frame_enforces_64_kib_payload_limit() -> None:
    payload = {"stream": "stdout", "data": "x" * 65_537}

    with pytest.raises(ProtocolError, match="64 KiB"):
        new_envelope("attempt.log", payload)


def test_outbound_frame_encoding_enforces_one_mib_limit() -> None:
    envelope = new_envelope("attempt.status", {"detail": "x" * MAX_FRAME_BYTES})

    with pytest.raises(ProtocolError, match="1 MiB"):
        envelope.to_json()


def test_last_sequences_accepts_canonical_welcome_cursor_map() -> None:
    assert last_sequences({"last_seq": {ATTEMPT_ID: 4}}) == {ATTEMPT_ID: 4}


def test_last_sequences_rejects_invalid_cursor_map() -> None:
    with pytest.raises(ProtocolError, match="last_seq"):
        last_sequences({"last_seq": {"attempt-a": -1}})


def test_uuid_and_timestamp_validation_is_strict() -> None:
    current = datetime.now(UTC)
    envelope = new_envelope("hello", timestamp=current)
    raw = envelope.to_dict()
    raw["id"] = envelope.id.upper()
    with pytest.raises(ProtocolError, match="canonical UUID"):
        decode_frame(json.dumps(raw))

    raw = envelope.to_dict()
    raw["ts"] = (current - timedelta(minutes=6)).isoformat().replace("+00:00", "Z")
    with pytest.raises(ProtocolError, match="clock-skew"):
        decode_frame(json.dumps(raw))

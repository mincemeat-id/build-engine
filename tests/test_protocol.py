"""Agent WSS protocol envelope tests."""

import pytest

from build_engine.agent.protocol import (
    INBOUND_MESSAGE_TYPES,
    ProtocolError,
    decode_frame,
    last_sequences,
    new_envelope,
)


def test_new_envelope_round_trips_as_compact_json() -> None:
    envelope = new_envelope(
        "status",
        {"phase": "PREPARING"},
        build_job_id="11111111-1111-1111-1111-111111111111",
        attempt_id="22222222-2222-2222-2222-222222222222",
        seq=1,
    )

    decoded = decode_frame(envelope.to_json())

    assert decoded.type == "status"
    assert decoded.payload == {"phase": "PREPARING"}
    assert decoded.seq == 1


def test_decode_rejects_unknown_inbound_type() -> None:
    frame = new_envelope("status", {"phase": "PREPARING"}).to_json()

    with pytest.raises(ProtocolError, match="Unknown message type"):
        decode_frame(frame, allowed_types=INBOUND_MESSAGE_TYPES)


def test_log_frame_enforces_64_kib_payload_limit() -> None:
    payload = {"stream": "stdout", "data": "x" * 65_537}

    with pytest.raises(ProtocolError, match="64 KiB"):
        new_envelope("log", payload)


def test_last_sequences_accepts_welcome_cursor_map() -> None:
    assert last_sequences({"last_seq": {"attempt-a": 4}}) == {"attempt-a": 4}


def test_last_sequences_rejects_invalid_cursor_map() -> None:
    with pytest.raises(ProtocolError, match="last_seq"):
        last_sequences({"last_seq": {"attempt-a": -1}})

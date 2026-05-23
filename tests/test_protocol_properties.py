"""Property tests for the v1 websocket envelope boundary."""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from build_engine.agent.protocol import (
    ALL_MESSAGE_TYPES,
    MAX_FRAME_BYTES,
    ProtocolError,
    decode_frame,
    new_envelope,
    validate_envelope,
)

json_scalars = st.one_of(
    st.none(), st.booleans(), st.integers(), st.floats(allow_nan=False), st.text()
)
json_values = st.recursive(
    json_scalars,
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=16), children, max_size=4)
    ),
    max_leaves=12,
)
payloads = st.dictionaries(st.text(min_size=1, max_size=24), json_values, max_size=6)


@settings(max_examples=80)
@given(message_type=st.sampled_from(sorted(ALL_MESSAGE_TYPES)), payload=payloads)
def test_new_envelope_decode_frame_round_trips_json_payloads(
    message_type: str,
    payload: dict[str, Any],
) -> None:
    envelope = new_envelope(message_type, payload)

    decoded = decode_frame(envelope.to_json())

    assert decoded.type == message_type
    assert decoded.payload == payload


@settings(max_examples=80)
@given(raw=st.from_type(object))
def test_validate_envelope_fuzz_never_raises_unexpected_errors(raw: object) -> None:
    try:
        validate_envelope(raw)
    except ProtocolError:
        return


@settings(max_examples=80)
@given(payload=st.one_of(json_values.filter(lambda value: not isinstance(value, dict))))
def test_validate_envelope_rejects_non_object_payloads(payload: object) -> None:
    raw = _valid_raw() | {"payload": payload}

    with pytest.raises(ProtocolError, match="payload must be an object"):
        validate_envelope(raw)


@settings(max_examples=80)
@given(seq=st.one_of(st.integers(max_value=-1), st.text(), st.none(), st.booleans()))
def test_validate_envelope_rejects_invalid_sequence_values(seq: object) -> None:
    raw = _valid_raw() | {"attempt_id": "attempt-a", "seq": seq}

    with pytest.raises(ProtocolError, match="seq must be a non-negative integer"):
        validate_envelope(raw)


def test_validate_envelope_rejects_sequence_without_attempt_id() -> None:
    raw = _valid_raw() | {"seq": 1}

    with pytest.raises(ProtocolError, match="seq requires attempt_id"):
        validate_envelope(raw)


def test_decode_frame_rejects_oversized_binary_frame_before_json_parsing() -> None:
    with pytest.raises(ProtocolError, match="1 MiB"):
        decode_frame(b"{" + (b" " * MAX_FRAME_BYTES) + b"}")


@settings(max_examples=40)
@given(message_type=st.text().filter(lambda value: value not in ALL_MESSAGE_TYPES))
def test_validate_envelope_rejects_unknown_message_types(message_type: str) -> None:
    raw = _valid_raw() | {"type": message_type}

    with pytest.raises(ProtocolError, match="Unknown message type|type must be a string"):
        validate_envelope(raw)


def _valid_raw() -> dict[str, Any]:
    return {
        "v": 1,
        "id": "message-id",
        "type": "status",
        "ts": "2030-01-01T00:00:00Z",
        "payload": {},
    }

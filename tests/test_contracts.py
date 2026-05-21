"""Contract snapshot smoke tests."""

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def test_openapi_snapshot_contains_locked_agent_routes() -> None:
    snapshot = _load_json(ROOT / "contracts" / "openapi" / "build-engine.openapi.json")
    paths = snapshot["paths"]

    assert "/api/v1/build-engines/agent/register" in paths
    assert "/api/v1/build-engines/agent/sessions" in paths
    assert "/api/v1/build-engines/agent/heartbeats" in paths
    assert (
        "/api/v1/build-engines/agent/jobs/{job_id}/attempts/{attempt_id}/artifact-upload-url"
        in paths
    )
    assert "/api/v1/build-engines/agent/health" in paths


def test_openapi_snapshot_contains_locked_component_names() -> None:
    snapshot = _load_json(ROOT / "contracts" / "openapi" / "build-engine.openapi.json")
    schemas = snapshot["components"]["schemas"]

    assert schemas["BuildEngineStatus"]["enum"] == [
        "PENDING",
        "ONLINE",
        "OFFLINE",
        "DISABLED",
        "DRAINING",
        "QUARANTINED",
    ]
    assert schemas["BuildEngineCommandType"]["enum"] == [
        "job.assign",
        "cancel",
        "cache.reset",
        "drain",
    ]


def test_openapi_snapshot_uses_token_only_agent_auth() -> None:
    snapshot = _load_json(ROOT / "contracts" / "openapi" / "build-engine.openapi.json")
    schemas = snapshot["components"]["schemas"]
    register_request = schemas["BuildEngineAgentRegisterRequest"]
    register_response = schemas["BuildEngineAgentRegisterResponse"]
    engine_response = schemas["BuildEngineResponse"]

    assert "cert_pem" not in register_request["properties"]
    assert "backend_cert_fingerprint" not in register_response["properties"]
    assert "fingerprint" not in engine_response["properties"]


def test_protocol_schema_locks_message_types() -> None:
    schema = _load_json(ROOT / "contracts" / "protocol" / "wss-v1.json")
    message_types = set(schema["properties"]["type"]["enum"])

    assert {"job.assign", "cancel", "cache.reset", "drain"} <= message_types
    assert {"hello", "heartbeat", "artifact.ready", "job.ack"} <= message_types


def test_image_manifest_schema_requires_digests() -> None:
    schema = _load_json(ROOT / "contracts" / "image-manifest" / "manifest.schema.json")
    image_entry = schema["properties"]["images"]["additionalProperties"]

    assert "digest" in image_entry["required"]
    assert image_entry["properties"]["digest"]["pattern"].startswith("^sha256:")


def _load_json(path: Path) -> dict[str, Any]:
    with path.open() as handle:
        return json.load(handle)

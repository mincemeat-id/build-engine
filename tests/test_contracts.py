"""Contract snapshot smoke tests."""

import json
from pathlib import Path
from typing import Any

import pytest

from build_engine.config import DEFAULT_IMAGE_MANIFEST_VERSION
from build_engine.detect.framework import FRAMEWORK_PROFILES

ROOT = Path(__file__).resolve().parents[1]

# Sibling repo that ships the GA builder-image manifest. Optional in CI —
# tests that depend on it skip rather than fail when the checkout is absent.
BUILD_ENGINE_IMAGES_MANIFEST = ROOT.parent / "build-engine-images" / "manifest.json"

# HTTP routes the engine actually invokes. Discovered by grepping the engine
# source for `/api/v1/...` and locked here so OpenAPI subset drift is caught.
# WSS routes (`/api/v1/build-engines/agent/ws`) are validated separately.
ENGINE_INVOKED_HTTP_ROUTES = {
    "/api/v1/build-engines/agent/register",
    "/api/v1/build-engines/agent/sessions",
    "/api/v1/build-engines/agent/health",
    "/api/v1/build-engines/agent/metrics",
    ("/api/v1/build-engines/agent/jobs/{job_id}/attempts/{attempt_id}/artifact-upload-url"),
}

# Framework profile IDs that map to a builder-image entry under a different
# name in the build-engine-images manifest. Keeps the cross-repo overlap
# check honest without forcing a rename that has its own follow-up.
FRAMEWORK_ID_TO_MANIFEST_NAME = {
    "next-export": "nextjs-export",
}


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


def test_default_image_manifest_version_matches_shipped_manifest() -> None:
    if not BUILD_ENGINE_IMAGES_MANIFEST.exists():
        pytest.skip(f"missing sibling checkout: {BUILD_ENGINE_IMAGES_MANIFEST}")
    shipped = _load_json(BUILD_ENGINE_IMAGES_MANIFEST).get("version")
    assert shipped == DEFAULT_IMAGE_MANIFEST_VERSION, (
        "EngineConfig.image_manifest_version drifted from the build-engine-images "
        f"manifest: engine={DEFAULT_IMAGE_MANIFEST_VERSION!r}, shipped={shipped!r}"
    )


def test_every_framework_profile_appears_in_image_manifest() -> None:
    if not BUILD_ENGINE_IMAGES_MANIFEST.exists():
        pytest.skip(f"missing sibling checkout: {BUILD_ENGINE_IMAGES_MANIFEST}")
    manifest = _load_json(BUILD_ENGINE_IMAGES_MANIFEST)
    advertised: set[str] = set()
    for entry in manifest.get("images", {}).values():
        advertised.update(entry.get("frameworks", ()))

    missing: list[str] = []
    for profile_id in FRAMEWORK_PROFILES:
        if profile_id == "generic":
            continue
        manifest_name = FRAMEWORK_ID_TO_MANIFEST_NAME.get(profile_id, profile_id)
        if manifest_name not in advertised:
            missing.append(f"{profile_id} (manifest name: {manifest_name})")
    assert not missing, (
        "FRAMEWORK_PROFILES entries are not advertised by any builder image: " + ", ".join(missing)
    )


def test_openapi_subset_covers_every_engine_invoked_http_route() -> None:
    snapshot = _load_json(ROOT / "contracts" / "openapi" / "build-engine.openapi.json")
    paths = set(snapshot["paths"])
    missing = ENGINE_INVOKED_HTTP_ROUTES - paths
    assert not missing, "Engine HTTP routes missing from the contract OpenAPI subset: " + ", ".join(
        sorted(missing)
    )


def _load_json(path: Path) -> dict[str, Any]:
    with path.open() as handle:
        return json.load(handle)

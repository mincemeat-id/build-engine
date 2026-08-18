"""Protocol, OpenAPI, and builder-manifest contract smoke tests."""

import importlib.util
import json
from pathlib import Path
from typing import Any, cast

import pytest

from build_engine.agent.protocol import INBOUND_MESSAGE_TYPES, OUTBOUND_MESSAGE_TYPES
from build_engine.config import DEFAULT_IMAGE_MANIFEST_VERSION
from build_engine.detect.framework import FRAMEWORK_PROFILES, SUPPORTED_FRAMEWORK_IDS

ROOT = Path(__file__).resolve().parents[1]
BUILD_ENGINE_IMAGES_MANIFEST = ROOT / "manifest.json"
SYNC_CONTRACTS_SCRIPT = ROOT / "scripts" / "sync_contracts.py"
EXPECTED_FRAMEWORKS = {
    "astro",
    "vite",
    "eleventy",
    "docusaurus",
    "vitepress",
    "vuepress",
    "gatsby",
    "hugo",
}
ENGINE_INVOKED_HTTP_ROUTES = {
    "/api/v2/build-engines/agent/register",
    "/api/v2/build-engines/agent/sessions",
    "/api/v2/build-engines/agent/health",
    "/api/v2/build-engines/agent/jobs/{job_id}/attempts/{attempt_id}/artifact-upload-url",
    "/api/v2/build-engines/agent/jobs/{job_id}/attempts/{attempt_id}/secrets",
}


def test_openapi_snapshot_contains_only_v2_agent_routes() -> None:
    paths = _load_json(ROOT / "contracts" / "openapi" / "build-engine.openapi.json")["paths"]

    assert set(paths) >= ENGINE_INVOKED_HTTP_ROUTES
    assert not any("/api/v1/build-engines/agent" in path for path in paths)
    assert not any(
        name in paths
        for name in (
            "/api/v2/build-engines/agent/heartbeats",
            "/api/v2/build-engines/agent/ack",
        )
    )


def test_openapi_snapshot_locks_v2_capabilities_and_commands() -> None:
    schemas = _load_json(ROOT / "contracts" / "openapi" / "build-engine.openapi.json")[
        "components"
    ]["schemas"]

    assert schemas["BuildEngineCapabilities"]["properties"]["proto_version"]["const"] == 2
    assert schemas["BuildEngineCommandType"]["enum"] == [
        "job.assign",
        "job.cancel",
        "cache.reset",
        "drain",
        "ping",
    ]


def test_openapi_snapshot_uses_token_only_agent_auth() -> None:
    schemas = _load_json(ROOT / "contracts" / "openapi" / "build-engine.openapi.json")[
        "components"
    ]["schemas"]
    register_request = schemas["BuildEngineAgentRegisterRequest"]
    register_response = schemas["BuildEngineAgentRegisterResponse"]
    engine_response = schemas["BuildEngineResponse"]

    assert "cert_pem" not in register_request["properties"]
    assert "backend_cert_fingerprint" not in register_response["properties"]
    assert "fingerprint" not in engine_response["properties"]


def test_protocol_schema_locks_clean_v2_message_types() -> None:
    schema = _load_json(ROOT / "contracts" / "protocol" / "wss-v2.json")
    message_types = set(schema["properties"]["type"]["enum"])

    assert message_types == INBOUND_MESSAGE_TYPES | OUTBOUND_MESSAGE_TYPES
    assert "job.cancel" in message_types
    assert "attempt.status" in message_types
    assert "job.ack" not in message_types
    assert "status" not in message_types


def test_image_manifest_schema_requires_digest_only_execution() -> None:
    schema = _load_json(ROOT / "contracts" / "image-manifest" / "manifest.schema.json")
    image_entry = schema["properties"]["images"]["additionalProperties"]

    assert "digest" in image_entry["required"]
    assert image_entry["properties"]["digest"]["pattern"] == "^sha256:[0-9a-f]{64}$"


def test_default_image_manifest_version_matches_shipped_manifest() -> None:
    shipped = _load_json(BUILD_ENGINE_IMAGES_MANIFEST).get("version")
    assert shipped == DEFAULT_IMAGE_MANIFEST_VERSION == "1.0.0"


def test_every_certified_profile_appears_in_image_manifest() -> None:
    manifest = _load_json(BUILD_ENGINE_IMAGES_MANIFEST)
    advertised = {
        framework
        for entry in manifest.get("images", {}).values()
        for framework in entry.get("frameworks", ())
    }

    assert set(FRAMEWORK_PROFILES) == SUPPORTED_FRAMEWORK_IDS == EXPECTED_FRAMEWORKS
    assert advertised == EXPECTED_FRAMEWORKS


def test_manifest_fixture_matches_repo_snapshot() -> None:
    fixture = _load_json(ROOT / "tests/fixtures/manifests/build-engine-images-v1.0.0.json")
    assert fixture == _load_json(BUILD_ENGINE_IMAGES_MANIFEST)


def test_manifest_url_guard_rejects_version_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad_manifest = tmp_path / "manifest.json"
    bad_manifest.write_text(json.dumps({"version": "9.9.9"}) + "\n", encoding="utf-8")
    monkeypatch.setenv("BUILD_ENGINE_IMAGES_MANIFEST_URL", str(bad_manifest))

    sync_contracts = _load_sync_contracts_module()
    with pytest.raises(SystemExit, match="image-manifest version drift"):
        sync_contracts._check_image_manifest_version_drift()


def test_contract_sync_keeps_pinned_snapshot_without_adjacent_coreapp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync_contracts = _load_sync_contracts_module()
    pinned_snapshot = tmp_path / "build-engine.openapi.json"
    pinned_snapshot.write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(sync_contracts, "COREAPP_OPENAPI", tmp_path / "missing-openapi.json")
    monkeypatch.setattr(sync_contracts, "TARGET_OPENAPI", pinned_snapshot)

    assert sync_contracts._load_coreapp_openapi_source() is None
    assert pinned_snapshot.read_text(encoding="utf-8") == "{}\n"


def test_openapi_subset_covers_every_engine_invoked_http_route() -> None:
    paths = set(_load_json(ROOT / "contracts/openapi/build-engine.openapi.json")["paths"])
    missing = ENGINE_INVOKED_HTTP_ROUTES - paths
    assert not missing, "Engine HTTP routes missing from the contract: " + ", ".join(
        sorted(missing)
    )


def _load_json(path: Path) -> dict[str, Any]:
    with path.open() as handle:
        return cast("dict[str, Any]", json.load(handle))


def _load_sync_contracts_module() -> Any:
    spec = importlib.util.spec_from_file_location("sync_contracts", SYNC_CONTRACTS_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

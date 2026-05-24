"""Contract snapshot smoke tests."""

import importlib.util
import json
from pathlib import Path
from typing import Any, cast

import pytest

from build_engine.config import DEFAULT_IMAGE_MANIFEST_VERSION
from build_engine.detect.framework import FRAMEWORK_PROFILES

ROOT = Path(__file__).resolve().parents[1]

# Published build-engine-images manifest snapshot. The v1.0.0 release asset was
# compared against this file on 2026-05-24 and matched byte-for-byte.
BUILD_ENGINE_IMAGES_MANIFEST = ROOT / "manifest.json"
SYNC_CONTRACTS_SCRIPT = ROOT / "scripts" / "sync_contracts.py"

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

EXPECTED_FINAL_IMAGE_FRAMEWORKS = {
    "astro",
    "vite",
    "eleventy",
    "docusaurus",
    "vitepress",
    "vuepress",
    "gatsby",
    "hugo",
    "zola",
    "nextjs-export",
    "nuxt-generate",
    "sveltekit-static",
    "angular-static",
    "remix-spa",
    "generic",
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
    shipped = _load_json(BUILD_ENGINE_IMAGES_MANIFEST).get("version")
    assert shipped == DEFAULT_IMAGE_MANIFEST_VERSION, (
        "EngineConfig.image_manifest_version drifted from the build-engine-images "
        f"manifest: engine={DEFAULT_IMAGE_MANIFEST_VERSION!r}, shipped={shipped!r}"
    )


def test_every_framework_profile_appears_in_image_manifest() -> None:
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


def test_final_image_manifest_advertises_public_release_framework_matrix() -> None:
    manifest = _load_json(BUILD_ENGINE_IMAGES_MANIFEST)
    advertised: set[str] = set()
    for entry in manifest.get("images", {}).values():
        advertised.update(entry.get("frameworks", ()))

    assert advertised >= EXPECTED_FINAL_IMAGE_FRAMEWORKS


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
    snapshot = _load_json(ROOT / "contracts" / "openapi" / "build-engine.openapi.json")
    paths = set(snapshot["paths"])
    missing = ENGINE_INVOKED_HTTP_ROUTES - paths
    assert not missing, "Engine HTTP routes missing from the contract OpenAPI subset: " + ", ".join(
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

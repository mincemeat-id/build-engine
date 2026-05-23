"""Synchronize Stage 0 contract snapshots from the adjacent coreapp checkout."""

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
COREAPP = ROOT.parent / "coreapp"
COREAPP_OPENAPI = COREAPP / "frontend" / "openapi.json"
TARGET_OPENAPI = ROOT / "contracts" / "openapi" / "build-engine.openapi.json"
BUILD_ENGINE_IMAGES_MANIFEST = ROOT.parent / "build-engine-images" / "manifest.json"

# Schema names imported into the engine OpenAPI subset. Drop entries the
# engine no longer references (e.g. cache-reset response — coreapp performs
# the broadcast over WSS; the engine never POSTs to that route).
BUILD_ENGINE_SCHEMA_NAMES = {
    "BuildArtifactUploadUrlRequest",
    "BuildArtifactUploadUrlResponse",
    "BuildAttemptAckRequest",
    "BuildEngineAgentHealthResponse",
    "BuildEngineAgentRegisterRequest",
    "BuildEngineAgentRegisterResponse",
    "BuildEngineAgentSessionRequest",
    "BuildEngineAgentSessionResponse",
    "BuildEngineCacheResetRequest",
    "BuildEngineCapabilities",
    "BuildEngineCommandEnvelope",
    "BuildEngineCommandType",
    "BuildEngineHeartbeatRequest",
    "BuildEngineListResponse",
    "BuildEngineMetricRollupRequest",
    "BuildEngineRegistrationTokenCreate",
    "BuildEngineRegistrationTokenResponse",
    "BuildEngineResponse",
    "BuildEngineStatus",
    "BuildEngineUpdateRequest",
    "BuildErrorClass",
    "BuildJobAttemptResponse",
    "BuildJobListResponse",
    "BuildJobResponse",
    "BuildJobStatus",
    "BuildPackageManager",
    "BuildSourceArchiveFormat",
    "HTTPValidationError",
    "SiteBuildCacheResetResponse",
    "SiteBuildConfigResponse",
    "SiteBuildConfigUpdateRequest",
    "SiteBuildSecretItem",
    "SiteBuildSecretListResponse",
    "SiteBuildSecretUpsertRequest",
    "ValidationError",
}

BUILD_ENGINE_PATH_MARKERS = (
    "/build-engines",
    "/build-jobs",
    "/build-cache",
    "/build-config",
    "/build-secrets",
)


def main() -> int:
    """Extract the build-engine-facing OpenAPI subset."""

    if not COREAPP_OPENAPI.exists():
        raise SystemExit(f"missing coreapp OpenAPI source: {COREAPP_OPENAPI}")

    source = _load_json(COREAPP_OPENAPI)
    schemas = source["components"]["schemas"]
    paths = {
        path: value
        for path, value in source["paths"].items()
        if any(marker in path for marker in BUILD_ENGINE_PATH_MARKERS)
    }
    components = {
        name: schemas[name] for name in sorted(BUILD_ENGINE_SCHEMA_NAMES) if name in schemas
    }

    snapshot = {
        "openapi": source["openapi"],
        "info": {
            "title": "Mincemeat build-engine contract subset",
            "version": source["info"]["version"],
            "x-source": str(COREAPP_OPENAPI.relative_to(ROOT.parent)),
        },
        "paths": dict(sorted(paths.items())),
        "components": {"schemas": components},
    }
    TARGET_OPENAPI.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")

    _check_image_manifest_version_drift()
    return 0


def _check_image_manifest_version_drift() -> None:
    """Fail if EngineConfig.image_manifest_version drifts from the shipped manifest.

    The adjacent ``build-engine-images`` checkout is the source of truth for
    the manifest version. If the checkout is missing (e.g. CI), skip with a
    notice — the assertion still runs locally before a release.
    """

    if not BUILD_ENGINE_IMAGES_MANIFEST.exists():
        print(
            f"sync_contracts: skipped manifest-version drift check "
            f"(missing {BUILD_ENGINE_IMAGES_MANIFEST})",
            file=sys.stderr,
        )
        return

    sys.path.insert(0, str(ROOT / "src"))
    try:
        from build_engine.config import DEFAULT_IMAGE_MANIFEST_VERSION
    finally:
        sys.path.pop(0)

    shipped = _load_json(BUILD_ENGINE_IMAGES_MANIFEST).get("version")
    if shipped != DEFAULT_IMAGE_MANIFEST_VERSION:
        raise SystemExit(
            f"image-manifest version drift: EngineConfig default is "
            f"{DEFAULT_IMAGE_MANIFEST_VERSION!r} but "
            f"{BUILD_ENGINE_IMAGES_MANIFEST.name} ships {shipped!r}. "
            f"Bump build_engine.config.DEFAULT_IMAGE_MANIFEST_VERSION "
            f"in lockstep with the images manifest."
        )


def _load_json(path: Path) -> dict[str, Any]:
    with path.open() as handle:
        return json.load(handle)


if __name__ == "__main__":
    raise SystemExit(main())

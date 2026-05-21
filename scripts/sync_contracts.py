"""Synchronize Stage 0 contract snapshots from the adjacent coreapp checkout."""

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
COREAPP = ROOT.parent / "coreapp"
COREAPP_OPENAPI = COREAPP / "frontend" / "openapi.json"
TARGET_OPENAPI = ROOT / "contracts" / "openapi" / "build-engine.openapi.json"

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
    "BuildEngineCacheResetResponse",
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
    return 0


def _load_json(path: Path) -> dict[str, Any]:
    with path.open() as handle:
        return json.load(handle)


if __name__ == "__main__":
    raise SystemExit(main())

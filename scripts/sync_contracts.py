"""Synchronize Stage 0 contract snapshots from the adjacent coreapp checkout."""

import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
COREAPP = ROOT.parent / "coreapp"
COREAPP_OPENAPI = COREAPP / "frontend" / "openapi.json"
TARGET_OPENAPI = ROOT / "contracts" / "openapi" / "build-engine.openapi.json"
BUILD_ENGINE_IMAGES_MANIFEST = ROOT / "manifest.json"
BUILD_ENGINE_IMAGES_MANIFEST_URL_ENV = "BUILD_ENGINE_IMAGES_MANIFEST_URL"

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

    source = _load_coreapp_openapi_source()
    if source is not None:
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


def _load_coreapp_openapi_source() -> dict[str, Any] | None:
    """Load adjacent coreapp OpenAPI, or keep the pinned snapshot in standalone checkouts."""

    if COREAPP_OPENAPI.exists():
        return _load_json(COREAPP_OPENAPI)
    if TARGET_OPENAPI.exists():
        print(
            f"coreapp OpenAPI source not found at {COREAPP_OPENAPI}; "
            f"leaving pinned snapshot unchanged: {TARGET_OPENAPI}",
            file=sys.stderr,
        )
        return None
    raise SystemExit(
        f"missing coreapp OpenAPI source: {COREAPP_OPENAPI}; "
        f"pinned snapshot is also missing: {TARGET_OPENAPI}"
    )


def _check_image_manifest_version_drift() -> None:
    """Fail if EngineConfig.image_manifest_version drifts from the final manifest."""

    sys.path.insert(0, str(ROOT / "src"))
    try:
        from build_engine.config import DEFAULT_IMAGE_MANIFEST_VERSION
    finally:
        sys.path.pop(0)

    manifest, manifest_source = _load_image_manifest()
    shipped = manifest.get("version")
    if shipped != DEFAULT_IMAGE_MANIFEST_VERSION:
        raise SystemExit(
            f"image-manifest version drift: EngineConfig default is "
            f"{DEFAULT_IMAGE_MANIFEST_VERSION!r} but "
            f"{manifest_source} ships {shipped!r}. "
            f"Bump build_engine.config.DEFAULT_IMAGE_MANIFEST_VERSION "
            f"in lockstep with the images manifest."
        )


def _load_image_manifest() -> tuple[dict[str, Any], str]:
    location = os.environ.get(BUILD_ENGINE_IMAGES_MANIFEST_URL_ENV)
    if location:
        return _load_json_location(location), location
    return _load_json(BUILD_ENGINE_IMAGES_MANIFEST), str(BUILD_ENGINE_IMAGES_MANIFEST)


def _load_json_location(location: str) -> dict[str, Any]:
    parsed = urlparse(location)
    if parsed.scheme in {"http", "https"}:
        with urlopen(location, timeout=30) as response:  # noqa: S310
            decoded = json.loads(response.read().decode("utf-8"))
    elif parsed.scheme == "file":
        decoded = _load_json(Path(parsed.path))
    elif parsed.scheme:
        raise SystemExit(f"unsupported manifest URL scheme: {parsed.scheme}")
    else:
        decoded = _load_json(Path(location))
    if not isinstance(decoded, dict):
        raise SystemExit(f"manifest location did not return a JSON object: {location}")
    return decoded


def _load_json(path: Path) -> dict[str, Any]:
    with path.open() as handle:
        decoded = json.load(handle)
    if not isinstance(decoded, dict):
        raise SystemExit(f"JSON file did not contain an object: {path}")
    return decoded


if __name__ == "__main__":
    raise SystemExit(main())

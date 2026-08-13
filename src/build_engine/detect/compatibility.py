"""Compatibility guidance shared by the versioned framework registry."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from build_engine.detect.package_json import PackageJson

DOCS_BASE = "https://docs.mincemeat.id/static-sites/frameworks"


@dataclass(frozen=True, slots=True)
class Guidance:
    """User-actionable compatibility guidance sent in structured errors."""

    code: str
    title: str
    what_we_saw: str
    how_to_fix: str
    docs_url: str

    def to_dict(self) -> dict[str, str]:
        """Return the JSON-compatible payload form."""

        return {
            "code": self.code,
            "title": self.title,
            "what_we_saw": self.what_we_saw,
            "how_to_fix": self.how_to_fix,
            "docs_url": self.docs_url,
        }


@dataclass(frozen=True, slots=True)
class CompatibilityResult:
    """Result of static compatibility checks."""

    compatible: bool
    guidance: tuple[Guidance, ...] = ()


def check_static_compatibility(
    root: Path | str,
    framework_id: str,
    package_json: PackageJson | None,
    *,
    supported_frameworks: Collection[str] | None = None,
) -> CompatibilityResult:
    """Validate a detected profile against the supplied certified registry.

    Framework-specific rules belong in :mod:`build_engine.detect.framework`.
    This boundary intentionally contains only the registry membership check so
    deferred frameworks cannot acquire a silent compatibility fallback.
    """

    del root, package_json
    if supported_frameworks is None or framework_id in supported_frameworks:
        return CompatibilityResult(compatible=True)
    guidance = Guidance(
        code="FRAMEWORK_NOT_CERTIFIED",
        title="Framework is not certified for this build engine release",
        what_we_saw=f"Detected framework profile: {framework_id}",
        how_to_fix="Choose one of the certified static-site profiles for this release.",
        docs_url=f"{DOCS_BASE}/{framework_id}",
    )
    return CompatibilityResult(compatible=False, guidance=(guidance,))


def node_version_guidance(requested: str, supported: tuple[int, ...]) -> Guidance:
    """Build the guidance payload for an unsupported Node version range."""

    supported_text = ", ".join(str(value) for value in supported)
    return Guidance(
        code="NODE_VERSION_UNSUPPORTED",
        title="Node version is not supported by the build engine",
        what_we_saw=f"Requested Node version/range: {requested}",
        how_to_fix=f"Use one of the supported Node major versions: {supported_text}.",
        docs_url=f"{DOCS_BASE}/node",
    )


def generic_output_guidance(root: Path | str) -> Guidance:
    """Build migration guidance for callers that still inspect Generic output."""

    return Guidance(
        code="GENERIC_OUTPUT_NOT_FOUND",
        title="Generic build output could not be inferred",
        what_we_saw=f"No candidate output directory under {Path(root)} contained index.html",
        how_to_fix=(
            "Set an explicit output directory or make the build write index.html to out, dist, "
            "build, public, _site, or .output/public."
        ),
        docs_url=f"{DOCS_BASE}/generic",
    )

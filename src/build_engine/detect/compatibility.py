"""Static-site compatibility checks and guidance payloads."""

from __future__ import annotations

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
) -> CompatibilityResult:
    """Return compatibility guidance for frameworks with static-mode requirements."""

    project_root = Path(root)
    guidance: list[Guidance] = []

    if framework_id == "next-export":
        guidance.extend(_check_next_export(project_root))
    elif framework_id == "nuxt-generate":
        guidance.extend(_check_nuxt_generate(package_json))
    elif framework_id == "sveltekit-static":
        guidance.extend(_check_sveltekit_static(project_root, package_json))
    elif (
        framework_id == "generic"
        and package_json is not None
        and package_json.script("build") is None
    ):
        guidance.append(
            Guidance(
                code="GENERIC_REQUIRES_BUILD_SCRIPT",
                title="Generic project has no build script",
                what_we_saw="package.json does not define a build script",
                how_to_fix="Add a build script or choose a framework-specific build configuration.",
                docs_url=f"{DOCS_BASE}/generic",
            ),
        )

    return CompatibilityResult(compatible=not guidance, guidance=tuple(guidance))


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
    """Build the guidance payload when Generic output inference finds nothing."""

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


def _check_next_export(root: Path) -> tuple[Guidance, ...]:
    config = _first_existing(
        root,
        (
            "next.config.js",
            "next.config.mjs",
            "next.config.cjs",
            "next.config.ts",
        ),
    )
    if config is None:
        return (
            Guidance(
                code="NEXTJS_REQUIRES_EXPORT",
                title="Next.js project is not configured for static export",
                what_we_saw="No next.config file was found with output: 'export'",
                how_to_fix=(
                    "Add output: 'export' to next.config.js and ensure API routes and other "
                    "server-only features are not used."
                ),
                docs_url=f"{DOCS_BASE}/nextjs",
            ),
        )
    text = config.read_text(errors="ignore")
    normalized = "".join(text.split())
    if "output:'export'" not in normalized and 'output:"export"' not in normalized:
        return (
            Guidance(
                code="NEXTJS_REQUIRES_EXPORT",
                title="Next.js project is not configured for static export",
                what_we_saw=f"{config.name} does not set output: 'export'",
                how_to_fix=(
                    "Add output: 'export' to next.config.js and ensure API routes and other "
                    "server-only features are not used."
                ),
                docs_url=f"{DOCS_BASE}/nextjs",
            ),
        )
    return ()


def _check_nuxt_generate(package_json: PackageJson | None) -> tuple[Guidance, ...]:
    if package_json is None:
        return ()
    build_script = package_json.script("build") or ""
    generate_script = package_json.script("generate") or ""
    if "nuxi generate" in build_script or "nuxt generate" in build_script or generate_script:
        return ()
    if "nuxi build" in build_script or "nuxt build" in build_script:
        return (
            Guidance(
                code="NUXT_REQUIRES_GENERATE",
                title="Nuxt project uses the SSR build command",
                what_we_saw="package.json build script runs nuxi build/nuxt build",
                how_to_fix="Use nuxi generate for static output and set the build command to it.",
                docs_url=f"{DOCS_BASE}/nuxt",
            ),
        )
    return ()


def _check_sveltekit_static(
    root: Path,
    package_json: PackageJson | None,
) -> tuple[Guidance, ...]:
    has_adapter_dep = package_json is not None and package_json.has_dependency(
        "@sveltejs/adapter-static",
    )
    config = _first_existing(root, ("svelte.config.js", "svelte.config.mjs", "svelte.config.ts"))
    has_adapter_config = config is not None and "adapter-static" in config.read_text(
        errors="ignore"
    )
    if has_adapter_dep and has_adapter_config:
        return ()
    what_we_saw = "SvelteKit adapter-static was not found in dependencies or svelte.config"
    if has_adapter_dep:
        what_we_saw = "adapter-static is installed but svelte.config does not use it"
    elif has_adapter_config:
        what_we_saw = "svelte.config mentions adapter-static but package.json does not install it"
    return (
        Guidance(
            code="SVELTEKIT_REQUIRES_ADAPTER_STATIC",
            title="SvelteKit project is not configured for static output",
            what_we_saw=what_we_saw,
            how_to_fix="Install @sveltejs/adapter-static and configure svelte.config to use it.",
            docs_url=f"{DOCS_BASE}/sveltekit",
        ),
    )


def _first_existing(root: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        path = root / name
        if path.exists():
            return path
    return None

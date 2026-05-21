"""Framework detection and build-plan resolution."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from build_engine.detect.compatibility import (
    CompatibilityResult,
    Guidance,
    check_static_compatibility,
    generic_output_guidance,
    node_version_guidance,
)
from build_engine.detect.lockfiles import (
    PackageManager,
    PackageManagerDetection,
    detect_package_manager,
    install_command,
    run_script_command,
)
from build_engine.detect.package_json import PackageJson, load_package_json

SUPPORTED_NODE_MAJORS = (20, 22)
GENERIC_OUTPUT_CANDIDATES = ("out", "dist", "build", "public", "_site", ".output/public")


class DetectionError(ValueError):
    """Raised when project detection cannot produce a runnable build plan."""


@dataclass(frozen=True, slots=True)
class FrameworkProfile:
    """Static-site framework profile shipped in v1 GA."""

    id: str
    name: str
    default_command: str
    output_dir: str | None
    dependency_markers: tuple[str, ...] = ()
    script_markers: tuple[str, ...] = ()
    config_markers: tuple[str, ...] = ()
    node_based: bool = True


@dataclass(frozen=True, slots=True)
class FrameworkDetection:
    """Detected framework and source evidence."""

    profile: FrameworkProfile
    source: str


@dataclass(frozen=True, slots=True)
class NodeSelection:
    """Selected Node major/image and the evidence used."""

    major: int
    image: str
    source: str
    requested: str | None = None


@dataclass(frozen=True, slots=True)
class BuildPlan:
    """Resolved build plan consumed by later executor stages."""

    root: Path
    framework_id: str
    package_manager: PackageManager
    package_manager_source: str
    install_command: str
    build_command: str
    output_dir: str | None
    detected_output_dir: str | None
    image: str
    node_version: int | None
    compatibility: CompatibilityResult

    @property
    def guidance(self) -> tuple[Guidance, ...]:
        """Return compatibility guidance for callers that only need payloads."""

        return self.compatibility.guidance

    def to_job_payload_fields(self) -> dict[str, object]:
        """Return the fields that mirror the backend job.assign contract."""

        return {
            "framework_id": self.framework_id,
            "package_manager": self.package_manager,
            "image": self.image,
            "build_command": self.build_command,
            "output_dir": self.output_dir,
            "detected_output_dir": self.detected_output_dir,
        }


FRAMEWORK_PROFILES: dict[str, FrameworkProfile] = {
    "astro": FrameworkProfile(
        id="astro",
        name="Astro",
        default_command="astro build",
        output_dir="dist",
        dependency_markers=("astro",),
        script_markers=("astro build",),
    ),
    "vite": FrameworkProfile(
        id="vite",
        name="Vite",
        default_command="vite build",
        output_dir="dist",
        dependency_markers=("vite",),
        script_markers=("vite build",),
    ),
    "eleventy": FrameworkProfile(
        id="eleventy",
        name="Eleventy",
        default_command="eleventy",
        output_dir="_site",
        dependency_markers=("@11ty/eleventy", "eleventy"),
        script_markers=("eleventy",),
        config_markers=(".eleventy.js", "eleventy.config.js", "eleventy.config.mjs"),
    ),
    "docusaurus": FrameworkProfile(
        id="docusaurus",
        name="Docusaurus",
        default_command="docusaurus build",
        output_dir="build",
        dependency_markers=("@docusaurus/core",),
        script_markers=("docusaurus build",),
        config_markers=("docusaurus.config.js", "docusaurus.config.ts"),
    ),
    "vitepress": FrameworkProfile(
        id="vitepress",
        name="VitePress",
        default_command="vitepress build",
        output_dir=".vitepress/dist",
        dependency_markers=("vitepress",),
        script_markers=("vitepress build",),
    ),
    "vuepress": FrameworkProfile(
        id="vuepress",
        name="VuePress",
        default_command="vuepress build",
        output_dir="dist",
        dependency_markers=("vuepress", "vuepress-vite"),
        script_markers=("vuepress build",),
    ),
    "gatsby": FrameworkProfile(
        id="gatsby",
        name="Gatsby",
        default_command="gatsby build",
        output_dir="public",
        dependency_markers=("gatsby",),
        script_markers=("gatsby build",),
    ),
    "hugo": FrameworkProfile(
        id="hugo",
        name="Hugo",
        default_command="hugo",
        output_dir="public",
        config_markers=("hugo.toml", "hugo.yaml", "hugo.json", "config.toml"),
        node_based=False,
    ),
    "next-export": FrameworkProfile(
        id="next-export",
        name="Next.js static export",
        default_command="next build",
        output_dir="out",
        dependency_markers=("next",),
        script_markers=("next build",),
    ),
    "nuxt-generate": FrameworkProfile(
        id="nuxt-generate",
        name="Nuxt generate",
        default_command="nuxi generate",
        output_dir=".output/public",
        dependency_markers=("nuxt",),
        script_markers=("nuxi generate", "nuxt generate", "nuxi build", "nuxt build"),
    ),
    "sveltekit-static": FrameworkProfile(
        id="sveltekit-static",
        name="SvelteKit static",
        default_command="vite build",
        output_dir="build",
        dependency_markers=("@sveltejs/kit",),
        script_markers=("svelte-kit build",),
        config_markers=("svelte.config.js", "svelte.config.mjs", "svelte.config.ts"),
    ),
    "generic": FrameworkProfile(
        id="generic",
        name="Generic",
        default_command="",
        output_dir=None,
    ),
}

DETECTION_ORDER = (
    "astro",
    "next-export",
    "nuxt-generate",
    "sveltekit-static",
    "docusaurus",
    "vitepress",
    "vuepress",
    "gatsby",
    "eleventy",
    "vite",
)


def plan_build(
    root: Path | str,
    *,
    framework_override: str | None = None,
    build_command: str | None = None,
    output_dir: str | None = None,
    detected_output_dir: str | None = None,
    node_version: str | int | None = None,
) -> BuildPlan:
    """Detect a project and return the executable v1 build plan."""

    project_root = Path(root)
    package_json = load_package_json(project_root)
    pm_detection = detect_package_manager(project_root, package_json)
    framework = detect_framework(
        project_root,
        package_json=package_json,
        framework_override=framework_override,
    )
    node_selection = (
        select_node_version(package_json, override=node_version)
        if framework.profile.node_based and pm_detection.manager != "bun"
        else None
    )
    image = _select_image(framework.profile, pm_detection.manager, node_selection)
    compatibility = check_static_compatibility(project_root, framework.profile.id, package_json)
    resolved_output_dir = output_dir or detected_output_dir or framework.profile.output_dir
    return BuildPlan(
        root=project_root,
        framework_id=framework.profile.id,
        package_manager=pm_detection.manager,
        package_manager_source=pm_detection.source,
        install_command=install_command(
            pm_detection.manager,
            root=project_root,
            detection=pm_detection,
        ),
        build_command=build_command
        or _default_build_command(framework.profile, pm_detection, package_json),
        output_dir=resolved_output_dir,
        detected_output_dir=detected_output_dir,
        image=image,
        node_version=node_selection.major if node_selection is not None else None,
        compatibility=compatibility,
    )


def detect_framework(
    root: Path | str,
    *,
    package_json: PackageJson | None = None,
    framework_override: str | None = None,
) -> FrameworkDetection:
    """Detect one of the v1 GA framework profiles."""

    project_root = Path(root)
    package_json = package_json if package_json is not None else load_package_json(project_root)
    if framework_override:
        profile = FRAMEWORK_PROFILES.get(framework_override)
        if profile is None:
            raise DetectionError(f"Unknown framework override: {framework_override}")
        return FrameworkDetection(profile=profile, source="override")

    hugo_profile = FRAMEWORK_PROFILES["hugo"]
    if _has_config_marker(project_root, ("hugo.toml", "hugo.yaml", "hugo.json")):
        return FrameworkDetection(profile=hugo_profile, source="config")
    if package_json is None and (project_root / "config.toml").exists():
        return FrameworkDetection(profile=hugo_profile, source="config")

    if package_json is not None:
        for profile_id in DETECTION_ORDER:
            profile = FRAMEWORK_PROFILES[profile_id]
            if package_json.has_dependency(*profile.dependency_markers):
                return FrameworkDetection(profile=profile, source="dependency")
        for profile_id in DETECTION_ORDER:
            profile = FRAMEWORK_PROFILES[profile_id]
            if any(_script_contains(package_json, marker) for marker in profile.script_markers):
                return FrameworkDetection(profile=profile, source="script")
        if package_json.script("build") is not None:
            return FrameworkDetection(profile=FRAMEWORK_PROFILES["generic"], source="build-script")

    raise DetectionError("No supported static-site framework or Generic build script was detected")


def select_node_version(
    package_json: PackageJson | None,
    *,
    override: str | int | None = None,
    supported: tuple[int, ...] = SUPPORTED_NODE_MAJORS,
) -> NodeSelection:
    """Select the newest supported Node major satisfying config and engines.node."""

    requested = (
        str(override)
        if override is not None
        else package_json.engines_node
        if package_json
        else None
    )
    if requested is None or not requested.strip():
        major = max(supported)
        return NodeSelection(major=major, image=f"node:{major}", source="default")
    requested = requested.strip()
    candidates = [major for major in supported if _node_major_satisfies(major, requested)]
    if not candidates:
        guidance = node_version_guidance(requested, supported)
        raise DetectionError(guidance.how_to_fix)
    major = max(candidates)
    source = "override" if override is not None else "engines.node"
    return NodeSelection(major=major, image=f"node:{major}", source=source, requested=requested)


def infer_generic_output(root: Path | str) -> str:
    """Infer Generic output using the v1 candidate order."""

    project_root = Path(root)
    for candidate in GENERIC_OUTPUT_CANDIDATES:
        path = project_root / candidate
        if (path / "index.html").is_file():
            return candidate
    guidance = generic_output_guidance(project_root)
    raise DetectionError(guidance.how_to_fix)


def _default_build_command(
    profile: FrameworkProfile,
    pm_detection: PackageManagerDetection,
    package_json: PackageJson | None,
) -> str:
    if not profile.node_based:
        return profile.default_command
    if package_json is not None:
        script_name = _preferred_script(profile, package_json)
        if script_name is not None:
            return run_script_command(pm_detection.manager, script_name)
    if profile.id == "generic":
        return run_script_command(pm_detection.manager, "build")
    return profile.default_command


def _preferred_script(profile: FrameworkProfile, package_json: PackageJson) -> str | None:
    if profile.id == "nuxt-generate" and package_json.script("generate") is not None:
        return "generate"
    for script_name in ("build", "docs:build"):
        script = package_json.script(script_name)
        if script is not None and _script_matches_profile(profile, script):
            return script_name
    if profile.id in {"vitepress", "vuepress"} and package_json.script("docs:build") is not None:
        return "docs:build"
    if profile.id == "generic" and package_json.script("build") is not None:
        return "build"
    return None


def _script_matches_profile(profile: FrameworkProfile, script: str) -> bool:
    if profile.id == "generic":
        return True
    markers = profile.dependency_markers + (profile.default_command.split()[0],)
    return any(marker.replace("@11ty/", "") in script for marker in markers)


def _select_image(
    profile: FrameworkProfile,
    package_manager: PackageManager,
    node_selection: NodeSelection | None,
) -> str:
    if not profile.node_based:
        return "hugo:latest"
    if package_manager == "bun":
        return "bun:1"
    if node_selection is None:
        raise DetectionError("Node selection is required for Node-based framework profiles")
    return node_selection.image


def _has_config_marker(root: Path, markers: tuple[str, ...]) -> bool:
    return any((root / marker).exists() for marker in markers)


def _script_contains(package_json: PackageJson, marker: str) -> bool:
    return any(marker in script for script in package_json.scripts.values())


def _node_major_satisfies(major: int, range_text: str) -> bool:
    alternatives = [part.strip() for part in range_text.split("||")]
    return any(
        _node_clause_satisfies(major, alternative) for alternative in alternatives if alternative
    )


def _node_clause_satisfies(major: int, clause: str) -> bool:
    tokens = re.findall(r"(?:>=|<=|>|<|=|\^|~)?\s*\d+(?:\.\d+)?(?:\.\d+)?|[xX*]", clause)
    if not tokens:
        return False
    return all(_node_token_satisfies(major, token.strip()) for token in tokens)


def _node_token_satisfies(major: int, token: str) -> bool:
    if token in {"*", "x", "X"}:
        return True
    match = re.match(r"(?P<op>>=|<=|>|<|=|\^|~)?\s*(?P<major>\d+)", token)
    if match is None:
        return False
    op = match.group("op") or "="
    requested_major = int(match.group("major"))
    match op:
        case "=":
            return major == requested_major
        case ">=":
            return major >= requested_major
        case ">":
            return major > requested_major
        case "<=":
            return major <= requested_major
        case "<":
            return major < requested_major
        case "^" | "~":
            return major == requested_major
    return False

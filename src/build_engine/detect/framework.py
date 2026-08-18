"""Framework detection and build-plan resolution."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version

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

SUPPORTED_NODE_MAJORS = (22,)
GENERIC_OUTPUT_CANDIDATES = ("out", "dist", "build", "public", "_site", ".output/public")


class DetectionError(ValueError):
    """Raised when project detection cannot produce a runnable build plan."""


@dataclass(frozen=True, slots=True)
class FrameworkProfile:
    """Versioned static-site framework profile."""

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
        """Return safe profile metadata for an attempt status event."""

        return {
            "framework_id": self.framework_id,
            "package_manager": self.package_manager,
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
        config_markers=("hugo.toml", "hugo.yaml", "hugo.json"),
        node_based=False,
    ),
}

SUPPORTED_FRAMEWORK_IDS = frozenset(FRAMEWORK_PROFILES)
DEFERRED_FRAMEWORK_IDS = frozenset(
    {
        "bun",
        "yarn",
        "zola",
        "generic",
        "angular",
        "remix",
        "next",
        "nuxt",
        "sveltekit",
    }
)

DETECTION_ORDER = (
    "astro",
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
    """Detect a project and return a certified v2 build plan."""

    project_root = Path(root)
    package_json = load_package_json(project_root)
    pm_detection = detect_package_manager(project_root, package_json)
    framework = detect_framework(
        project_root,
        package_json=package_json,
        framework_override=framework_override,
    )
    if framework.profile.node_based and pm_detection.manager not in {"npm", "pnpm"}:
        raise DetectionError("Only npm and pnpm are certified for Node-based profiles")
    selected_package_manager: PackageManager = (
        pm_detection.manager if framework.profile.node_based else "none"
    )
    node_selection = (
        select_node_version(package_json, override=node_version, supported=SUPPORTED_NODE_MAJORS)
        if framework.profile.node_based
        else None
    )
    image = _select_image(framework.profile, node_selection)
    compatibility = check_static_compatibility(
        project_root,
        framework.profile.id,
        package_json,
        supported_frameworks=SUPPORTED_FRAMEWORK_IDS,
    )
    selected_build_command = build_command or _default_build_command(
        framework.profile, pm_detection, package_json
    )
    resolved_output_dir = (
        output_dir
        or detected_output_dir
        or _profile_output_dir(framework.profile, selected_build_command)
    )
    return BuildPlan(
        root=project_root,
        framework_id=framework.profile.id,
        package_manager=selected_package_manager,
        package_manager_source=pm_detection.source if framework.profile.node_based else "profile",
        install_command=install_command(
            selected_package_manager,
            root=project_root,
            detection=pm_detection,
        ),
        build_command=selected_build_command,
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
    """Detect one framework profile from the versioned registry."""

    project_root = Path(root)
    package_json = package_json if package_json is not None else load_package_json(project_root)
    if framework_override:
        profile = FRAMEWORK_PROFILES.get(framework_override)
        if profile is None:
            if framework_override in DEFERRED_FRAMEWORK_IDS:
                raise DetectionError(
                    f"Framework {framework_override!r} is deferred and not certified "
                    "for this release"
                )
            raise DetectionError(f"Unknown framework override: {framework_override}")
        return FrameworkDetection(profile=profile, source="override")

    hugo_profile = FRAMEWORK_PROFILES["hugo"]
    if _has_config_marker(project_root, ("hugo.toml", "hugo.yaml", "hugo.json")):
        return FrameworkDetection(profile=hugo_profile, source="config")
    if package_json is not None:
        if package_json.has_dependency("next", "nuxt", "@sveltejs/kit", "@angular/core"):
            raise DetectionError(
                "Detected a deferred framework; this release supports only the curated eight"
            )
        for profile_id in DETECTION_ORDER:
            profile = FRAMEWORK_PROFILES[profile_id]
            if package_json.has_dependency(*profile.dependency_markers):
                return FrameworkDetection(profile=profile, source="dependency")
        for profile_id in DETECTION_ORDER:
            profile = FRAMEWORK_PROFILES[profile_id]
            if any(
                _script_contains_command(package_json, marker) for marker in profile.script_markers
            ):
                return FrameworkDetection(profile=profile, source="script")

    raise DetectionError("No certified static-site framework was detected")


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
    """Infer output for migration tooling; Generic is not a production profile."""

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
    return profile.default_command


def _preferred_script(profile: FrameworkProfile, package_json: PackageJson) -> str | None:
    for script_name in ("build", "docs:build"):
        script = package_json.script(script_name)
        if script is not None and _script_matches_profile(profile, script):
            return script_name
    if profile.id in {"vitepress", "vuepress"} and package_json.script("docs:build") is not None:
        return "docs:build"
    return None


def _script_matches_profile(profile: FrameworkProfile, script: str) -> bool:
    commands = {
        *profile.script_markers,
        profile.default_command,
        *(marker.rsplit("/", maxsplit=1)[-1] for marker in profile.dependency_markers),
    }
    return any(_command_tokens_match(script, command) for command in commands if command)


def _select_image(
    profile: FrameworkProfile,
    node_selection: NodeSelection | None,
) -> str:
    if not profile.node_based:
        return "hugo:latest"
    if node_selection is None:
        raise DetectionError("Node selection is required for Node-based framework profiles")
    return node_selection.image


def _profile_output_dir(profile: FrameworkProfile, build_command: str) -> str | None:
    """Resolve output paths for profiles whose CLI target changes the root."""

    if profile.id == "vitepress" and _build_targets_docs(build_command):
        return "docs/.vitepress/dist"
    if profile.id == "vuepress" and _build_targets_docs(build_command):
        return "docs/.vuepress/dist"
    return profile.output_dir


def _build_targets_docs(build_command: str) -> bool:
    """Return whether a VitePress/VuePress command builds the docs subdirectory."""

    return "docs:build" in build_command or bool(
        re.search(r"(?:vitepress|vuepress)\s+build\s+docs(?:\s|$)", build_command)
    )


def _has_config_marker(root: Path, markers: tuple[str, ...]) -> bool:
    return any((root / marker).exists() for marker in markers)


def _script_contains_command(package_json: PackageJson, command: str) -> bool:
    return any(_command_tokens_match(script, command) for script in package_json.scripts.values())


def _node_major_satisfies(major: int, range_text: str) -> bool:
    alternatives = [part.strip() for part in range_text.split("||")]
    return any(
        _node_clause_satisfies(major, alternative) for alternative in alternatives if alternative
    )


def _node_clause_satisfies(major: int, clause: str) -> bool:
    try:
        specifier = _node_clause_to_specifier_set(clause)
    except InvalidSpecifier:
        return False
    return any(
        specifier.contains(candidate, prereleases=True)
        for candidate in (Version(f"{major}.0.0"), Version(f"{major}.999.999"))
    )


def _node_clause_to_specifier_set(clause: str) -> SpecifierSet:
    normalized = clause.strip()
    if normalized in {"*", "x", "X"}:
        return SpecifierSet(">=0")
    parts = [_node_token_to_specifier(token) for token in normalized.split()]
    return SpecifierSet(",".join(part for part in parts if part))


def _node_token_to_specifier(token: str) -> str:
    if token in {"*", "x", "X"}:
        return ">=0"
    match = re.fullmatch(
        r"(?P<op>>=|<=|>|<|=|\^|~)?(?P<version>\d+(?:\.\d+)?(?:\.\d+)?)(?:\.[xX*])?",
        token,
    )
    if match is None:
        raise InvalidSpecifier(token)
    op = match.group("op") or "=="
    version = _complete_node_version(match.group("version"))
    if op == "=":
        op = "=="
    if op == "^":
        major = int(version.split(".", maxsplit=1)[0])
        return f">={version},<{major + 1}.0.0"
    if op == "~":
        major_text, minor_text, _patch_text = version.split(".")
        return f">={version},<{major_text}.{int(minor_text) + 1}.0"
    if op == "==" and token.endswith((".x", ".X", ".*")):
        return f"=={match.group('version')}.*"
    return f"{op}{version}"


def _complete_node_version(version: str) -> str:
    parts = version.split(".")
    return ".".join([*parts, *(["0"] * (3 - len(parts)))])


def _command_tokens_match(script: str, command: str) -> bool:
    script_tokens = _shell_tokens(script)
    command_tokens = _shell_tokens(command)
    if not command_tokens or len(command_tokens) > len(script_tokens):
        return False
    return any(
        tuple(script_tokens[index : index + len(command_tokens)]) == tuple(command_tokens)
        for index in range(len(script_tokens) - len(command_tokens) + 1)
    )


def _shell_tokens(value: str) -> list[str]:
    try:
        return shlex.split(value)
    except ValueError:
        return value.split()

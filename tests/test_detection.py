"""Certified framework registry and compatibility tests."""

from pathlib import Path

import pytest

from build_engine.detect.compatibility import check_static_compatibility
from build_engine.detect.framework import (
    DEFERRED_FRAMEWORK_IDS,
    SUPPORTED_FRAMEWORK_IDS,
    DetectionError,
    infer_generic_output,
    plan_build,
    select_node_version,
)
from build_engine.detect.lockfiles import (
    PackageManagerDetectionError,
    detect_package_manager,
    install_command,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sites"


@pytest.mark.parametrize(
    ("fixture", "framework_id", "package_manager", "image", "build_command", "output_dir"),
    (
        ("astro-blog", "astro", "pnpm", "node:22", "pnpm run build", "dist"),
        ("vite-vanilla", "vite", "npm", "node:22", "npm run build", "dist"),
        ("eleventy-blog", "eleventy", "npm", "node:22", "npm run build", "_site"),
        ("docusaurus-docs", "docusaurus", "npm", "node:22", "npm run build", "build"),
        (
            "vitepress-docs",
            "vitepress",
            "pnpm",
            "node:22",
            "pnpm run docs:build",
            "docs/.vitepress/dist",
        ),
        (
            "vuepress-docs",
            "vuepress",
            "pnpm",
            "node:22",
            "pnpm run docs:build",
            "docs/.vuepress/dist",
        ),
        ("gatsby-blog", "gatsby", "npm", "node:22", "npm run build", "public"),
        ("hugo-quickstart", "hugo", "none", "hugo:latest", "hugo", "public"),
    ),
)
def test_certified_fixture_profiles_resolve_build_plans(
    fixture: str,
    framework_id: str,
    package_manager: str,
    image: str,
    build_command: str,
    output_dir: str,
) -> None:
    plan = plan_build(FIXTURES / fixture)

    assert plan.framework_id == framework_id
    assert plan.package_manager == package_manager
    assert plan.image == image
    assert plan.build_command == build_command
    assert plan.output_dir == output_dir
    assert plan.compatibility.compatible


def test_registry_contains_only_the_eight_production_profiles() -> None:
    assert {
        "astro",
        "vite",
        "eleventy",
        "docusaurus",
        "vitepress",
        "vuepress",
        "gatsby",
        "hugo",
    } == SUPPORTED_FRAMEWORK_IDS
    assert {
        "bun",
        "yarn",
        "zola",
        "generic",
        "angular",
        "remix",
        "next",
        "nuxt",
        "sveltekit",
    } <= DEFERRED_FRAMEWORK_IDS


def test_deferred_frameworks_fail_without_silent_fallback() -> None:
    for fixture in ("nextjs-export", "nuxt-generate", "sveltekit-static", "generic-static"):
        with pytest.raises(DetectionError, match="curated eight|deferred|certified"):
            plan_build(FIXTURES / fixture)


def test_package_manager_prefers_package_manager_field_over_lockfile() -> None:
    detection = detect_package_manager(FIXTURES / "astro-blog")

    assert detection.manager == "pnpm"
    assert detection.source == "packageManager"
    assert detection.version == "9.12.0"


def test_unsupported_package_manager_is_blocked(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"packageManager":"yarn@1.22.0"}')

    with pytest.raises(PackageManagerDetectionError, match="supported managers"):
        detect_package_manager(tmp_path)


def test_framework_detection_requires_certified_script_command(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts":{"build":"echo astro buildable"}}')

    with pytest.raises(DetectionError, match="certified"):
        plan_build(tmp_path)


def test_npm_install_uses_ci_when_lockfile_exists() -> None:
    detection = detect_package_manager(FIXTURES / "vite-vanilla")

    assert detection.manager == "npm"
    assert install_command("npm", root=FIXTURES / "vite-vanilla", detection=detection) == "npm ci"


def test_node_version_selects_only_supported_major() -> None:
    selection = select_node_version(None, override=">=20 <23")

    assert selection.major == 22
    assert selection.image == "node:22"


def test_node_version_rejects_ranges_outside_supported_majors() -> None:
    with pytest.raises(DetectionError, match="supported Node major versions"):
        select_node_version(None, override=">=24")


def test_hugo_has_no_node_package_manager() -> None:
    plan = plan_build(FIXTURES / "hugo-quickstart")

    assert plan.package_manager == "none"
    assert plan.install_command == ""


def test_generic_output_inference_remains_migration_only(tmp_path: Path) -> None:
    output = tmp_path / "dist"
    output.mkdir()
    (output / "index.html").write_text("<!doctype html>")

    assert infer_generic_output(tmp_path) == "dist"


def test_generic_output_inference_returns_guidance_when_missing() -> None:
    with pytest.raises(DetectionError, match="Set an explicit output directory"):
        infer_generic_output(FIXTURES / "generic-static")


def test_compatibility_checker_blocks_profiles_outside_the_registry() -> None:
    result = check_static_compatibility(
        FIXTURES / "nextjs-noexport",
        "next-export",
        None,
        supported_frameworks=SUPPORTED_FRAMEWORK_IDS,
    )

    assert result.guidance[0].code == "FRAMEWORK_NOT_CERTIFIED"

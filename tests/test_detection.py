"""Build detection and planning tests."""

from pathlib import Path

import pytest

from build_engine.detect.compatibility import check_static_compatibility
from build_engine.detect.framework import (
    DetectionError,
    infer_generic_output,
    plan_build,
    select_node_version,
)
from build_engine.detect.lockfiles import detect_package_manager, install_command

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
            ".vitepress/dist",
        ),
        ("vuepress-docs", "vuepress", "pnpm", "node:22", "pnpm run docs:build", "dist"),
        ("gatsby-blog", "gatsby", "npm", "node:22", "npm run build", "public"),
        ("hugo-quickstart", "hugo", "npm", "hugo:latest", "hugo", "public"),
        ("nextjs-export", "next-export", "pnpm", "node:22", "pnpm run build", "out"),
        (
            "nuxt-generate",
            "nuxt-generate",
            "pnpm",
            "node:22",
            "pnpm run generate",
            ".output/public",
        ),
        (
            "sveltekit-static",
            "sveltekit-static",
            "pnpm",
            "node:22",
            "pnpm run build",
            "build",
        ),
        ("generic-static", "generic", "npm", "node:22", "npm run build", None),
    ),
)
def test_v1_ga_fixture_profiles_resolve_build_plans(
    fixture: str,
    framework_id: str,
    package_manager: str,
    image: str,
    build_command: str,
    output_dir: str | None,
) -> None:
    plan = plan_build(FIXTURES / fixture)

    assert plan.framework_id == framework_id
    assert plan.package_manager == package_manager
    assert plan.image == image
    assert plan.build_command == build_command
    assert plan.output_dir == output_dir
    assert plan.compatibility.compatible


def test_package_manager_prefers_package_manager_field_over_lockfile() -> None:
    detection = detect_package_manager(FIXTURES / "astro-blog")

    assert detection.manager == "pnpm"
    assert detection.source == "packageManager"
    assert detection.version == "9.12.0"


def test_framework_detection_uses_package_scripts_without_dependency_marker(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts": {"build": "astro build"}}')

    plan = plan_build(tmp_path)

    assert plan.framework_id == "astro"
    assert plan.build_command == "npm run build"


def test_npm_install_uses_ci_when_lockfile_exists() -> None:
    detection = detect_package_manager(FIXTURES / "vite-vanilla")

    assert detection.manager == "npm"
    assert install_command("npm", root=FIXTURES / "vite-vanilla", detection=detection) == "npm ci"


def test_node_version_selects_newest_supported_major_from_range() -> None:
    selection = select_node_version(None, override=">=20 <23")

    assert selection.major == 22
    assert selection.image == "node:22"


def test_node_version_rejects_ranges_outside_supported_majors() -> None:
    with pytest.raises(DetectionError, match="supported Node major versions"):
        select_node_version(None, override=">=24")


def test_next_export_without_export_config_returns_guidance() -> None:
    plan = plan_build(FIXTURES / "nextjs-noexport")

    assert not plan.compatibility.compatible
    assert plan.guidance[0].to_dict()["code"] == "NEXTJS_REQUIRES_EXPORT"


def test_generic_output_inference_uses_first_index_html_candidate() -> None:
    assert infer_generic_output(FIXTURES / "generic-output") == "dist"


def test_generic_output_inference_returns_guidance_when_missing() -> None:
    with pytest.raises(DetectionError, match="Set an explicit output directory"):
        infer_generic_output(FIXTURES / "generic-static")


def test_sveltekit_static_guidance_requires_adapter_config(tmp_path: Path) -> None:
    package_json_path = tmp_path / "package.json"
    package_json_path.write_text(
        """
        {
          "scripts": {"build": "vite build"},
          "dependencies": {"@sveltejs/kit": "^2.0.0"}
        }
        """
    )
    plan = plan_build(tmp_path)

    assert plan.framework_id == "sveltekit-static"
    assert not plan.compatibility.compatible
    assert plan.guidance[0].code == "SVELTEKIT_REQUIRES_ADAPTER_STATIC"


def test_compatibility_guidance_payload_shape() -> None:
    result = check_static_compatibility(FIXTURES / "nextjs-noexport", "next-export", None)

    assert result.guidance[0].to_dict() == {
        "code": "NEXTJS_REQUIRES_EXPORT",
        "title": "Next.js project is not configured for static export",
        "what_we_saw": "next.config.js does not set output: 'export'",
        "how_to_fix": (
            "Add output: 'export' to next.config.js and ensure API routes and other "
            "server-only features are not used."
        ),
        "docs_url": "https://docs.mincemeat.id/static-sites/frameworks/nextjs",
    }

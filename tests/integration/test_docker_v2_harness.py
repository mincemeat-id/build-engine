"""Required opt-in Docker harness for the eight certified v2 profiles."""

from __future__ import annotations

import asyncio
import os
import shutil
import tarfile
from pathlib import Path

import pytest

from build_engine.config import EngineConfig
from build_engine.detect.framework import BuildPlan, plan_build
from build_engine.executor.artifact import package_output
from build_engine.executor.docker_runner import (
    CacheMount,
    DockerRunSpec,
    load_bundled_image_manifest,
    pull_image,
    resolve_image_reference,
    run_container,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "sites"

CASES = (
    ("astro-blog", "astro", "node:22"),
    ("vite-vanilla", "vite", "node:22"),
    ("eleventy-blog", "eleventy", "node:22"),
    ("docusaurus-docs", "docusaurus", "node:22"),
    ("vitepress-docs", "vitepress", "node:22"),
    ("vuepress-docs", "vuepress", "node:22"),
    ("gatsby-blog", "gatsby", "node:22"),
    ("hugo-quickstart", "hugo", "hugo:latest"),
)


@pytest.mark.skipif(
    os.environ.get("BUILD_ENGINE_DOCKER_INTEGRATION") != "1",
    reason="required Docker harness is opt-in because it pulls and builds real images",
)
@pytest.mark.parametrize(("fixture", "framework", "image_key"), CASES)
def test_certified_fixture_builds_in_pinned_builder(
    tmp_path: Path,
    fixture: str,
    framework: str,
    image_key: str,
) -> None:
    asyncio.run(
        _certified_fixture_builds_in_pinned_builder(
            tmp_path,
            fixture=fixture,
            framework=framework,
            image_key=image_key,
        )
    )


async def _certified_fixture_builds_in_pinned_builder(
    tmp_path: Path,
    *,
    fixture: str,
    framework: str,
    image_key: str,
) -> None:
    source_root = tmp_path / "src"
    output_root = tmp_path / "out"
    shutil.copytree(FIXTURE_ROOT / fixture, source_root)
    plan = plan_build(source_root)
    assert plan.framework_id == framework
    manifest = load_bundled_image_manifest()
    image = resolve_image_reference(image_key, manifest=manifest)
    assert image_key in manifest
    assert framework in manifest[image_key].frameworks

    await asyncio.to_thread(pull_image, image, timeout_seconds=600)
    logs: list[tuple[str, str]] = []

    async def publish(stream: str, data: str) -> None:
        logs.append((stream, data))

    result = await run_container(
        DockerRunSpec(
            image=image,
            project_root=source_root,
            command="",
            source_root=source_root,
            output_root=output_root,
            build_manifest=_build_manifest(plan),
            config=EngineConfig(
                state_dir=tmp_path / "state",
                build_timeout_seconds=900,
                sigterm_grace_seconds=10,
            ),
            timeout_seconds=900,
            cache_mounts=(
                CacheMount(tmp_path / "cache", "/cache"),
                CacheMount(tmp_path / "node-home", "/home/node"),
            ),
        ),
        publish_log=publish,
    )
    assert result.exit_code == 0, "\n".join(f"{stream}: {data}" for stream, data in logs)

    artifact = package_output(
        project_root=tmp_path,
        output_dir="out",
        destination=tmp_path / "artifact.tar.gz",
        max_bytes=524_288_000,
    )
    with tarfile.open(artifact.path, mode="r:gz") as archive:
        assert "index.html" in archive.getnames()


def _build_manifest(plan: BuildPlan) -> dict[str, object]:
    """Create the only build-command contract a certified image receives."""

    return {
        "framework": plan.framework_id,
        "package_manager": plan.package_manager,
        "root": ".",
        "install_command": plan.install_command,
        "build_command": plan.build_command,
        "output_dir": plan.output_dir or "",
        "env": {},
    }

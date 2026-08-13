"""Opt-in real Docker integration tests."""

from __future__ import annotations

import asyncio
import os
import tarfile
from pathlib import Path

import pytest

from build_engine.config import EngineConfig
from build_engine.executor.artifact import package_output
from build_engine.executor.docker_runner import (
    DockerRunSpec,
    load_bundled_image_manifest,
    pull_image,
    resolve_image_reference,
    run_container,
)


@pytest.mark.skipif(
    os.environ.get("BUILD_ENGINE_DOCKER_TESTS") != "1",
    reason="real Docker integration tests are opt-in",
)
def test_hugo_latest_fixture_builds_and_packages_output(tmp_path: Path) -> None:
    asyncio.run(_hugo_latest_fixture_builds_and_packages_output(tmp_path))


async def _hugo_latest_fixture_builds_and_packages_output(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    project_root.chmod(0o777)
    (project_root / "hugo.toml").write_text(
        'baseURL = "https://example.test/"\ntitle = "Mincemeat Smoke"\n',
        encoding="utf-8",
    )
    content = project_root / "content"
    content.mkdir()
    (content / "_index.md").write_text("# Smoke\n\nBuilt by Hugo.\n", encoding="utf-8")
    logs: list[tuple[str, str]] = []

    async def publish(stream: str, data: str) -> None:
        logs.append((stream, data))

    image = resolve_image_reference("hugo:latest", manifest=load_bundled_image_manifest())
    await asyncio.to_thread(pull_image, image, timeout_seconds=180)
    result = await run_container(
        DockerRunSpec(
            image=image,
            project_root=project_root,
            command="hugo --destination public",
            config=EngineConfig(
                state_dir=tmp_path / "state",
                build_timeout_seconds=60,
                sigterm_grace_seconds=1,
            ),
        ),
        publish_log=publish,
    )

    assert result.exit_code == 0, logs
    artifact = package_output(
        project_root=project_root,
        output_dir="public",
        destination=tmp_path / "artifact.tar.gz",
        max_bytes=1_000_000,
    )
    with tarfile.open(artifact.path, mode="r:gz") as tar:
        assert "index.html" in tar.getnames()

"""Opt-in simulation against the final build-engine-images 1.0.0 release."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from build_engine.agent import job_loop
from build_engine.config import EngineConfig
from build_engine.executor.artifact import package_output
from build_engine.executor.cache import CacheMount
from build_engine.executor.docker_runner import (
    DockerRunSpec,
    load_image_manifest,
    pull_image,
    run_container,
)
from build_engine.executor.network import DockerNetworkGuard

ROOT = Path(__file__).resolve().parents[2]
SITE_FIXTURES = ROOT / "tests" / "fixtures" / "sites"
FINAL_MANIFEST = ROOT / "tests" / "fixtures" / "manifests" / "build-engine-images-v1.0.0.json"


@dataclass(frozen=True, slots=True)
class FinalImageCase:
    id: str
    configured_image: str
    framework: str
    package_manager: str
    source_fixture: str | None
    build_command: str
    output_dir: str


FINAL_IMAGE_CASES = (
    FinalImageCase(
        id="node20",
        configured_image="node:20",
        framework="generic",
        package_manager="none",
        source_fixture="generic-static",
        build_command=(
            "node --version && mkdir -p dist && printf '<!doctype html>node20' > dist/index.html"
        ),
        output_dir="dist",
    ),
    FinalImageCase(
        id="node22",
        configured_image="node:22",
        framework="generic",
        package_manager="none",
        source_fixture="generic-static",
        build_command=(
            "node --version && mkdir -p dist && printf '<!doctype html>node22' > dist/index.html"
        ),
        output_dir="dist",
    ),
    FinalImageCase(
        id="bun1",
        configured_image="bun:1",
        framework="generic",
        package_manager="none",
        source_fixture="generic-static",
        build_command=(
            "bun --version && mkdir -p dist && printf '<!doctype html>bun' > dist/index.html"
        ),
        output_dir="dist",
    ),
    FinalImageCase(
        id="hugo",
        configured_image="hugo:latest",
        framework="hugo",
        package_manager="none",
        source_fixture="hugo-quickstart",
        build_command=(
            "hugo version && mkdir -p public && printf '<!doctype html>hugo' > public/index.html"
        ),
        output_dir="public",
    ),
    FinalImageCase(
        id="zola",
        configured_image="zola:latest",
        framework="zola",
        package_manager="none",
        source_fixture=None,
        build_command=(
            "zola --version && mkdir -p public && printf '<!doctype html>zola' > public/index.html"
        ),
        output_dir="public",
    ),
)


@pytest.mark.skipif(
    os.environ.get("BUILD_ENGINE_FINAL_IMAGE_TESTS") != "1",
    reason="final build-engine-images simulation is opt-in because it pulls public GHCR images",
)
@pytest.mark.parametrize("case", FINAL_IMAGE_CASES, ids=lambda case: case.id)
def test_final_1_0_image_executes_entrypoint_contract(
    tmp_path: Path,
    case: FinalImageCase,
) -> None:
    asyncio.run(_run_final_image_case(tmp_path, case))


async def _run_final_image_case(tmp_path: Path, case: FinalImageCase) -> None:
    manifest = load_image_manifest(FINAL_MANIFEST)
    resolved_image = job_loop._resolve_builder_image(
        case.configured_image,
        {"image_manifest_path": str(FINAL_MANIFEST)},
    )
    assert resolved_image == manifest[case.configured_image].reference
    assert "@sha256:" in resolved_image

    project_root = _prepare_project(tmp_path, case)
    output_root = tmp_path / "normalized-out"
    cache_root = tmp_path / "cache"
    build_manifest = {
        "framework": case.framework,
        "package_manager": case.package_manager,
        "root": ".",
        "install_command": "",
        "build_command": case.build_command,
        "output_dir": case.output_dir,
        "env": {},
    }
    logs: list[tuple[str, str]] = []

    async def publish(stream: str, data: str) -> None:
        logs.append((stream, data))

    await asyncio.to_thread(pull_image, resolved_image, timeout_seconds=600)
    result = await run_container(
        DockerRunSpec(
            image=resolved_image,
            project_root=project_root,
            command=case.build_command,
            config=EngineConfig(
                state_dir=tmp_path / "state",
                build_timeout_seconds=120,
                sigterm_grace_seconds=1,
            ),
            network_guard=DockerNetworkGuard(name="none"),
            source_root=project_root,
            output_root=output_root,
            build_manifest=build_manifest,
            cache_mounts=(CacheMount(host_path=cache_root, container_path="/cache"),),
        ),
        publish_log=publish,
    )

    assert result.exit_code == 0, logs
    artifact = package_output(
        project_root=project_root,
        output_dir=case.output_dir,
        destination=tmp_path / f"{case.id}.tar.gz",
        max_bytes=1_000_000,
    )
    with tarfile.open(artifact.path, mode="r:gz") as tar:
        assert "index.html" in tar.getnames()
    assert (output_root / "index.html").is_file()


def test_final_manifest_fixture_matches_repo_snapshot() -> None:
    release_fixture = json.loads(FINAL_MANIFEST.read_text(encoding="utf-8"))
    repo_snapshot = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))

    assert release_fixture == repo_snapshot


def _prepare_project(tmp_path: Path, case: FinalImageCase) -> Path:
    project_root = tmp_path / case.id / "src"
    if case.source_fixture is None:
        project_root.mkdir(parents=True)
        (project_root / "config.toml").write_text(
            'base_url = "https://example.test"\ntitle = "Mincemeat Smoke"\n',
            encoding="utf-8",
        )
    else:
        shutil.copytree(SITE_FIXTURES / case.source_fixture, project_root)
    _make_writable(project_root)
    return project_root


def _make_writable(root: Path) -> None:
    root.chmod(0o777)
    for path in root.rglob("*"):
        if path.is_dir():
            path.chmod(0o777)
        else:
            path.chmod(0o666)

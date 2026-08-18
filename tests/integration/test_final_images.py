"""Opt-in Docker checks for the pinned production builder manifest."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from build_engine.executor.docker_runner import load_image_manifest, pull_image

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "manifest.json"

CASES = (
    ("astro", "node:22"),
    ("vite", "node:22"),
    ("eleventy", "node:22"),
    ("docusaurus", "node:22"),
    ("vitepress", "node:22"),
    ("vuepress", "node:22"),
    ("gatsby", "node:22"),
    ("hugo", "hugo:latest"),
)


def test_release_manifest_is_the_shipped_manifest() -> None:
    fixture = ROOT / "tests/fixtures/manifests/build-engine-images-v1.0.0.json"
    assert json.loads(fixture.read_text()) == json.loads(MANIFEST_PATH.read_text())


@pytest.mark.skipif(
    os.environ.get("BUILD_ENGINE_DOCKER_INTEGRATION") != "1",
    reason="production Docker fixture harness is opt-in",
)
@pytest.mark.parametrize(("framework", "image_key"), CASES)
def test_pinned_builder_image_is_pullable(framework: str, image_key: str) -> None:
    manifest = load_image_manifest(MANIFEST_PATH)
    entry = manifest[image_key]
    assert framework in entry.frameworks
    assert "@sha256:" in entry.reference
    pull_image(entry.reference, timeout_seconds=600)

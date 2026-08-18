"""Production v2 fixture and security smoke tests.

The filename is retained for downstream test invocations that referenced the
old smoke module; the assertions now cover only the certified v2 matrix.
"""

from pathlib import Path

import pytest

from build_engine.detect.framework import DetectionError, plan_build
from build_engine.queue.store import SQLiteQueueStore

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures" / "sites"


@pytest.mark.parametrize(
    ("fixture", "framework", "output"),
    (
        ("astro-blog", "astro", "dist"),
        ("vite-vanilla", "vite", "dist"),
        ("eleventy-blog", "eleventy", "_site"),
        ("docusaurus-docs", "docusaurus", "build"),
        ("vitepress-docs", "vitepress", "docs/.vitepress/dist"),
        ("vuepress-docs", "vuepress", "docs/.vuepress/dist"),
        ("gatsby-blog", "gatsby", "public"),
        ("hugo-quickstart", "hugo", "public"),
    ),
)
def test_production_fixtures_have_execution_blocking_compatible_plans(
    fixture: str,
    framework: str,
    output: str,
) -> None:
    plan = plan_build(FIXTURES / fixture)

    assert plan.framework_id == framework
    assert plan.output_dir == output
    assert plan.compatibility.compatible


@pytest.mark.parametrize(
    "fixture",
    ("nextjs-export", "nuxt-generate", "sveltekit-static", "generic-static"),
)
def test_deferred_fixture_cannot_fall_back_to_generic(fixture: str) -> None:
    with pytest.raises(DetectionError):
        plan_build(FIXTURES / fixture)


def test_assignment_persistence_does_not_store_source_urls_or_secrets(tmp_path: Path) -> None:
    store = SQLiteQueueStore(tmp_path / "queue.sqlite")
    payload = {
        "build_job_id": "11111111-1111-1111-1111-111111111111",
        "attempt_id": "22222222-2222-2222-2222-222222222222",
        "site_id": "site-a",
        "source_download_url": "https://storage.example/one-shot",
        "secret_env": {"TOKEN": "must-not-persist"},
        "framework_id": "vite",
    }

    job = store.enqueue(payload).job
    store.close()
    reopened = SQLiteQueueStore(tmp_path / "queue.sqlite")
    persisted = reopened.get_job(job.build_job_id, job.attempt_id)

    assert persisted is not None
    assert "source_download_url" not in persisted.payload
    assert "secret_env" not in persisted.payload

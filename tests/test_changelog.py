"""Changelog guardrails."""

import re
import tomllib
from pathlib import Path

from build_engine import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_current_project_version_has_changelog_entry() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = pyproject["project"]["version"]
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    heading = re.compile(rf"^## \[{re.escape(version)}\](?: - \d{{4}}-\d{{2}}-\d{{2}})?$", re.M)
    assert heading.search(changelog), (
        f"pyproject.toml version {version!r} is missing a matching CHANGELOG.md release entry"
    )


def test_package_version_comes_from_project_metadata() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert __version__ == pyproject["project"]["version"]

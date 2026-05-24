#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:-${VERSION:-}}"

if [[ -z "$VERSION" ]]; then
  echo "usage: make release VERSION=x.y.z" >&2
  exit 2
fi

if [[ "$VERSION" == v* ]]; then
  echo "VERSION must not include the leading v: use ${VERSION#v}" >&2
  exit 2
fi

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]]; then
  echo "VERSION must look like x.y.z or x.y.z-prerelease" >&2
  exit 2
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "release preparation requires a clean worktree" >&2
  exit 1
fi

if git rev-parse "v${VERSION}" >/dev/null 2>&1; then
  echo "tag v${VERSION} already exists" >&2
  exit 1
fi

uv run python - "$VERSION" <<'PY'
from __future__ import annotations

import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

version = sys.argv[1]
root = Path.cwd()

pyproject = root / "pyproject.toml"
pyproject_text = pyproject.read_text(encoding="utf-8")
updated_pyproject, count = re.subn(
    r'(?m)^version = "[^"]+"$',
    f'version = "{version}"',
    pyproject_text,
    count=1,
)
if count != 1:
    raise SystemExit("could not update project.version in pyproject.toml")
pyproject.write_text(updated_pyproject, encoding="utf-8")

changelog = root / "CHANGELOG.md"
changelog_text = changelog.read_text(encoding="utf-8")
if re.search(rf"(?m)^## \[{re.escape(version)}\](?: - \d{{4}}-\d{{2}}-\d{{2}})?$", changelog_text):
    raise SystemExit(f"CHANGELOG.md already has a {version} section")

unreleased_match = re.search(
    r"(?ms)^## \[Unreleased\]\n\n(?P<body>.*?)(?=^## \[)",
    changelog_text,
)
if unreleased_match is None:
    raise SystemExit("could not find [Unreleased] section in CHANGELOG.md")

unreleased_body = unreleased_match.group("body").strip()
if not unreleased_body:
    raise SystemExit("CHANGELOG.md [Unreleased] section is empty")

today = dt.date.today().isoformat()
release_section = f"## [{version}] - {today}\n\n{unreleased_body}\n\n"
changelog_text = (
    changelog_text[: unreleased_match.start()]
    + "## [Unreleased]\n\n"
    + release_section
    + changelog_text[unreleased_match.end() :]
)

unreleased_link = re.compile(
    r"^\[Unreleased\]: "
    r"(?P<base>https://github\.com/mincemeat-id/build-engine/compare/)"
    r"v(?P<previous>.+)\.\.\.HEAD$",
    re.M,
)
previous_match = unreleased_link.search(changelog_text)
if previous_match is None:
    raise SystemExit("could not find [Unreleased] comparison link in CHANGELOG.md")

previous = previous_match.group("previous")
changelog_text = unreleased_link.sub(
    rf"[Unreleased]: \g<base>v{version}...HEAD",
    changelog_text,
    count=1,
)
previous_tag_exists = (
    subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"v{previous}"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode
    == 0
)
if previous_tag_exists:
    release_link = (
        f"[{version}]: https://github.com/mincemeat-id/build-engine/compare/"
        f"v{previous}...v{version}\n"
    )
else:
    release_link = f"[{version}]: https://github.com/mincemeat-id/build-engine/releases/tag/v{version}\n"
changelog_text = changelog_text.replace(f"[{previous}]:", f"{release_link}[{previous}]:", 1)
changelog.write_text(changelog_text, encoding="utf-8")
PY

uv lock

git add pyproject.toml uv.lock CHANGELOG.md
git commit -m "chore(release): v${VERSION}"
git tag -a "v${VERSION}" -m "Build Engine v${VERSION}"
git push --follow-tags

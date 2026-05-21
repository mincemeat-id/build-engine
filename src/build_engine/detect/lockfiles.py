"""Package-manager and lockfile detection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from build_engine.detect.package_json import PackageJson, load_package_json

type PackageManager = Literal["npm", "pnpm", "yarn", "bun"]

PACKAGE_MANAGERS: frozenset[PackageManager] = frozenset(("npm", "pnpm", "yarn", "bun"))
LOCKFILE_PRIORITY: tuple[tuple[str, PackageManager], ...] = (
    ("bun.lockb", "bun"),
    ("bun.lock", "bun"),
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("package-lock.json", "npm"),
    ("npm-shrinkwrap.json", "npm"),
)


class PackageManagerDetectionError(ValueError):
    """Raised when package-manager metadata is explicit but unsupported."""


@dataclass(frozen=True, slots=True)
class PackageManagerDetection:
    """Resolved package-manager decision and the evidence used."""

    manager: PackageManager
    source: str
    version: str | None = None
    lockfile: Path | None = None


def detect_package_manager(
    root: Path | str,
    package_json: PackageJson | None = None,
) -> PackageManagerDetection:
    """Detect the package manager using packageManager, lockfiles, then npm."""

    project_root = Path(root)
    package_json = package_json if package_json is not None else load_package_json(project_root)
    if package_json is not None and package_json.package_manager:
        manager, version = parse_package_manager(package_json.package_manager)
        return PackageManagerDetection(
            manager=manager,
            source="packageManager",
            version=version,
        )

    for filename, manager in LOCKFILE_PRIORITY:
        path = project_root / filename
        if path.exists():
            return PackageManagerDetection(manager=manager, source="lockfile", lockfile=path)

    return PackageManagerDetection(manager="npm", source="fallback")


def parse_package_manager(value: str) -> tuple[PackageManager, str | None]:
    """Parse packageManager values such as pnpm@9.12.0."""

    name, separator, version = value.partition("@")
    if not name:
        raise PackageManagerDetectionError("packageManager must name npm, pnpm, yarn, or bun")
    if name not in PACKAGE_MANAGERS:
        supported = ", ".join(sorted(PACKAGE_MANAGERS))
        raise PackageManagerDetectionError(
            f"Unsupported packageManager {name!r}; supported managers: {supported}",
        )
    return cast("PackageManager", name), version if separator and version else None


def install_command(
    manager: PackageManager,
    *,
    root: Path | str,
    detection: PackageManagerDetection | None = None,
) -> str:
    """Return the standardized install command for a package manager."""

    project_root = Path(root)
    match manager:
        case "npm":
            return "npm ci" if _has_npm_lock(project_root) else "npm install"
        case "pnpm":
            return "pnpm install --frozen-lockfile"
        case "yarn":
            if _is_yarn_berry(project_root, detection):
                return "yarn install --immutable"
            return "yarn install --frozen-lockfile"
        case "bun":
            return "bun install --frozen-lockfile"


def run_script_command(manager: PackageManager, script_name: str) -> str:
    """Return the command for invoking a package script."""

    match manager:
        case "npm":
            return f"npm run {script_name}"
        case "pnpm":
            return f"pnpm run {script_name}"
        case "yarn":
            return f"yarn {script_name}"
        case "bun":
            return f"bun run {script_name}"


def _has_npm_lock(root: Path) -> bool:
    return (root / "package-lock.json").exists() or (root / "npm-shrinkwrap.json").exists()


def _is_yarn_berry(root: Path, detection: PackageManagerDetection | None) -> bool:
    if (root / ".yarnrc.yml").exists():
        return True
    if detection is None or detection.version is None:
        return False
    major_text = detection.version.split(".", 1)[0]
    return major_text.isdigit() and int(major_text) >= 2

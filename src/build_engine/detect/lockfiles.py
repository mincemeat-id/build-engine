"""Package-manager and lockfile detection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from build_engine.detect.package_json import PackageJson, load_package_json

type PackageManager = Literal["npm", "pnpm", "none"]

PACKAGE_MANAGERS: frozenset[PackageManager] = frozenset(("npm", "pnpm"))
LOCKFILE_PRIORITY: tuple[tuple[str, PackageManager], ...] = (
    ("pnpm-lock.yaml", "pnpm"),
    ("package-lock.json", "npm"),
    ("npm-shrinkwrap.json", "npm"),
)
UNSUPPORTED_LOCKFILES = ("bun.lockb", "bun.lock", "yarn.lock")


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

    for filename in UNSUPPORTED_LOCKFILES:
        if (project_root / filename).exists():
            raise PackageManagerDetectionError(
                f"Unsupported package manager lockfile {filename!r}; use npm or pnpm"
            )

    return PackageManagerDetection(manager="npm", source="fallback")


def parse_package_manager(value: str) -> tuple[PackageManager, str | None]:
    """Parse packageManager values such as pnpm@9.12.0."""

    name, separator, version = value.partition("@")
    if not name:
        raise PackageManagerDetectionError("packageManager must name npm or pnpm")
    parsed_version = version if separator and version else None
    match name:
        case "npm":
            return "npm", parsed_version
        case "pnpm":
            return "pnpm", parsed_version
        case _:
            supported = ", ".join(sorted(PACKAGE_MANAGERS))
            raise PackageManagerDetectionError(
                f"Unsupported packageManager {name!r}; supported managers: {supported}",
            )


def install_command(
    manager: PackageManager,
    *,
    root: Path | str,
    detection: PackageManagerDetection | None = None,
) -> str:
    """Return the standardized install command for a package manager."""

    project_root = Path(root)
    match manager:
        case "none":
            return ""
        case "npm":
            return "npm ci" if _has_npm_lock(project_root) else "npm install"
        case "pnpm":
            return "pnpm install --frozen-lockfile"


def run_script_command(manager: PackageManager, script_name: str) -> str:
    """Return the command for invoking a package script."""

    match manager:
        case "none":
            raise PackageManagerDetectionError(
                "The selected profile does not use a package manager"
            )
        case "npm":
            return f"npm run {script_name}"
        case "pnpm":
            return f"pnpm run {script_name}"


def _has_npm_lock(root: Path) -> bool:
    return (root / "package-lock.json").exists() or (root / "npm-shrinkwrap.json").exists()

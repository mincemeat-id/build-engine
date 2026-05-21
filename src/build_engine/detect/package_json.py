"""package.json parsing helpers for project detection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast


class PackageJsonError(ValueError):
    """Raised when a package.json file exists but cannot be used."""


@dataclass(frozen=True, slots=True)
class PackageJson:
    """Typed view of the package.json fields used by build planning."""

    path: Path
    raw: dict[str, Any]
    scripts: dict[str, str]
    dependencies: dict[str, str]
    dev_dependencies: dict[str, str]
    optional_dependencies: dict[str, str]
    peer_dependencies: dict[str, str]
    package_manager: str | None
    engines_node: str | None

    @property
    def all_dependencies(self) -> dict[str, str]:
        """Return every dependency map merged with normal deps taking precedence."""

        merged: dict[str, str] = {}
        merged.update(self.peer_dependencies)
        merged.update(self.optional_dependencies)
        merged.update(self.dev_dependencies)
        merged.update(self.dependencies)
        return merged

    def has_dependency(self, *names: str) -> bool:
        """Return true when any dependency name is declared."""

        dependencies = self.all_dependencies
        return any(name in dependencies for name in names)

    def script(self, name: str) -> str | None:
        """Return a script command if present and non-empty."""

        value = self.scripts.get(name)
        return value if value else None


def load_package_json(root: Path | str) -> PackageJson | None:
    """Load package.json from a project root, returning None when absent."""

    path = Path(root) / "package.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise PackageJsonError(f"{path} is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise PackageJsonError(f"{path} must contain a JSON object")
    raw = cast("dict[str, Any]", raw)
    return PackageJson(
        path=path,
        raw=raw,
        scripts=_string_map(raw.get("scripts"), "scripts", path),
        dependencies=_string_map(raw.get("dependencies"), "dependencies", path),
        dev_dependencies=_string_map(raw.get("devDependencies"), "devDependencies", path),
        optional_dependencies=_string_map(
            raw.get("optionalDependencies"),
            "optionalDependencies",
            path,
        ),
        peer_dependencies=_string_map(raw.get("peerDependencies"), "peerDependencies", path),
        package_manager=_optional_string(raw.get("packageManager"), "packageManager", path),
        engines_node=_engine_node(raw.get("engines"), path),
    )


def _string_map(value: object, field: str, path: Path) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PackageJsonError(f"{path} field {field} must be an object")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise PackageJsonError(f"{path} field {field} must map strings to strings")
        result[key] = item
    return result


def _optional_string(value: object, field: str, path: Path) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PackageJsonError(f"{path} field {field} must be a string")
    return value


def _engine_node(value: object, path: Path) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise PackageJsonError(f"{path} field engines must be an object")
    engines = cast("dict[str, object]", value)
    node = engines.get("node")
    if node is None:
        return None
    if not isinstance(node, str):
        raise PackageJsonError(f"{path} field engines.node must be a string")
    return node

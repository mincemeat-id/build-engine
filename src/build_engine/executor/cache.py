"""Per-site package-manager cache mount mapping."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from build_engine.detect.lockfiles import PackageManager
from build_engine.executor.docker_runner import CacheMount


class CacheError(RuntimeError):
    """Raised when local build cache operations fail."""


@dataclass(frozen=True, slots=True)
class SiteCache:
    """Resolved local cache paths for a site."""

    root: Path
    package_manager: PackageManager

    def mounts(self) -> tuple[CacheMount, ...]:
        """Return Docker mounts for the package manager."""

        self.root.mkdir(parents=True, exist_ok=True)
        match self.package_manager:
            case "npm":
                host = self.root / "npm" / "_cacache"
                container = "/home/node/.npm/_cacache"
            case "pnpm":
                host = self.root / "pnpm" / "store"
                container = "/home/node/.local/share/pnpm/store"
            case "yarn":
                host = self.root / "yarn" / "cache"
                container = "/home/node/.cache/yarn"
            case "bun":
                host = self.root / "bun" / "install-cache"
                container = "/home/node/.bun/install/cache"
        host.mkdir(parents=True, exist_ok=True)
        return (CacheMount(host_path=host, container_path=container),)


def site_cache(state_dir: Path | str, site_id: str, package_manager: PackageManager) -> SiteCache:
    """Resolve the cache root for a site and package manager."""

    return SiteCache(root=Path(state_dir) / "cache" / site_id, package_manager=package_manager)


def reset_cache(state_dir: Path | str, *, site_id: str | None = None) -> None:
    """Delete one site's cache, or all local build caches."""

    cache_root = Path(state_dir) / "cache"
    target = cache_root / site_id if site_id is not None else cache_root
    if not target.exists():
        return
    if not target.is_dir():
        raise CacheError(f"Cache path is not a directory: {target}")
    shutil.rmtree(target)

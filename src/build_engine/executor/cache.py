"""Per-site package-manager cache mount lifecycle."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from build_engine.detect.lockfiles import LOCKFILE_PRIORITY, PackageManager
from build_engine.executor.docker_runner import CacheMount


class CacheError(RuntimeError):
    """Raised when local build cache operations fail."""


type CacheEvent = Literal["HIT", "MISS", "WIPED"]

METADATA_FILENAME = ".build-engine-cache.json"
DISABLED_MARKER_FILENAME = ".build-engine-cache-disabled"


@dataclass(frozen=True, slots=True)
class LockfileSnapshot:
    """Lockfile identity used to invalidate stale per-site cache data."""

    name: str
    sha256: str


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

    @property
    def metadata_path(self) -> Path:
        """Return the cache metadata path for this site."""

        return self.root / METADATA_FILENAME

    @property
    def disabled_marker_path(self) -> Path:
        """Return the marker written while site cache is disabled."""

        return self.root / DISABLED_MARKER_FILENAME


@dataclass(frozen=True, slots=True)
class CachePrepareResult:
    """Cache state prepared for one build attempt."""

    mounts: tuple[CacheMount, ...]
    event: CacheEvent | None
    lockfile: LockfileSnapshot | None

    @property
    def enabled(self) -> bool:
        """Return whether the build should receive cache mounts."""

        return bool(self.mounts)


def site_cache(state_dir: Path | str, site_id: str, package_manager: PackageManager) -> SiteCache:
    """Resolve the cache root for a site and package manager."""

    return SiteCache(root=Path(state_dir) / "cache" / site_id, package_manager=package_manager)


def prepare_site_cache(
    *,
    state_dir: Path | str,
    site_id: str,
    package_manager: PackageManager,
    project_root: Path | str,
    enabled: bool,
) -> CachePrepareResult:
    """Prepare per-site package-manager cache mounts for one build."""

    cache = site_cache(state_dir, site_id, package_manager)
    if not enabled:
        cache.root.mkdir(parents=True, exist_ok=True)
        cache.disabled_marker_path.write_text(_utcnow(), encoding="utf-8")
        return CachePrepareResult(mounts=(), event=None, lockfile=None)

    lockfile = lockfile_snapshot(project_root, package_manager)
    wiped = False
    if cache.disabled_marker_path.exists():
        reset_cache(state_dir, site_id=site_id)
        wiped = True

    previous = _read_metadata(cache.metadata_path)
    if previous is not None and _metadata_lockfile(previous) != lockfile:
        reset_cache(state_dir, site_id=site_id)
        wiped = True

    had_contents = _has_cache_contents(cache.root)
    mounts = cache.mounts()
    _write_metadata(cache, lockfile)
    touch_site_cache(cache.root)
    if wiped:
        return CachePrepareResult(mounts=mounts, event="WIPED", lockfile=lockfile)
    return CachePrepareResult(
        mounts=mounts,
        event="HIT" if previous is not None and had_contents else "MISS",
        lockfile=lockfile,
    )


def lockfile_snapshot(
    project_root: Path | str,
    package_manager: PackageManager,
) -> LockfileSnapshot | None:
    """Return the sha256 snapshot for the selected package manager lockfile."""

    root = Path(project_root)
    for filename, manager in LOCKFILE_PRIORITY:
        if manager != package_manager:
            continue
        path = root / filename
        if path.is_file():
            return LockfileSnapshot(name=filename, sha256=_sha256_file(path))
    return None


def touch_site_cache(root: Path | str) -> None:
    """Update a site cache's last-access marker for LRU pruning."""

    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    for metadata in (path / METADATA_FILENAME,):
        if metadata.exists():
            metadata.touch()


def cache_size_bytes(state_dir: Path | str) -> int:
    """Return total on-disk bytes used by all build caches."""

    return directory_size_bytes(Path(state_dir) / "cache")


def directory_size_bytes(root: Path | str) -> int:
    """Return recursive file size for a directory tree."""

    path = Path(root)
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def prune_cache(
    state_dir: Path | str,
    *,
    site_max_bytes: int,
    ttl_days: int,
) -> list[Path]:
    """Prune expired and over-limit per-site caches using LRU order."""

    cache_root = Path(state_dir) / "cache"
    if not cache_root.exists():
        return []
    pruned: list[Path] = []
    expires_before = datetime.now(UTC) - timedelta(days=ttl_days)
    for site_root in _site_roots(cache_root):
        if _last_access(site_root) < expires_before:
            _remove_site_root(site_root)
            pruned.append(site_root)

    for site_root in _site_roots(cache_root):
        if directory_size_bytes(site_root) <= site_max_bytes:
            continue
        _prune_children_lru(site_root, max_bytes=site_max_bytes)
        if directory_size_bytes(site_root) > site_max_bytes:
            _remove_site_root(site_root)
            pruned.append(site_root)
    return pruned


def reset_cache(state_dir: Path | str, *, site_id: str | None = None) -> None:
    """Delete one site's cache, or all local build caches."""

    cache_root = Path(state_dir) / "cache"
    target = cache_root / site_id if site_id is not None else cache_root
    if not target.exists():
        return
    if not target.is_dir():
        raise CacheError(f"Cache path is not a directory: {target}")
    shutil.rmtree(target)


def _site_roots(cache_root: Path) -> tuple[Path, ...]:
    return tuple(path for path in sorted(cache_root.iterdir()) if path.is_dir())


def _prune_children_lru(site_root: Path, *, max_bytes: int) -> None:
    candidates = [
        path
        for path in site_root.rglob("*")
        if path.is_file() and path.name not in {METADATA_FILENAME, DISABLED_MARKER_FILENAME}
    ]
    candidates.sort(key=_file_atime)
    for path in candidates:
        if directory_size_bytes(site_root) <= max_bytes:
            return
        path.unlink(missing_ok=True)
    _remove_empty_dirs(site_root)


def _remove_empty_dirs(root: Path) -> None:
    for path in sorted((item for item in root.rglob("*") if item.is_dir()), reverse=True):
        try:
            path.rmdir()
        except OSError:
            continue


def _remove_site_root(site_root: Path) -> None:
    if not site_root.is_dir():
        raise CacheError(f"Cache path is not a directory: {site_root}")
    shutil.rmtree(site_root)


def _has_cache_contents(root: Path) -> bool:
    if not root.exists():
        return False
    ignored = {METADATA_FILENAME, DISABLED_MARKER_FILENAME}
    return any(path.is_file() and path.name not in ignored for path in root.rglob("*"))


def _read_metadata(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CacheError(f"Cache metadata is invalid: {path}") from exc
    if not isinstance(decoded, dict):
        raise CacheError(f"Cache metadata is invalid: {path}")
    return decoded


def _write_metadata(cache: SiteCache, lockfile: LockfileSnapshot | None) -> None:
    cache.root.mkdir(parents=True, exist_ok=True)
    payload = {
        "package_manager": cache.package_manager,
        "lockfile": (
            {"name": lockfile.name, "sha256": lockfile.sha256} if lockfile is not None else None
        ),
        "updated_at": _utcnow(),
    }
    cache.metadata_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _metadata_lockfile(metadata: dict[str, object]) -> LockfileSnapshot | None:
    raw_lockfile = metadata.get("lockfile")
    if raw_lockfile is None:
        return None
    if not isinstance(raw_lockfile, Mapping):
        raise CacheError("Cache metadata lockfile field is invalid")
    lockfile = cast("Mapping[str, object]", raw_lockfile)
    name = lockfile.get("name")
    sha256 = lockfile.get("sha256")
    if not isinstance(name, str) or not isinstance(sha256, str):
        raise CacheError("Cache metadata lockfile field is invalid")
    return LockfileSnapshot(name=name, sha256=sha256)


def _last_access(site_root: Path) -> datetime:
    path = site_root / METADATA_FILENAME
    stat_path = path if path.exists() else site_root
    return datetime.fromtimestamp(stat_path.stat().st_mtime, tz=UTC)


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _file_atime(path: Path) -> float:
    try:
        return path.stat().st_atime
    except OSError:
        return 0.0


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

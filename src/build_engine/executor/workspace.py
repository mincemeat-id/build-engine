"""Workspace setup, source download, and safe extraction."""

from __future__ import annotations

import hashlib
import re
import shutil
import tarfile
from dataclasses import dataclass
from http.client import HTTPMessage
from pathlib import Path
from types import TracebackType
from typing import IO, Protocol, cast
from urllib import error, request
from urllib.parse import urljoin, urlparse
from uuid import UUID


class WorkspaceError(RuntimeError):
    """Raised when source material cannot be prepared safely."""


class SourceResponse(Protocol):
    """Readable context manager returned for local or remote source bytes."""

    def __enter__(self) -> SourceResponse:
        """Enter the response context."""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        """Exit the response context."""

    def read(self, size: int = -1, /) -> bytes:
        """Read source bytes."""


@dataclass(frozen=True, slots=True)
class WorkspacePaths:
    """Filesystem layout for one build attempt."""

    attempt_dir: Path
    source_archive: Path
    source_root: Path
    artifact_dir: Path
    output_root: Path
    cache_root: Path
    tmp_root: Path


def create_workspace(state_dir: Path | str, attempt_id: str) -> WorkspacePaths:
    """Create a fresh workspace for an attempt."""

    _validate_path_component(attempt_id, "attempt_id")
    root = Path(state_dir) / "workspaces" / attempt_id
    if root.exists():
        shutil.rmtree(root)
    source_root = root / "src"
    output_root = root / "out"
    cache_root = root / "cache"
    tmp_root = root / "tmp"
    for path in (source_root, output_root, cache_root, tmp_root):
        path.mkdir(parents=True, exist_ok=True)
    return WorkspacePaths(
        attempt_dir=root,
        source_archive=tmp_root / "source.tar.gz",
        source_root=source_root,
        artifact_dir=tmp_root,
        output_root=output_root,
        cache_root=cache_root,
        tmp_root=tmp_root,
    )


def download_source(
    source_url: str,
    destination: Path | str,
    *,
    expected_sha256: str,
    expected_size: int | None = None,
    max_bytes: int | None = None,
    timeout_seconds: float = 60.0,
    allowed_origins: tuple[str, ...] = (),
    allow_file: bool = False,
) -> Path:
    """Download a source archive and verify its SHA256 while streaming."""

    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise WorkspaceError("expected_sha256 must be a 64-character hexadecimal digest")
    if expected_size is not None and expected_size < 0:
        raise WorkspaceError("expected_size must not be negative")
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination_path.with_suffix(f"{destination_path.suffix}.tmp")
    hasher = hashlib.sha256()
    total = 0
    try:
        with (
            _open_source(
                source_url,
                timeout_seconds=timeout_seconds,
                allowed_origins=allowed_origins,
                allow_file=allow_file,
            ) as response,
            temp_path.open("wb") as output,
        ):
            content_length = _content_length(response)
            if (
                expected_size is not None
                and content_length is not None
                and content_length != expected_size
            ):
                raise WorkspaceError("Source archive size does not match the assignment")
            if max_bytes is not None and expected_size is not None and expected_size > max_bytes:
                raise WorkspaceError("Source archive exceeds configured maximum size")
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise WorkspaceError("Source archive exceeds configured maximum size")
                hasher.update(chunk)
                output.write(chunk)
    except error.URLError as exc:
        raise WorkspaceError(f"Source download failed: {exc.reason}") from exc
    except OSError as exc:
        raise WorkspaceError(f"Source download failed: {exc}") from exc

    actual_sha256 = hasher.hexdigest()
    if actual_sha256.lower() != expected_sha256.lower():
        temp_path.unlink(missing_ok=True)
        raise WorkspaceError("Source SHA256 verification failed")
    temp_path.replace(destination_path)
    return destination_path


def extract_source(archive_path: Path | str, destination: Path | str) -> None:
    """Safely extract a tar archive into `destination`."""

    archive = Path(archive_path)
    destination_path = Path(destination)
    destination_path.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, mode="r:*") as tar:
            members = tar.getmembers()
            for member in members:
                _validate_member(destination_path, member)
            tar.extractall(destination_path, members=members, filter="data")
    except tarfile.TarError as exc:
        raise WorkspaceError(f"Source archive is not a valid tar file: {exc}") from exc
    except OSError as exc:
        raise WorkspaceError(f"Source extraction failed: {exc}") from exc


def resolve_project_root(source_root: Path | str, root_directory: str | None) -> Path:
    """Resolve and validate the project root inside the extracted source."""

    source_root_path = Path(source_root).resolve()
    requested = root_directory or "."
    if Path(requested).is_absolute():
        raise WorkspaceError("root_directory must be relative")
    if ".." in Path(requested).parts:
        raise WorkspaceError("root_directory must not contain traversal components")
    project_root = (source_root_path / requested).resolve()
    if not _is_relative_to(project_root, source_root_path):
        raise WorkspaceError("root_directory escapes the extracted source")
    if not project_root.is_dir():
        raise WorkspaceError("root_directory does not exist in source archive")
    return project_root


def cleanup_workspace(
    paths: WorkspacePaths,
    *,
    retain_failed: bool = False,
    failed_keep: int = 5,
) -> None:
    """Remove a workspace, or retain and prune failed workspaces for debugging."""

    if retain_failed:
        marker = paths.attempt_dir / "FAILED"
        marker.touch(exist_ok=True)
        _prune_failed_workspaces(paths.attempt_dir.parent, keep=failed_keep)
        return
    shutil.rmtree(paths.attempt_dir, ignore_errors=True)


def _open_source(
    source_url: str,
    *,
    timeout_seconds: float,
    allowed_origins: tuple[str, ...] = (),
    allow_file: bool = False,
) -> SourceResponse:
    parsed = urlparse(source_url)
    if parsed.scheme == "file":
        if not allow_file:
            raise WorkspaceError("source_download_url must use HTTPS")
        return Path(request.url2pathname(parsed.path)).open("rb")
    if parsed.scheme != "https":
        raise WorkspaceError("source_download_url must use HTTPS")
    if not parsed.netloc:
        raise WorkspaceError("source_download_url must include a host")
    if allowed_origins and parsed.netloc not in {
        urlparse(origin).netloc for origin in allowed_origins if urlparse(origin).netloc
    }:
        raise WorkspaceError("source_download_url host is not an allowed storage origin")
    req = request.Request(
        source_url,
        headers={"Accept": "application/gzip, application/x-tar, */*"},
    )
    opener = request.build_opener(_SafeRedirectHandler(source_url, allowed_origins))
    return cast("SourceResponse", opener.open(req, timeout=timeout_seconds))  # nosec B310


class _SafeRedirectHandler(request.HTTPRedirectHandler):
    """Allow only HTTPS redirects that stay within an approved storage origin."""

    def __init__(self, source_url: str, allowed_origins: tuple[str, ...]) -> None:
        self.source_origin = _origin(source_url)
        self.allowed_origins = {_origin(value) for value in allowed_origins}

    def redirect_request(
        self,
        req: request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> request.Request | None:
        target = urljoin(req.full_url, newurl)
        parsed = urlparse(target)
        if parsed.scheme != "https" or not parsed.netloc:
            raise WorkspaceError("Source redirect must use HTTPS")
        target_origin = _origin(target)
        permitted = self.allowed_origins or {self.source_origin}
        if target_origin not in permitted:
            raise WorkspaceError("Source redirect leaves the approved storage origin")
        return super().redirect_request(req, fp, code, msg, headers, target)


def _origin(value: str) -> str:
    parsed = urlparse(value)
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _content_length(response: SourceResponse) -> int | None:
    headers = getattr(response, "headers", None)
    raw = headers.get("Content-Length") if headers is not None else None
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise WorkspaceError("Source response has an invalid Content-Length") from exc
    if value < 0:
        raise WorkspaceError("Source response has an invalid Content-Length")
    return value


def _validate_member(destination: Path, member: tarfile.TarInfo) -> None:
    member_path = Path(member.name)
    if member_path.is_absolute() or ".." in member_path.parts:
        raise WorkspaceError("Source archive contains an unsafe path")
    resolved_target = (destination / member_path).resolve()
    if not _is_relative_to(resolved_target, destination.resolve()):
        raise WorkspaceError("Source archive member escapes destination")
    if member.isdev() or member.isfifo():
        raise WorkspaceError("Source archive contains unsupported device entries")
    if member.issym() or member.islnk():
        link_target = Path(member.linkname)
        if link_target.is_absolute():
            raise WorkspaceError("Source archive contains an absolute link target")
        resolved_link = (resolved_target.parent / link_target).resolve()
        if not _is_relative_to(resolved_link, destination.resolve()):
            raise WorkspaceError("Source archive link target escapes destination")


def _prune_failed_workspaces(jobs_dir: Path, *, keep: int) -> None:
    failed = sorted(
        (path for path in jobs_dir.iterdir() if (path / "FAILED").exists()),
        key=lambda path: ((path / "FAILED").stat().st_mtime_ns, path.name),
        reverse=True,
    )
    for old_workspace in failed[keep:]:
        shutil.rmtree(old_workspace, ignore_errors=True)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_path_component(value: str, name: str) -> None:
    """Reject path separators and traversal-like identifiers before deletion."""

    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise WorkspaceError(f"{name} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise WorkspaceError(f"{name} must be a canonical UUID")

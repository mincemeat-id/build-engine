"""Artifact packaging and staging upload helpers."""

from __future__ import annotations

import gzip
import hashlib
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path
from urllib import error, request
from urllib.parse import quote

from build_engine.executor.validate import OutputValidationError, validate_output_dir


class ArtifactError(RuntimeError):
    """Raised when an artifact cannot be packaged or uploaded."""


class ArtifactUploadError(ArtifactError):
    """Raised when coreapp or staging storage rejects an artifact upload."""


@dataclass(frozen=True, slots=True)
class ArtifactPackage:
    """Packaged build output ready for upload."""

    path: Path
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class UploadTicket:
    """Presigned upload destination returned by coreapp."""

    upload_url: str
    expires_at: str
    storage_key: str


def package_output(
    *,
    project_root: Path | str,
    output_dir: str | None,
    destination: Path | str,
    max_bytes: int,
) -> ArtifactPackage:
    """Validate and package static output as a deterministic tar.gz."""

    try:
        snapshot = validate_output_dir(project_root, output_dir, max_bytes=max_bytes)
    except OutputValidationError as exc:
        raise ArtifactError(str(exc)) from exc

    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with (
        destination_path.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        for path in _artifact_members(snapshot.path):
            rel = path.relative_to(snapshot.path).as_posix()
            info = tar.gettarinfo(path, arcname=rel)
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            if path.is_file():
                with path.open("rb") as handle:
                    tar.addfile(info, handle)
            else:
                tar.addfile(info)

    size_bytes = destination_path.stat().st_size
    if size_bytes > max_bytes:
        destination_path.unlink(missing_ok=True)
        raise ArtifactError("Packaged artifact exceeds configured maximum size")
    return ArtifactPackage(
        path=destination_path,
        sha256=_sha256_file(destination_path),
        size_bytes=size_bytes,
    )


class ArtifactUploadClient:
    """Small coreapp/staging-storage client for artifact uploads."""

    def __init__(
        self,
        *,
        backend_url: str,
        session_jwt: str,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.backend_url = backend_url.rstrip("/")
        self.session_jwt = session_jwt
        self.timeout_seconds = timeout_seconds

    def request_upload_url(
        self,
        *,
        build_job_id: str,
        attempt_id: str,
        artifact: ArtifactPackage,
    ) -> UploadTicket:
        """Request a presigned artifact upload URL from coreapp."""

        path = (
            "/api/v1/build-engines/agent/jobs/"
            f"{quote(build_job_id)}/attempts/{quote(attempt_id)}/artifact-upload-url"
        )
        body = json.dumps(
            {"sha256": artifact.sha256, "size_bytes": artifact.size_bytes},
            separators=(",", ":"),
        ).encode("utf-8")
        req = request.Request(
            f"{self.backend_url}{path}",
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.session_jwt}",
                "Content-Type": "application/json",
            },
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:  # nosec B310
                decoded = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ArtifactUploadError(
                f"Artifact upload URL request failed: HTTP {exc.code} {detail}"
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ArtifactUploadError(f"Artifact upload URL request failed: {exc}") from exc
        if not isinstance(decoded, dict):
            raise ArtifactError("Artifact upload URL response was not a JSON object")
        return _upload_ticket_from_payload(decoded)

    def upload(self, ticket: UploadTicket, artifact: ArtifactPackage) -> None:
        """PUT the artifact bytes to a presigned staging-storage URL."""

        req = request.Request(
            ticket.upload_url,
            data=artifact.path.read_bytes(),
            method="PUT",
            headers={
                "Content-Length": str(artifact.size_bytes),
                "Content-Type": "application/gzip",
            },
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:  # nosec B310
                response.read()
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ArtifactUploadError(f"Artifact upload failed: HTTP {exc.code} {detail}") from exc
        except OSError as exc:
            raise ArtifactUploadError(f"Artifact upload failed: {exc}") from exc


def _artifact_members(root: Path) -> list[Path]:
    return sorted(
        (path for path in root.rglob("*") if path.is_file() or path.is_dir() or path.is_symlink()),
        key=lambda path: path.relative_to(root).as_posix(),
    )


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _upload_ticket_from_payload(payload: dict[object, object]) -> UploadTicket:
    upload_url = payload.get("upload_url")
    expires_at = payload.get("expires_at")
    storage_key = payload.get("storage_key")
    if not isinstance(upload_url, str) or not upload_url:
        raise ArtifactError("Artifact upload URL response missing upload_url")
    if not isinstance(expires_at, str) or not expires_at:
        raise ArtifactError("Artifact upload URL response missing expires_at")
    if not isinstance(storage_key, str) or not storage_key:
        raise ArtifactError("Artifact upload URL response missing storage_key")
    return UploadTicket(upload_url=upload_url, expires_at=expires_at, storage_key=storage_key)

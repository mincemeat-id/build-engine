"""Static-site output validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class OutputValidationError(RuntimeError):
    """Raised when a build output directory is not publishable."""


@dataclass(frozen=True, slots=True)
class OutputSnapshot:
    """Validated output metadata."""

    path: Path
    file_count: int
    total_bytes: int


def validate_output_dir(
    project_root: Path | str,
    output_dir: str | None,
    *,
    max_bytes: int,
) -> OutputSnapshot:
    """Validate that `output_dir` is a non-empty directory under `project_root`."""

    if not output_dir:
        raise OutputValidationError("No output_dir was resolved for this build")
    if Path(output_dir).is_absolute():
        raise OutputValidationError("output_dir must be relative")
    project_root_path = Path(project_root).resolve()
    output_path = (project_root_path / output_dir).resolve()
    if not _is_relative_to(output_path, project_root_path):
        raise OutputValidationError("output_dir escapes the project root")
    if not output_path.is_dir():
        raise OutputValidationError("Build output directory does not exist")

    file_count = 0
    total_bytes = 0
    for path in output_path.rglob("*"):
        if path.is_symlink():
            target = path.resolve()
            if not _is_relative_to(target, output_path):
                raise OutputValidationError("Build output contains a symlink escaping output_dir")
            continue
        if not path.is_file():
            continue
        file_count += 1
        total_bytes += path.stat().st_size
        if total_bytes > max_bytes:
            raise OutputValidationError("Build output exceeds artifact size limit")

    if file_count == 0:
        raise OutputValidationError("Build output directory is empty")
    return OutputSnapshot(path=output_path, file_count=file_count, total_bytes=total_bytes)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True

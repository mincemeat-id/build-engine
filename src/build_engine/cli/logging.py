"""Logging setup for CLI and long-running service commands."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import override


class JsonFormatter(logging.Formatter):
    """Format log records as one JSON object per line."""

    @override
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC)
            .isoformat(timespec="milliseconds")
            .replace(
                "+00:00",
                "Z",
            ),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, sort_keys=True)


def configure_logging(*, log_format: str = "human", level: str = "INFO") -> None:
    """Configure root logging for a CLI invocation."""

    handler: logging.Handler
    if log_format == "json":
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
    else:
        handler = _human_handler()

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=[handler],
        force=True,
    )


def _human_handler() -> logging.Handler:
    try:
        from rich.console import Console
        from rich.logging import RichHandler
    except ImportError:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        return handler
    return RichHandler(
        console=Console(stderr=True),
        rich_tracebacks=True,
        show_time=False,
        show_path=False,
    )

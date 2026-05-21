"""Docker stdout/stderr streaming with exact-value redaction."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable

from build_engine.agent.protocol import MAX_LOG_DATA_BYTES

type LogPublisher = Callable[[str, str], Awaitable[None]]


class SecretRedactor:
    """Redact configured exact secret values from log data."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        clean: set[str] = set()
        for secret in secrets:
            if secret:
                clean.add(secret)
        self._secrets = tuple(sorted(clean, key=lambda value: len(value), reverse=True))

    def redact(self, value: str) -> str:
        """Replace every exact secret occurrence with a fixed token."""

        redacted = value
        for secret in self._secrets:
            redacted = redacted.replace(secret, "[REDACTED]")
        return redacted


async def pump_stream(
    reader: asyncio.StreamReader,
    *,
    stream: str,
    redactor: SecretRedactor,
    publish: LogPublisher,
) -> None:
    """Read process output and publish protocol-sized redacted log frames."""

    while chunk := await reader.read(MAX_LOG_DATA_BYTES):
        text = chunk.decode("utf-8", errors="replace")
        for frame in _frame_chunks(redactor.redact(text)):
            await publish(stream, frame)


def _frame_chunks(value: str) -> tuple[str, ...]:
    if len(value.encode("utf-8")) <= MAX_LOG_DATA_BYTES:
        return (value,)
    chunks: list[str] = []
    remaining = value
    while remaining:
        limit = min(len(remaining), MAX_LOG_DATA_BYTES)
        chunk = remaining[:limit]
        while len(chunk.encode("utf-8")) > MAX_LOG_DATA_BYTES:
            limit -= 1
            chunk = remaining[:limit]
        chunks.append(chunk)
        remaining = remaining[limit:]
    return tuple(chunks)

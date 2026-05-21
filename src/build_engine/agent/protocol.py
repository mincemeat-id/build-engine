"""Agent protocol constants."""

PROTOCOL_VERSION = 1

INBOUND_MESSAGE_TYPES = frozenset(
    {"welcome", "job.assign", "cancel", "cache.reset", "drain", "ping"},
)
OUTBOUND_MESSAGE_TYPES = frozenset(
    {
        "hello",
        "job.ack",
        "status",
        "log",
        "metric",
        "artifact.ready",
        "cache.event",
        "error",
        "heartbeat",
        "pong",
    },
)

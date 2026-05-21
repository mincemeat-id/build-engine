"""Configuration defaults for the bootstrap scaffold."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EngineDefaults:
    """Compiled defaults documented in the build-engine design."""

    max_concurrency: int = 2
    heartbeat_interval_seconds: int = 15
    build_timeout_seconds: int = 600
    sigterm_grace_seconds: int = 10
    container_memory: str = "2g"
    container_cpus: float = 1.0
    artifact_max_bytes: int = 524_288_000
    cache_site_max_bytes: int = 5_368_709_120
    cache_ttl_days: int = 30


DEFAULTS = EngineDefaults()

"""Docker network policy for production builders."""

from __future__ import annotations

DEFAULT_NETWORK_MODE = "bridge"


class NetworkPolicyError(ValueError):
    """Raised when a caller requests an unsupported Docker network mode."""


def network_args(mode: str = DEFAULT_NETWORK_MODE) -> list[str]:
    """Return Docker arguments for the selected unrestricted egress network."""

    if mode != DEFAULT_NETWORK_MODE:
        raise NetworkPolicyError("Build containers must use the unrestricted bridge network")
    return ["--network", DEFAULT_NETWORK_MODE]

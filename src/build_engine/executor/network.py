"""Docker network guard setup."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


class NetworkGuardError(RuntimeError):
    """Raised when the Docker network guard cannot be prepared."""


@dataclass(frozen=True, slots=True)
class DockerNetworkGuard:
    """Prepared Docker network for executor containers."""

    name: str = "build-engine-guard"

    def docker_args(self) -> list[str]:
        """Return `docker run` args for the guarded network."""

        return ["--network", self.name]


def ensure_network_guard(
    *,
    docker_bin: str = "docker",
    network_name: str = "build-engine-guard",
) -> DockerNetworkGuard:
    """Ensure the executor network exists, failing closed on Docker errors."""

    inspect = subprocess.run(
        [docker_bin, "network", "inspect", network_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if inspect.returncode == 0:
        return DockerNetworkGuard(name=network_name)

    create = subprocess.run(
        [
            docker_bin,
            "network",
            "create",
            "--driver",
            "bridge",
            "--label",
            "mincemeat.build-engine.network-guard=true",
            network_name,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if create.returncode != 0:
        detail = create.stderr.strip() or "unknown Docker error"
        raise NetworkGuardError(f"Could not create Docker network guard: {detail}")
    return DockerNetworkGuard(name=network_name)

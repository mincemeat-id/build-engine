"""Docker network guard setup."""

from __future__ import annotations

import ipaddress
import json
import shutil
import subprocess  # nosec B404
from dataclasses import dataclass
from typing import Any

DEFAULT_NETWORK_NAME = "build-engine-guard"
DEFAULT_BRIDGE_NAME = "be-guard0"
DEFAULT_SUBNET = "172.31.255.0/24"
DEFAULT_GATEWAY = "172.31.255.1"
GUARD_CHAIN = "BUILD_ENGINE_GUARD"
DEFAULT_BLOCKLIST = (
    "169.254.0.0/16",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.100.100.200/32",
)


class NetworkGuardError(RuntimeError):
    """Raised when the Docker network guard cannot be prepared."""


@dataclass(frozen=True, slots=True)
class DockerNetworkGuard:
    """Prepared Docker network for executor containers."""

    name: str = DEFAULT_NETWORK_NAME
    bridge_name: str = DEFAULT_BRIDGE_NAME
    gateway: str = DEFAULT_GATEWAY
    blocklist: tuple[str, ...] = DEFAULT_BLOCKLIST

    def docker_args(self) -> list[str]:
        """Return `docker run` args for the guarded network."""

        return ["--network", self.name]


def ensure_network_guard(
    *,
    docker_bin: str = "docker",
    iptables_bin: str = "iptables",
    network_name: str = DEFAULT_NETWORK_NAME,
    bridge_name: str = DEFAULT_BRIDGE_NAME,
    subnet: str = DEFAULT_SUBNET,
    gateway: str = DEFAULT_GATEWAY,
    blocklist: tuple[str, ...] = (),
) -> DockerNetworkGuard:
    """Ensure the executor network and egress firewall exist.

    The guarded Docker bridge is allowed public outbound internet, but traffic
    forwarded from that bridge is dropped when its destination is metadata,
    private, Docker-gateway, or operator-provided platform CIDR space.
    """

    details = _ensure_docker_network(
        docker_bin=docker_bin,
        network_name=network_name,
        bridge_name=bridge_name,
        subnet=subnet,
        gateway=gateway,
    )
    deny_list = normalize_blocklist((*DEFAULT_BLOCKLIST, details.gateway, *blocklist))
    _ensure_iptables_guard(
        iptables_bin=iptables_bin,
        bridge_name=details.bridge_name,
        blocklist=deny_list,
    )
    return DockerNetworkGuard(
        name=network_name,
        bridge_name=details.bridge_name,
        gateway=details.gateway,
        blocklist=deny_list,
    )


@dataclass(frozen=True, slots=True)
class _NetworkDetails:
    name: str
    bridge_name: str
    gateway: str


def normalize_blocklist(blocklist: tuple[str, ...]) -> tuple[str, ...]:
    """Return validated, stable IPv4 CIDR strings for firewall rules."""

    normalized: dict[str, None] = {}
    for raw_entry in blocklist:
        entry = raw_entry.strip()
        if not entry:
            continue
        try:
            if "/" in entry:
                network = ipaddress.ip_network(entry, strict=False)
            else:
                address = ipaddress.ip_address(entry)
                network = ipaddress.ip_network(address)
        except ValueError as exc:
            raise NetworkGuardError(f"Invalid network blocklist entry {entry!r}") from exc
        if network.version != 4:
            raise NetworkGuardError(f"IPv6 network blocklist entries are not supported: {entry!r}")
        normalized[str(network)] = None
    return tuple(normalized)


def _ensure_docker_network(
    *,
    docker_bin: str,
    network_name: str,
    bridge_name: str,
    subnet: str,
    gateway: str,
) -> _NetworkDetails:
    inspect = _docker_network_inspect(docker_bin=docker_bin, network_name=network_name)
    if inspect is not None:
        return _network_details(inspect, fallback_bridge_name=bridge_name, fallback_gateway=gateway)

    create = _run(
        [
            docker_bin,
            "network",
            "create",
            "--driver",
            "bridge",
            "--subnet",
            subnet,
            "--gateway",
            gateway,
            "--opt",
            f"com.docker.network.bridge.name={bridge_name}",
            "--label",
            "mincemeat.build-engine.network-guard=true",
            network_name,
        ]
    )
    if create.returncode != 0:
        raise NetworkGuardError(f"Could not create Docker network guard: {_detail(create)}")
    inspect = _docker_network_inspect(docker_bin=docker_bin, network_name=network_name)
    if inspect is None:
        raise NetworkGuardError("Could not inspect Docker network guard after creation")
    return _network_details(inspect, fallback_bridge_name=bridge_name, fallback_gateway=gateway)


def _docker_network_inspect(*, docker_bin: str, network_name: str) -> dict[str, Any] | None:
    inspect = _run([docker_bin, "network", "inspect", network_name])
    if inspect.returncode != 0:
        return None
    try:
        decoded = json.loads(inspect.stdout)
    except json.JSONDecodeError as exc:
        raise NetworkGuardError("Docker network inspect returned invalid JSON") from exc
    if not isinstance(decoded, list) or not decoded or not isinstance(decoded[0], dict):
        raise NetworkGuardError("Docker network inspect returned an unexpected payload")
    return decoded[0]


def _network_details(
    network: dict[str, Any],
    *,
    fallback_bridge_name: str,
    fallback_gateway: str,
) -> _NetworkDetails:
    name = str(network.get("Name") or DEFAULT_NETWORK_NAME)
    options = network.get("Options")
    bridge_name = fallback_bridge_name
    has_configured_bridge = False
    if isinstance(options, dict):
        configured_bridge = options.get("com.docker.network.bridge.name")
        if isinstance(configured_bridge, str) and configured_bridge:
            bridge_name = configured_bridge
            has_configured_bridge = True
    network_id = network.get("Id")
    if not has_configured_bridge and isinstance(network_id, str) and network_id:
        bridge_name = f"br-{network_id[:12]}"

    gateway = fallback_gateway
    ipam = network.get("IPAM")
    if isinstance(ipam, dict) and isinstance(ipam.get("Config"), list):
        for raw_config in ipam["Config"]:
            if isinstance(raw_config, dict) and isinstance(raw_config.get("Gateway"), str):
                gateway = raw_config["Gateway"]
                break
    return _NetworkDetails(name=name, bridge_name=bridge_name, gateway=gateway)


def _ensure_iptables_guard(
    *,
    iptables_bin: str,
    bridge_name: str,
    blocklist: tuple[str, ...],
) -> None:
    if shutil.which(iptables_bin) is None:
        raise NetworkGuardError(f"{iptables_bin} was not found; cannot install network guard")

    chain_create = _run([iptables_bin, "-N", GUARD_CHAIN])
    if chain_create.returncode != 0 and "exists" not in _detail(chain_create).lower():
        raise NetworkGuardError(
            f"Network guard command failed: {iptables_bin} -N {GUARD_CHAIN}: "
            f"{_detail(chain_create)}"
        )
    _ensure_jump(
        iptables_bin=iptables_bin,
        chain="DOCKER-USER",
        rule=("-i", bridge_name, "-j", GUARD_CHAIN),
    )
    for cidr in reversed(blocklist):
        _ensure_drop_rule(iptables_bin=iptables_bin, cidr=cidr)
    _ensure_jump(iptables_bin=iptables_bin, chain=GUARD_CHAIN, rule=("-j", "RETURN"), append=True)


def _ensure_drop_rule(*, iptables_bin: str, cidr: str) -> None:
    rule = ("-d", cidr, "-j", "DROP")
    check = _run([iptables_bin, "-C", GUARD_CHAIN, *rule])
    if check.returncode == 0:
        return
    _run_or_raise([iptables_bin, "-I", GUARD_CHAIN, "1", *rule])


def _ensure_jump(
    *,
    iptables_bin: str,
    chain: str,
    rule: tuple[str, ...],
    append: bool = False,
) -> None:
    check = _run([iptables_bin, "-C", chain, *rule])
    if check.returncode == 0:
        return
    operation = "-A" if append else "-I"
    command = [iptables_bin, operation, chain]
    if not append:
        command.append("1")
    command.extend(rule)
    _run_or_raise(command)


def _run_or_raise(command: list[str]) -> None:
    result = _run(command)
    if result.returncode != 0:
        joined = " ".join(command)
        raise NetworkGuardError(f"Network guard command failed: {joined}: {_detail(result)}")


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # nosec B603
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        joined = " ".join(command)
        raise NetworkGuardError(f"Network guard command failed: {joined}: {exc}") from exc


def _detail(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout).strip() or f"exit {result.returncode}"

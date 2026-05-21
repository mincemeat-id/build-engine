"""Bootstrap CLI commands for the build engine."""

import argparse
import json
from collections.abc import Sequence

from build_engine import __version__
from build_engine.config import DEFAULTS


def main(argv: Sequence[str] | None = None) -> int:
    """Run the build-engine command line interface."""

    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = args.handler
    return handler(args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="build-engine")
    parser.add_argument("--version", action="version", version=f"build-engine {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="run the agent service")
    serve.add_argument("--config", default="/etc/mincemeat/build-engine/config.toml")
    serve.set_defaults(handler=_serve)

    register = subparsers.add_parser("register", help="register this engine with coreapp")
    register.add_argument("--backend-url", required=True)
    register.add_argument("--token", required=True)
    register.add_argument("--name", required=True)
    register.add_argument("--max-concurrency", type=int, default=DEFAULTS.max_concurrency)
    register.set_defaults(handler=_register)

    status = subparsers.add_parser("status", help="show local status")
    status.set_defaults(handler=_status)

    doctor = subparsers.add_parser("doctor", help="run local diagnostics")
    doctor.add_argument("--json", action="store_true", dest="as_json")
    doctor.set_defaults(handler=_doctor)

    cache = subparsers.add_parser("cache", help="local cache operations")
    cache_subparsers = cache.add_subparsers(dest="cache_command")
    cache_reset = cache_subparsers.add_parser("reset", help="reset local build cache")
    cache_reset.add_argument("--site-id")
    cache_reset.set_defaults(handler=_cache_reset)

    drain = subparsers.add_parser("drain", help="request local drain mode")
    drain.set_defaults(handler=_drain)

    parser.set_defaults(handler=_help)
    return parser


def _help(args: argparse.Namespace) -> int:
    del args
    _build_parser().print_help()
    return 0


def _serve(args: argparse.Namespace) -> int:
    print(f"serve scaffold ready; config={args.config}")
    return 0


def _register(args: argparse.Namespace) -> int:
    token_redacted = f"{str(args.token)[:4]}..." if args.token else ""
    print(
        "register scaffold ready; "
        f"backend_url={args.backend_url} name={args.name} "
        f"max_concurrency={args.max_concurrency} token={token_redacted}",
    )
    return 0


def _status(args: argparse.Namespace) -> int:
    del args
    print("build-engine scaffold status: not registered")
    return 0


def _doctor(args: argparse.Namespace) -> int:
    payload = {
        "version": __version__,
        "protocol_version": 1,
        "checks": [],
        "status": "scaffold",
    }
    if args.as_json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print("build-engine doctor scaffold: no runtime checks implemented yet")
    return 0


def _cache_reset(args: argparse.Namespace) -> int:
    scope = args.site_id or "all-sites"
    print(f"cache reset scaffold ready; scope={scope}")
    return 0


def _drain(args: argparse.Namespace) -> int:
    del args
    print("drain scaffold ready")
    return 0

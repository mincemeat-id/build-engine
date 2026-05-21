"""Bootstrap CLI commands for the build engine."""

import argparse
import json
import sys
from collections.abc import Sequence

from build_engine import __version__
from build_engine.agent.auth import (
    AuthError,
    refresh_session,
    register_engine,
    validate_credentials_file,
)
from build_engine.config import DEFAULTS, load_config


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
    serve.add_argument("--credentials", default=None)
    serve.set_defaults(handler=_serve)

    register = subparsers.add_parser("register", help="register this engine with coreapp")
    register.add_argument("--config", default="/etc/mincemeat/build-engine/config.toml")
    register.add_argument("--credentials", default=None)
    register.add_argument("--cert", default=None)
    register.add_argument("--key", default=None)
    register.add_argument("--backend-url", required=True)
    register.add_argument("--token", required=True)
    register.add_argument("--name", required=True)
    register.add_argument("--max-concurrency", type=int, default=DEFAULTS.max_concurrency)
    register.set_defaults(handler=_register)

    status = subparsers.add_parser("status", help="show local status")
    status.add_argument("--config", default="/etc/mincemeat/build-engine/config.toml")
    status.add_argument("--credentials", default=None)
    status.set_defaults(handler=_status)

    doctor = subparsers.add_parser("doctor", help="run local diagnostics")
    doctor.add_argument("--config", default="/etc/mincemeat/build-engine/config.toml")
    doctor.add_argument("--credentials", default=None)
    doctor.add_argument("--json", action="store_true", dest="as_json")
    doctor.set_defaults(handler=_doctor)

    session = subparsers.add_parser("session", help="session operations")
    session_subparsers = session.add_subparsers(dest="session_command")
    session_refresh = session_subparsers.add_parser(
        "refresh",
        help="refresh the backend session JWT",
    )
    session_refresh.add_argument("--config", default="/etc/mincemeat/build-engine/config.toml")
    session_refresh.add_argument("--credentials", default=None)
    session_refresh.add_argument("--backend-url", default=None)
    session_refresh.set_defaults(handler=_session_refresh)

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
    config = load_config(config_path=args.config, credentials_path=args.credentials)
    print(f"serve scaffold ready; config={args.config} max_concurrency={config.max_concurrency}")
    return 0


def _register(args: argparse.Namespace) -> int:
    config = load_config(
        config_path=args.config,
        credentials_path=args.credentials,
        overrides={
            "backend_url": args.backend_url,
            "name": args.name,
            "max_concurrency": args.max_concurrency,
            "cert_path": args.cert,
            "key_path": args.key,
        },
    )
    try:
        result = register_engine(config, registration_token=args.token)
    except AuthError as exc:
        print(f"registration failed: {exc}", file=sys.stderr)
        return 1
    print(
        "registered build engine "
        f"{result.engine_id}; credentials={result.credentials_path} cert={result.cert_path}",
    )
    return 0


def _status(args: argparse.Namespace) -> int:
    config = load_config(config_path=args.config, credentials_path=args.credentials)
    try:
        credentials = validate_credentials_file(config.credentials_path)
    except (AuthError, ValueError, FileNotFoundError) as exc:
        print(f"build-engine status: not registered ({exc})")
        return 1
    print(
        "build-engine status: registered "
        f"engine_id={credentials.engine_id} "
        f"backend={credentials.backend_url or config.backend_url}",
    )
    return 0


def _doctor(args: argparse.Namespace) -> int:
    config = load_config(config_path=args.config, credentials_path=args.credentials)
    checks: list[dict[str, object]] = []
    status = "ok"
    try:
        credentials = validate_credentials_file(config.credentials_path)
    except (AuthError, ValueError, FileNotFoundError) as exc:
        credentials = None
        status = "error"
        checks.append({"name": "credentials", "ok": False, "detail": str(exc)})
    else:
        checks.append({"name": "credentials", "ok": True, "detail": str(config.credentials_path)})
        checks.append({"name": "certificate", "ok": True, "detail": str(credentials.cert_path)})
    payload = {
        "version": __version__,
        "protocol_version": 1,
        "checks": checks,
        "status": status,
    }
    if args.as_json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"build-engine doctor: {status}")
        for check in checks:
            state = "ok" if check["ok"] else "fail"
            print(f"- {check['name']}: {state} ({check['detail']})")
    return 0 if credentials is not None else 1


def _cache_reset(args: argparse.Namespace) -> int:
    scope = args.site_id or "all-sites"
    print(f"cache reset scaffold ready; scope={scope}")
    return 0


def _drain(args: argparse.Namespace) -> int:
    del args
    print("drain scaffold ready")
    return 0


def _session_refresh(args: argparse.Namespace) -> int:
    config = load_config(config_path=args.config, credentials_path=args.credentials)
    try:
        credentials = refresh_session(
            config.credentials_path,
            backend_url=args.backend_url or config.backend_url,
        )
    except (AuthError, ValueError, FileNotFoundError) as exc:
        print(f"session refresh failed: {exc}", file=sys.stderr)
        return 1
    print(
        "session refreshed "
        f"engine_id={credentials.engine_id} expires_at={credentials.session_jwt_expires_at}",
    )
    return 0

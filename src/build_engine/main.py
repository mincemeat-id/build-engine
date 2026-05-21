"""Console entrypoint for the build engine."""

from build_engine.cli.commands import main

__all__ = ["main"]

if __name__ == "__main__":
    raise SystemExit(main())

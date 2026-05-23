"""Mincemeat build engine agent package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("mincemeat-build-engine")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

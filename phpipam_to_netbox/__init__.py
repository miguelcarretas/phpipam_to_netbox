"""Utilities to migrate IP data from phpIPAM to NetBox."""

from importlib.metadata import version, PackageNotFoundError

try:  # pragma: no cover - fallback for local execution without packaging metadata
    __version__ = version("phpipam-to-netbox")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.1.0"

__all__ = ["__version__"]

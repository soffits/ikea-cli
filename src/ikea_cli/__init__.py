"""Unofficial command-line client and Python library for the IKEA website."""

__version__ = "0.1.0"

from .client import IkeaClient
from .config import Settings
from .errors import IkeaError

__all__ = ["IkeaClient", "IkeaError", "Settings", "__version__"]

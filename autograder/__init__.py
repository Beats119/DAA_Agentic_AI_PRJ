"""AutoGrader: agentic evaluation platform for student C programs. (Phase 2.)"""

from importlib import metadata as _metadata

try:
    __version__ = _metadata.version("autograder")
except _metadata.PackageNotFoundError:  # source checkout, not installed
    __version__ = "0.1.0"  # keep in sync with pyproject.toml

__all__ = ["__version__"]

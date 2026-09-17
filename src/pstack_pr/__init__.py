"""pstack-pr: export a linear stack of commits as stacked GitHub pull requests."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("pstack-pr")
except PackageNotFoundError:  # pragma: no cover - only when running from a checkout
    __version__ = "0+unknown"

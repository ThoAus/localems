"""Base interface for modular energy data file readers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import polars as pl


class BaseReader(ABC):
    """Abstract base class for source- and format-specific measurement readers."""

    name: str
    description: str = ""

    @abstractmethod
    def read(self, path: Path | str, **kwargs: Any) -> pl.DataFrame:
        """Read raw measurements from `path` and return a canonical Polars DataFrame."""

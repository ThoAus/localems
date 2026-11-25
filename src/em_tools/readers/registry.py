"""Reader registry for discovering, auto-detecting, and dispatching input format readers."""

from __future__ import annotations

from pathlib import Path

from em_tools.readers.base import BaseReader
from em_tools.readers.csv_standard import StandardCsvReader
from em_tools.readers.edyna import EdynaXlsxReader


class ReaderRegistry:
    """Registry mapping short reader names to BaseReader instances with filename auto-detection."""

    def __init__(self) -> None:
        self._readers: dict[str, BaseReader] = {}

    def register(self, reader: BaseReader) -> None:
        self._readers[reader.name] = reader

    def get(self, name: str) -> BaseReader:
        if name not in self._readers:
            available = ", ".join(sorted(self._readers))
            raise KeyError(f"Unknown reader '{name}'. Available readers: {available}")
        return self._readers[name]

    def detect(self, path: Path | str) -> BaseReader:
        """Auto-detect the unique reader matching `path` by filename & extension."""
        file_path = Path(path)
        matches = [r for r in self._readers.values() if r.can_read(file_path)]
        if not matches:
            available = ", ".join(sorted(self._readers))
            raise LookupError(
                f"No reader automatically matched '{file_path.name}'. "
                f"Specify --reader explicitly (available: {available})."
            )
        if len(matches) > 1:
            names = ", ".join(r.name for r in matches)
            raise ValueError(
                f"Ambiguous filename '{file_path.name}': matched multiple readers ({names})."
            )
        return matches[0]

    def list_readers(self) -> list[BaseReader]:
        return [self._readers[k] for k in sorted(self._readers)]


DEFAULT_REGISTRY = ReaderRegistry()
DEFAULT_REGISTRY.register(StandardCsvReader())
DEFAULT_REGISTRY.register(EdynaXlsxReader())

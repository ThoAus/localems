"""Reader registry for discovering and dispatching input format readers."""

from __future__ import annotations

from em_tools.readers.base import BaseReader
from em_tools.readers.csv_standard import StandardCsvReader
from em_tools.readers.edyna import EdynaXlsxReader


class ReaderRegistry:
    """Registry mapping short reader names to BaseReader instances."""

    def __init__(self) -> None:
        self._readers: dict[str, BaseReader] = {}

    def register(self, reader: BaseReader) -> None:
        self._readers[reader.name] = reader

    def get(self, name: str) -> BaseReader:
        if name not in self._readers:
            available = ", ".join(sorted(self._readers))
            raise KeyError(f"Unknown reader '{name}'. Available readers: {available}")
        return self._readers[name]

    def list_readers(self) -> list[BaseReader]:
        return [self._readers[k] for k in sorted(self._readers)]


DEFAULT_REGISTRY = ReaderRegistry()
DEFAULT_REGISTRY.register(StandardCsvReader())
DEFAULT_REGISTRY.register(EdynaXlsxReader())

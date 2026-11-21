from abc import ABC, abstractmethod
from app.schemas import ParsedStatement


class StatementParser(ABC):
    """A parser receives layout-preserving text, one string per PDF page."""
    name: str

    @abstractmethod
    def detect(self, pages: list[str]) -> float:
        """Confidence 0..1 that this parser understands the layout. 0 means 'not mine'."""

    @abstractmethod
    def parse(self, pages: list[str]) -> ParsedStatement:
        ...

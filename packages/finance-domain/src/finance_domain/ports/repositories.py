from __future__ import annotations

from typing import Protocol


class FilingRepository(Protocol):
    """Application-facing persistence port; implementations may use SQLAlchemy or PostgreSQL."""

    def filing_status(self, filing_id: int) -> str | None: ...


class ObjectStorage(Protocol):
    async def save_pdf(self, filename: str, content: bytes) -> tuple[str, str]: ...


class LlmProvider(Protocol):
    def analyze(self, input_snapshot: dict) -> dict: ...


class OcrProvider(Protocol):
    def recognize(self, image: bytes, *, page_no: int) -> tuple[str, dict]: ...

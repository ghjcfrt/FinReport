from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TableCandidate:
    page_no: int
    rows: list[list[str]]
    extractor: str
    confidence: float
    review_required: bool


def extract_table_candidates(path: Path, page_no: int) -> list[TableCandidate]:
    """Best-effort Camelot adapter; table output remains a review candidate."""
    try:
        import camelot
    except ImportError:
        return []
    try:
        tables = camelot.read_pdf(str(path), pages=str(page_no), flavor="stream")
    except Exception:  # noqa: BLE001 - table parser is an isolated candidate extractor
        return []
    candidates = []
    for table in tables:
        rows = table.df.fillna("").astype(str).values.tolist()
        candidates.append(TableCandidate(page_no, rows, "camelot-stream-v1", 0.4, True))
    return candidates

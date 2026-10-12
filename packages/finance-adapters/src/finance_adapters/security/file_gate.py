"""The shared malware admission gate for API and background extraction."""

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_adapters.config import settings
from finance_adapters.persistence.models import FileScan, FilingVersion
from finance_adapters.storage.object_storage import file_sha256


def scanned_object_path(session: Session, filing: FilingVersion) -> Path:
    scan = session.scalar(
        select(FileScan).where(
            FileScan.file_sha256 == filing.file_sha256,
            FileScan.disposition == "clean",
        ).order_by(FileScan.id.desc())
    )
    if scan is None:
        raise ValueError("FILE_SCAN_REQUIRED")
    expected = settings.storage_dir / "objects" / filing.file_sha256[:2] / f"{filing.file_sha256}.pdf"
    path = Path(filing.object_key)
    try:
        if path.resolve(strict=True) != expected.resolve(strict=True):
            raise ValueError("TRUSTED_OBJECT_REQUIRED")
        if path.stat().st_size != scan.file_size or file_sha256(path) != filing.file_sha256:
            raise ValueError("TRUSTED_OBJECT_HASH_MISMATCH")
    except OSError as exc:
        raise ValueError("TRUSTED_OBJECT_UNAVAILABLE") from exc
    return path

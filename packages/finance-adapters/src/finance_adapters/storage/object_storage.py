from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import UploadFile


async def persist_upload(upload: UploadFile) -> tuple[str, str]:
    raise RuntimeError("DIRECT_OBJECT_PERSIST_DISABLED_USE_SCANNER")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

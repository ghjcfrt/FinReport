from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from fastapi import UploadFile
from finance_adapters.config import settings
from finance_adapters.security.scanner import LocalClamScanner, ScanDisposition
from starlette.datastructures import Headers


def upload(content: bytes, name: str = "report.pdf") -> UploadFile:
    return UploadFile(BytesIO(content), filename=name, headers=Headers({"content-type": "application/pdf"}))


@pytest.mark.asyncio
async def test_mock_scanner_promotes_only_after_hash_verification(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr(settings, "av_mode", "mock")
    scanner = LocalClamScanner()

    result = await scanner.register_and_enqueue(upload(b"%PDF-1.7\nsafe pdf bytes"), original_filename="..\\report.pdf")

    assert result.disposition is ScanDisposition.CLEAN
    assert result.object_id == result.sha256
    assert result.original_filename == "report.pdf"
    assert (tmp_path / "objects" / result.sha256[:2] / f"{result.sha256}.pdf").read_bytes() == b"%PDF-1.7\nsafe pdf bytes"
    assert not list((tmp_path / "quarantine" / "managed").iterdir())


@pytest.mark.asyncio
async def test_scanner_unavailable_fails_closed_and_cleans_managed(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr(settings, "av_mode", "clamav")
    monkeypatch.setattr(settings, "av_scanner_executable", None)
    scanner = LocalClamScanner()

    result = await scanner.register_and_enqueue(upload(b"%PDF-1.7\nunscanned"), original_filename="report.pdf")

    assert result.disposition is ScanDisposition.UNAVAILABLE
    assert result.error_code == "SCANNER_UNAVAILABLE"
    assert not list((tmp_path / "quarantine" / "managed").iterdir())


@pytest.mark.asyncio
async def test_supervisor_mode_fails_closed_until_control_pipe_adapter_exists(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr(settings, "av_mode", "supervisor")
    scanner = LocalClamScanner()

    result = await scanner.register_and_enqueue(upload(b"%PDF-1.7\nreport"), original_filename="report.pdf")

    assert result.disposition is ScanDisposition.UNAVAILABLE
    assert result.error_code == "SCANNER_CONTROL_PIPE_UNAVAILABLE"
    assert not list((tmp_path / "quarantine" / "managed").iterdir())

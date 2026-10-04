from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from finance_adapters.config import settings


class ScanDisposition(StrEnum):
    CLEAN = "clean"
    INFECTED = "infected"
    ERROR = "error"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ScanResult:
    quarantine_id: str
    disposition: ScanDisposition
    sha256: str
    file_size: int = 0
    original_filename: str | None = None
    object_id: str | None = None
    threat_name: str | None = None
    error_code: str | None = None
    scanner_state: str = "healthy"
    database_version: str = "unknown"
    engine_version: str = "unknown"
    scan_time_ms: int = 0


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sanitize_original_filename(filename: str | None) -> str | None:
    if not filename:
        return None
    cleaned = "".join(char for char in Path(filename).name if char.isprintable() and char not in "\x00\r\n")
    return cleaned[:255] or None


class LocalClamScanner:
    """Small development adapter for an installed clamscan executable.

    Production deployments should use the Rust Supervisor over its control pipe. This
    adapter exists for local integration tests and explicitly fails closed when the
    configured scanner executable is absent.
    """

    def __init__(self) -> None:
        self.root = settings.storage_dir / "quarantine"
        self.incoming = self.root / "incoming"
        self.managed = self.root / "managed"
        self.objects = settings.storage_dir / "objects"
        for directory in (self.incoming, self.managed, self.objects):
            directory.mkdir(parents=True, exist_ok=True)

    async def register_and_enqueue(
        self,
        upload,
        *,
        original_filename: str | None,
    ) -> ScanResult:
        quarantine_id = str(uuid.uuid4())
        uploading = self.incoming / f"{quarantine_id}.uploading"
        ready = self.incoming / f"{quarantine_id}.ready"
        managed = self.managed / quarantine_id
        safe_name = sanitize_original_filename(original_filename)
        digest = hashlib.sha256()
        size = 0
        try:
            with uploading.open("xb") as handle:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        raise ValueError("FILE_TOO_LARGE")
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if size == 0:
                raise ValueError("EMPTY_FILE")
            uploading.replace(ready)
            expected = digest.hexdigest()
            ready.replace(managed)
            actual = _sha256_file(managed)
            if actual != expected:
                raise ValueError("FILE_HASH_MISMATCH")
            with managed.open("rb") as handle:
                if handle.read(5) != b"%PDF-":
                    raise ValueError("PDF_REQUIRED")
            result = await self._scan_and_promote(
                quarantine_id,
                managed,
                expected,
                original_filename=safe_name,
            )
            if result.disposition is not ScanDisposition.CLEAN:
                managed.unlink(missing_ok=True)
            return result
        except Exception:
            for path in (uploading, ready):
                path.unlink(missing_ok=True)
            managed.unlink(missing_ok=True)
            raise

    async def _scan_and_promote(
        self,
        quarantine_id: str,
        managed: Path,
        expected: str,
        *,
        original_filename: str | None,
    ) -> ScanResult:
        if settings.av_mode == "mock":
            return await self._promote_clean(
                quarantine_id, managed, expected, original_filename=original_filename
            )
        if settings.av_mode == "supervisor":
            result = ScanResult(
                quarantine_id=quarantine_id,
                disposition=ScanDisposition.UNAVAILABLE,
                sha256=expected,
                file_size=managed.stat().st_size,
                original_filename=original_filename,
                error_code="SCANNER_CONTROL_PIPE_UNAVAILABLE",
                scanner_state="unavailable",
            )
            managed.unlink(missing_ok=True)
            return result
        if settings.av_mode == "clamav-native":
            return await self._scan_native_probe(
                quarantine_id, managed, expected, original_filename=original_filename
            )
        executable = settings.av_scanner_executable
        if not executable:
            result = ScanResult(
                quarantine_id=quarantine_id,
                disposition=ScanDisposition.UNAVAILABLE,
                sha256=expected,
                file_size=managed.stat().st_size,
                original_filename=original_filename,
                error_code="SCANNER_UNAVAILABLE",
                scanner_state="unavailable",
            )
            return result
        started = datetime.now(UTC)
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                [executable, "--no-summary", str(managed)],
                capture_output=True,
                text=True,
                timeout=settings.av_scan_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ScanResult(
                quarantine_id=quarantine_id,
                disposition=ScanDisposition.ERROR,
                sha256=expected,
                file_size=managed.stat().st_size,
                original_filename=original_filename,
                error_code="SCAN_TIMEOUT" if isinstance(exc, subprocess.TimeoutExpired) else "SCANNER_ERROR",
                scanner_state="degraded",
                scan_time_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
            )
        if completed.returncode == 1:
            threat = (completed.stdout or completed.stderr).strip()[:512] or None
            return ScanResult(
                quarantine_id=quarantine_id,
                disposition=ScanDisposition.INFECTED,
                sha256=expected,
                file_size=managed.stat().st_size,
                original_filename=original_filename,
                threat_name=threat,
                scan_time_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
            )
        if completed.returncode != 0:
            return ScanResult(
                quarantine_id=quarantine_id,
                disposition=ScanDisposition.ERROR,
                sha256=expected,
                file_size=managed.stat().st_size,
                original_filename=original_filename,
                error_code="SCAN_FAILED",
                scanner_state="degraded",
            )
        return await self._promote_clean(
            quarantine_id, managed, expected, original_filename=original_filename, started=started
        )

    async def _scan_native_probe(
        self,
        quarantine_id: str,
        managed: Path,
        expected: str,
        *,
        original_filename: str | None,
    ) -> ScanResult:
        executable = settings.av_native_probe_executable
        shim = settings.av_native_shim
        database = settings.av_native_database
        certs = settings.av_native_certs
        if not executable or not shim or not database or not certs:
            return ScanResult(
                quarantine_id=quarantine_id,
                disposition=ScanDisposition.UNAVAILABLE,
                sha256=expected,
                file_size=managed.stat().st_size,
                original_filename=original_filename,
                error_code="NATIVE_SCANNER_UNAVAILABLE",
                scanner_state="unavailable",
            )
        started = datetime.now(UTC)
        command = [
            executable,
            "native-probe",
            "--shim",
            shim,
            "--database",
            database,
            "--file",
            str(managed),
            "--certs",
            certs,
            "--json",
        ]
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                command,
                capture_output=True,
                text=True,
                timeout=settings.av_scan_timeout_seconds,
                check=False,
            )
            payload = json.loads(completed.stdout)
        except (OSError, subprocess.TimeoutExpired):
            return ScanResult(
                quarantine_id=quarantine_id,
                disposition=ScanDisposition.ERROR,
                sha256=expected,
                file_size=managed.stat().st_size,
                original_filename=original_filename,
                error_code="NATIVE_SCAN_TIMEOUT_OR_ERROR",
                scanner_state="degraded",
                scan_time_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
            )
        except (ValueError, KeyError):
            return ScanResult(
                quarantine_id=quarantine_id,
                disposition=ScanDisposition.ERROR,
                sha256=expected,
                file_size=managed.stat().st_size,
                original_filename=original_filename,
                error_code="NATIVE_SCAN_INVALID_RESPONSE",
                scanner_state="degraded",
            )
        if payload.get("status") == 1:
            return ScanResult(
                quarantine_id=quarantine_id,
                disposition=ScanDisposition.INFECTED,
                sha256=expected,
                file_size=managed.stat().st_size,
                original_filename=original_filename,
                threat_name=payload.get("threat_name"),
                scan_time_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
            )
        if payload.get("status") != 0 or completed.returncode != 0:
            return ScanResult(
                quarantine_id=quarantine_id,
                disposition=ScanDisposition.ERROR,
                sha256=expected,
                file_size=managed.stat().st_size,
                original_filename=original_filename,
                error_code="NATIVE_SCAN_FAILED",
                scanner_state="degraded",
            )
        return await self._promote_clean(
            quarantine_id,
            managed,
            expected,
            original_filename=original_filename,
            started=started,
        )

    async def _promote_clean(
        self,
        quarantine_id: str,
        managed: Path,
        expected: str,
        *,
        original_filename: str | None = None,
        started: datetime | None = None,
    ) -> ScanResult:
        started = started or datetime.now(UTC)
        file_size = managed.stat().st_size
        target = self.objects / expected[:2] / f"{expected}.pdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if _sha256_file(target) != expected:
                return ScanResult(
                    quarantine_id=quarantine_id,
                    disposition=ScanDisposition.ERROR,
                    sha256=expected,
                    file_size=file_size,
                    original_filename=original_filename,
                    error_code="OBJECT_HASH_CONFLICT",
                    scanner_state="degraded",
                )
        else:
            with managed.open("rb") as source, tempfile.NamedTemporaryFile(
                dir=target.parent, prefix=f".{expected}.", delete=False
            ) as temporary:
                shutil.copyfileobj(source, temporary)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            if _sha256_file(temporary_path) != expected:
                temporary_path.unlink(missing_ok=True)
                raise ValueError("OBJECT_HASH_CONFLICT")
            try:
                temporary_path.rename(target)
            except FileExistsError:
                temporary_path.unlink(missing_ok=True)
                if _sha256_file(target) != expected:
                    return ScanResult(
                        quarantine_id=quarantine_id,
                        disposition=ScanDisposition.ERROR,
                        sha256=expected,
                        file_size=file_size,
                        original_filename=original_filename,
                        error_code="OBJECT_HASH_CONFLICT",
                        scanner_state="degraded",
                    )
        managed.unlink(missing_ok=True)
        return ScanResult(
            quarantine_id=quarantine_id,
            disposition=ScanDisposition.CLEAN,
            sha256=expected,
            file_size=file_size,
            original_filename=original_filename,
            object_id=expected,
            scan_time_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
        )


scanner = LocalClamScanner()


def validate_protocol_id(value: str) -> None:
    if not _UUID_RE.fullmatch(value):
        raise ValueError("INVALID_QUARANTINE_ID")


def validate_sha256(value: str) -> None:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError("INVALID_SHA256")

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

from .named_pipe import request_json


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
            if settings.av_mode != "supervisor":
                ready.replace(managed)
            scan_path = ready if settings.av_mode == "supervisor" else managed
            actual = _sha256_file(scan_path)
            if actual != expected:
                raise ValueError("FILE_HASH_MISMATCH")
            # The Supervisor owns the ready -> managed claim.  Moving the file
            # here would race its claim and makes a successful registration
            # impossible.  Other adapters keep the historical local managed
            # path and therefore move the ready file before scanning.
            with scan_path.open("rb") as handle:
                if handle.read(5) != b"%PDF-":
                    raise ValueError("PDF_REQUIRED")
            result = await self._scan_and_promote(
                quarantine_id,
                scan_path,
                expected,
                original_filename=safe_name,
            )
            # Rust removes or promotes the ready file.  Leaving it in place on
            # an unavailable/error result allows Supervisor recovery to inspect
            # the durable queue entry instead of silently dropping an upload.
            if (
                result.disposition is not ScanDisposition.CLEAN
                and settings.av_mode != "supervisor"
            ):
                managed.unlink(missing_ok=True)
            return result
        except Exception:
            uploading.unlink(missing_ok=True)
            # Once the ready file has been published, the Supervisor is the
            # owner of its lifecycle.  Keep it for reconciliation on protocol
            # or transport failures.  Before publication it is safe to clean.
            if settings.av_mode != "supervisor":
                ready.unlink(missing_ok=True)
            if settings.av_mode != "supervisor":
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
            if not settings.allow_mock_scanner:
                return ScanResult(
                    quarantine_id=quarantine_id,
                    disposition=ScanDisposition.UNAVAILABLE,
                    sha256=expected,
                    file_size=managed.stat().st_size,
                    original_filename=original_filename,
                    error_code="MOCK_SCANNER_DISABLED",
                    scanner_state="unavailable",
                )
            return await self._promote_clean(
                quarantine_id, managed, expected, original_filename=original_filename
            )
        if settings.av_mode == "supervisor":
            return await self._scan_supervisor(
                quarantine_id,
                managed,
                expected,
                original_filename=original_filename,
            )
        if settings.av_mode == "clamav-native":
            return await self._scan_native_probe(
                quarantine_id, managed, expected, original_filename=original_filename
            )
        if settings.av_mode not in {"clamav", "clamscan"}:
            return ScanResult(
                quarantine_id=quarantine_id,
                disposition=ScanDisposition.UNAVAILABLE,
                sha256=expected,
                file_size=managed.stat().st_size,
                original_filename=original_filename,
                error_code="SCANNER_MODE_INVALID",
                scanner_state="unavailable",
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

    async def _scan_supervisor(
        self,
        quarantine_id: str,
        ready: Path,
        expected: str,
        *,
        original_filename: str | None,
    ) -> ScanResult:
        """Register a ready file and wait for the Rust Supervisor verdict.

        The pipe protocol is deliberately kept here, at the scanner boundary:
        the rest of the API only sees :class:`ScanResult`.  Each request uses a
        fresh named-pipe connection and a 4-byte big-endian length prefix, as
        specified by scanner-protocol-v1.1.  Blocking Windows file operations
        run in a worker thread so an unavailable Supervisor cannot block FastAPI.
        """

        file_size = ready.stat().st_size
        started = datetime.now(UTC)
        request = {
            "operation": "register_and_enqueue",
            "payload": {
                "quarantine_id": quarantine_id,
                "sha256": expected,
                "size_bytes": file_size,
                "original_filename": original_filename,
            },
        }
        try:
            accepted = await self._supervisor_request(request)
            accepted_payload = self._supervisor_payload(accepted, expected_request="accepted")
            if accepted_payload.get("quarantine_id") not in {None, quarantine_id}:
                raise ValueError("SUPERVISOR_QUARANTINE_MISMATCH")
            if accepted_payload.get("disposition") not in {None, "queued"}:
                raise ValueError("SUPERVISOR_REGISTER_REJECTED")
        except TimeoutError:
            return self._supervisor_failure(
                quarantine_id,
                expected,
                file_size,
                original_filename,
                "SCANNER_CONTROL_PIPE_UNAVAILABLE",
                "unavailable",
                started,
            )
        except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
            error_code = (
                "SCANNER_CONTROL_PIPE_UNAVAILABLE"
                if isinstance(exc, OSError)
                else "SCANNER_PROTOCOL_ERROR"
            )
            return self._supervisor_failure(
                quarantine_id,
                expected,
                file_size,
                original_filename,
                error_code,
                "unavailable" if isinstance(exc, OSError) else "degraded",
                started,
            )

        deadline = asyncio.get_running_loop().time() + settings.av_scan_timeout_seconds
        while True:
            if asyncio.get_running_loop().time() >= deadline:
                return self._supervisor_failure(
                    quarantine_id,
                    expected,
                    file_size,
                    original_filename,
                    "SCAN_TIMEOUT",
                    "degraded",
                    started,
                )
            try:
                response = await self._supervisor_request(
                    {
                        "operation": "get_scan_status",
                        "payload": {"quarantine_id": quarantine_id},
                    }
                )
                envelope = response.get("payload")
                if not isinstance(envelope, dict):
                    raise TypeError("SUPERVISOR_PROTOCOL_PAYLOAD")
                status = envelope.get("status")
                if status == "scan_result":
                    payload = envelope.get("payload")
                    if not isinstance(payload, dict):
                        raise ValueError("SUPERVISOR_SCAN_RESULT_PAYLOAD")
                    if payload.get("disposition") in {
                        "queued", "claimed", "scanning", "promoting", "running"
                    }:
                        await asyncio.sleep(max(settings.av_supervisor_poll_interval_seconds, 0.01))
                        continue
                    return self._scan_result_from_supervisor(
                        payload,
                        quarantine_id=quarantine_id,
                        expected=expected,
                        file_size=file_size,
                        original_filename=original_filename,
                        started=started,
                    )
                if status not in {"accepted", "queued", "pending", "scanning", "running"}:
                    raise ValueError("SUPERVISOR_UNKNOWN_SCAN_STATUS")
            except TimeoutError:
                return self._supervisor_failure(
                    quarantine_id,
                    expected,
                    file_size,
                    original_filename,
                    "SCANNER_CONTROL_PIPE_TIMEOUT",
                    "degraded",
                    started,
                )
            except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
                return self._supervisor_failure(
                    quarantine_id,
                    expected,
                    file_size,
                    original_filename,
                    "SCANNER_CONTROL_PIPE_UNAVAILABLE"
                    if isinstance(exc, OSError)
                    else "SCANNER_PROTOCOL_ERROR",
                    "unavailable" if isinstance(exc, OSError) else "degraded",
                    started,
                )
            await asyncio.sleep(max(settings.av_supervisor_poll_interval_seconds, 0.01))

    @staticmethod
    def _supervisor_payload(response: dict, *, expected_request: str) -> dict:
        payload = response.get("payload")
        if not isinstance(payload, dict) or payload.get("status") != expected_request:
            raise ValueError("SUPERVISOR_REGISTER_RESPONSE_INVALID")
        nested = payload.get("payload")
        if not isinstance(nested, dict):
            raise TypeError("SUPERVISOR_REGISTER_PAYLOAD_INVALID")
        return nested

    async def _supervisor_request(self, operation: dict) -> dict:
        request_id = str(uuid.uuid4())
        envelope = {
            "protocol_major": 1,
            "protocol_minor": 1,
            "request_id": request_id,
            "payload": operation,
        }
        response = await asyncio.to_thread(
            self._supervisor_request_sync,
            envelope,
        )
        if response.get("request_id") != request_id:
            raise ValueError("SUPERVISOR_REQUEST_ID_MISMATCH")
        if response.get("protocol_major") != 1 or response.get("protocol_minor") != 1:
            raise ValueError("SUPERVISOR_PROTOCOL_VERSION_MISMATCH")
        return response

    def supervisor_request_sync(self, operation: str, payload: dict | None = None) -> dict:
        """Synchronous control-pipe request for launchers and health checks."""
        request_id = str(uuid.uuid4())
        request = {"operation": operation}
        if payload is not None:
            request["payload"] = payload
        envelope = {
            "protocol_major": 1,
            "protocol_minor": 1,
            "request_id": request_id,
            "payload": request,
        }
        response = self._supervisor_request_sync(envelope)
        if response.get("request_id") != request_id:
            raise ValueError("SUPERVISOR_REQUEST_ID_MISMATCH")
        if response.get("protocol_major") != 1 or response.get("protocol_minor") != 1:
            raise ValueError("SUPERVISOR_PROTOCOL_VERSION_MISMATCH")
        return response

    def supervisor_get_health(self) -> dict:
        return self.supervisor_request_sync("get_health")

    @staticmethod
    def _supervisor_request_sync(envelope: dict) -> dict:
        return request_json(
            settings.av_supervisor_pipe,
            envelope,
            timeout_seconds=settings.av_supervisor_connect_timeout_seconds,
        )

    @staticmethod
    def _supervisor_failure(
        quarantine_id: str,
        expected: str,
        file_size: int,
        original_filename: str | None,
        error_code: str,
        scanner_state: str,
        started: datetime,
    ) -> ScanResult:
        return ScanResult(
            quarantine_id=quarantine_id,
            disposition=ScanDisposition.UNAVAILABLE
            if error_code.startswith("SCANNER_CONTROL_PIPE")
            else ScanDisposition.ERROR,
            sha256=expected,
            file_size=file_size,
            original_filename=original_filename,
            error_code=error_code,
            scanner_state=scanner_state,
            scan_time_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
        )

    @staticmethod
    def _scan_result_from_supervisor(
        payload: dict,
        *,
        quarantine_id: str,
        expected: str,
        file_size: int,
        original_filename: str | None,
        started: datetime,
    ) -> ScanResult:
        if payload.get("quarantine_id") != quarantine_id:
            raise ValueError("SUPERVISOR_QUARANTINE_MISMATCH")
        if payload.get("sha256") != expected:
            raise ValueError("SUPERVISOR_HASH_MISMATCH")
        try:
            disposition = ScanDisposition(str(payload["disposition"]))
        except (KeyError, ValueError) as exc:
            raise ValueError("SUPERVISOR_DISPOSITION_INVALID") from exc
        if disposition is ScanDisposition.CLEAN and payload.get("object_id") != expected:
            raise ValueError("SUPERVISOR_OBJECT_ID_MISMATCH")
        return ScanResult(
            quarantine_id=quarantine_id,
            disposition=disposition,
            sha256=str(payload.get("sha256") or expected),
            file_size=int(payload.get("file_size") or file_size),
            original_filename=payload.get("original_filename") or original_filename,
            object_id=payload.get("object_id"),
            threat_name=payload.get("threat_name"),
            error_code=payload.get("error_code"),
            scanner_state=str(payload.get("scanner_state") or "healthy"),
            database_version=str(payload.get("database_version") or "unknown"),
            engine_version=str(payload.get("engine_version") or "unknown"),
            scan_time_ms=int(payload.get("scan_time_ms") or int((datetime.now(UTC) - started).total_seconds() * 1000)),
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


def supervisor_request_sync(operation: str, payload: dict | None = None) -> dict:
    """Send one synchronous request to the configured Scanner Supervisor."""
    return scanner.supervisor_request_sync(operation, payload)


def supervisor_get_health() -> dict:
    """Return the Supervisor health response for local launchers."""
    return scanner.supervisor_get_health()


def validate_protocol_id(value: str) -> None:
    if not _UUID_RE.fullmatch(value):
        raise ValueError("INVALID_QUARANTINE_ID")


def validate_sha256(value: str) -> None:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError("INVALID_SHA256")

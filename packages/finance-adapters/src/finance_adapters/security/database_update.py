"""Controlled ClamAV database update orchestration.

The updater is deliberately outside the upload path. It invokes the checked-in
PowerShell staging helper, which downloads into a new version directory,
verifies official detached signatures and a native probe, then atomically swaps
``current.json``. The Rust Supervisor notices that pointer and validates the
candidate before replacing its Worker.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from finance_adapters.config import settings

from .scanner import supervisor_get_health

_lock = threading.Lock()
_status: dict[str, Any] = {"state": "idle", "error": None}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _active_database() -> dict[str, Any]:
    root = Path(settings.av_native_database or "")
    pointer_path = root / "current.json"
    if not pointer_path.is_file():
        return {}
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        manifest_path = root / str(pointer["manifest"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        created = datetime.fromisoformat(str(manifest["created_at"]))
        return {
            "database_id": pointer.get("database_id"),
            "activated_at": pointer.get("activated_at"),
            "created_at": manifest.get("created_at"),
            "age_seconds": max(0, int((datetime.now(UTC) - created.astimezone(UTC)).total_seconds())),
            "official_database": bool(manifest.get("official_database")),
        }
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return {"error": "DATABASE_POINTER_INVALID"}


def get_status() -> dict[str, Any]:
    with _lock:
        result = dict(_status)
    database = _active_database()
    result["database"] = database
    try:
        result["supervisor"] = supervisor_get_health().get("payload", {}).get("payload", {})
    except (OSError, TimeoutError, ValueError, KeyError, TypeError) as exc:  # status must remain readable when Supervisor is down
        result["supervisor"] = {"ready": False, "error": str(exc)[:256]}
    health = result["supervisor"]
    # Stable flat fields for desktop clients; nested objects remain for future
    # extension and diagnostics.
    result.update({
        "ready": bool(health.get("ready", False)),
        "queue_depth": int(health.get("queue_depth", 0) or 0),
        "active_scans": int(health.get("active_scans", 0) or 0),
        "update_error": health.get("update_error") or result.get("error"),
        "database_version": database.get("database_id"),
        "database_age_seconds": database.get("age_seconds"),
        "database_updated_at": database.get("activated_at") or database.get("created_at"),
    })
    return result


def _set(**values: Any) -> None:
    with _lock:
        _status.update(values)


def _run_update() -> None:
    _set(state="running", started_at=_now(), finished_at=None, error=None)
    lock_path: Path | None = None
    try:
        script = Path(settings.av_update_script or "")
        if not script.is_file():
            raise RuntimeError("AV_UPDATE_SCRIPT_NOT_CONFIGURED")
        root = Path(settings.av_native_database or "")
        if not root.is_dir():
            raise RuntimeError("AV_DATABASE_ROOT_NOT_CONFIGURED")
        # Protect against two API worker processes invoking freshclam at once.
        lock_path = root / ".finreport-update.lock"
        try:
            handle = lock_path.open("x", encoding="ascii")
            handle.write(_now())
            handle.close()
        except FileExistsError as exc:
            raise RuntimeError("AV_UPDATE_ALREADY_RUNNING") from exc
        required = {
            "freshclam": settings.av_update_freshclam,
            "sigtool": settings.av_update_sigtool,
            "signature_certs": settings.av_update_signature_certs,
            "probe_executable": settings.av_update_probe_executable or settings.av_native_probe_executable,
            "probe_shim": settings.av_native_shim,
            "probe_file": settings.av_update_probe_file,
            "probe_certs": settings.av_native_certs,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError("AV_UPDATE_CONFIGURATION_MISSING:" + ",".join(missing))
        args = [
            "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
            "-DatabaseRoot", str(root), "-FreshclamPath", str(required["freshclam"]),
            "-SigtoolPath", str(required["sigtool"]), "-SignatureCerts", str(required["signature_certs"]),
            "-ProbeExecutable", str(required["probe_executable"]), "-ProbeShim", str(required["probe_shim"]),
            "-ProbeFile", str(required["probe_file"]), "-ProbeCerts", str(required["probe_certs"]),
            "-RequireOfficialDatabase", "-Activate",
        ]
        if settings.av_update_config:
            args.extend(["-FreshclamConfig", str(settings.av_update_config)])
        completed = subprocess.run(
            ["powershell.exe", *args], capture_output=True, text=True,
            timeout=max(30.0, settings.av_update_timeout_seconds), check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "freshclam update failed").strip()[-2000:]
            raise RuntimeError(f"AV_UPDATE_FAILED:{detail}")
        manifest = None
        for line in reversed(completed.stdout.splitlines()):
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and candidate.get("schema") == "finreport-clamav-database-v1":
                manifest = candidate
                break
        if manifest is None:
            raise RuntimeError("AV_UPDATE_INVALID_MANIFEST_OUTPUT")
        # Confirm the Supervisor actually adopted the candidate. The helper has
        # already validated the native probe; this catches an ABI/runtime issue
        # before reporting success to the UI.
        expected_id = manifest.get("database_id")
        deadline = __import__("time").monotonic() + 30
        adopted = False
        while __import__("time").monotonic() < deadline:
            try:
                health = supervisor_get_health().get("payload", {}).get("payload", {})
                active = _active_database()
                if health.get("ready") and active.get("database_id") == expected_id:
                    adopted = True
                    if adopted:
                        break
            except (OSError, TimeoutError, ValueError, KeyError, TypeError):
                pass
            __import__("time").sleep(0.5)
        if not adopted:
            raise RuntimeError("AV_UPDATE_SUPERVISOR_ADOPTION_TIMEOUT")
        _set(state="succeeded", finished_at=_now(), result={"database_id": expected_id, "manifest": manifest})
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        _set(state="failed", finished_at=_now(), error=str(exc)[:2000])
    finally:
        if lock_path is not None:
            lock_path.unlink(missing_ok=True)


async def start_update() -> dict[str, Any]:
    if not settings.av_update_enabled:
        raise RuntimeError("AV_UPDATE_DISABLED")
    if settings.av_mode != "supervisor":
        raise RuntimeError("AV_UPDATE_REQUIRES_SUPERVISOR")
    with _lock:
        if _status.get("state") in {"queued", "running"}:
            raise RuntimeError("AV_UPDATE_ALREADY_RUNNING")
        _status.update({"state": "queued", "queued_at": _now(), "error": None})
    asyncio.create_task(asyncio.to_thread(_run_update))
    return get_status()

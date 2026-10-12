"""Start the bundled Scanner before exposing the API, and own its lifetime."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    args, uvicorn_args = parser.parse_known_args()
    bundle = args.bundle.resolve()
    runtime = bundle / "clamav"
    executable = bundle / "scanner" / "finreport-scanner-supervisor.exe"
    database_root = runtime / "db"
    current_pointer = database_root / "current.json"
    if not current_pointer.is_file():
        raise SystemExit(f"Bundled ClamAV database pointer missing: {current_pointer}")
    # The bundle must carry an activated immutable database version.  Requiring
    # the pointer and manifest here prevents a launcher from silently starting
    # an API with an empty directory or an experimental HDB fallback.
    try:
        current = json.loads(current_pointer.read_text(encoding="utf-8"))
        root_resolved = database_root.resolve()
        database_dir = (database_root / current["directory"]).resolve()
        manifest_path = (database_root / current["manifest"]).resolve()
        database_dir.relative_to(root_resolved)
        manifest_path.relative_to(root_resolved)
        if current.get("schema") != "finreport-clamav-current-v1" or not database_dir.is_dir() or not manifest_path.is_file():
            raise ValueError
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("schema") != "finreport-clamav-database-v1"
            or manifest.get("database_id") != current.get("database_id")
            or not manifest.get("official_database")
            or len(manifest.get("signature_verification") or []) != 3
            or (manifest.get("native_probe") or {}).get("status") != 0
            or (manifest.get("native_probe") or {}).get("scan_rc") != 0
        ):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Bundled ClamAV database pointer/manifest invalid: {current_pointer}") from exc
    for required in (executable, runtime / "finreport_clamav.dll", runtime / "certs" / "clamav.crt"):
        if not required.is_file():
            raise SystemExit(f"Bundled scanner asset missing: {required}")
    os.environ.update({
        "FINREPORT_AV_MODE": "supervisor",
        "FINREPORT_ALLOW_MOCK_SCANNER": "false",
        "FINREPORT_STORAGE_DIR": str(bundle / "data" / "storage"),
        "FINREPORT_DATABASE_URL": f"sqlite:///{(bundle / 'data' / 'finreport.db').as_posix()}",
        "FINREPORT_AV_NATIVE_SHIM": str(runtime / "finreport_clamav.dll"),
        "FINREPORT_AV_NATIVE_DATABASE": str(runtime / "db"),
        "FINREPORT_AV_NATIVE_CERTS": str(runtime / "certs"),
        # Official databases can take tens of seconds to load on first start.
        # Keep health polling and API requests fail-closed without racing that
        # initialization window.
        "FINREPORT_SCANNER_CONNECT_TIMEOUT_SECONDS": "90",
        "PATH": f"{runtime};{os.environ.get('PATH', '')}",
    })
    # A private pipe per bundle prevents an unrelated running Scanner from
    # returning results for a different storage directory.
    import hashlib

    identity = hashlib.sha256(str(bundle).casefold().encode()).hexdigest()[:16]
    os.environ["FINREPORT_SCANNER_PIPE"] = rf"\\.\pipe\FinReport.Scanner.Bundle.{identity}"
    from finance_adapters.security import supervisor_get_health

    logs = bundle / "data" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / "scanner.log").open("ab", buffering=0) as log:
        process = subprocess.Popen(
            [str(executable), "serve"], stdout=log, stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            deadline = time.monotonic() + 60
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f"Scanner startup failed; see {logs / 'scanner.log'}")
                try:
                    health = supervisor_get_health()["payload"]
                    if health["status"] == "health" and health["payload"]["ready"]:
                        break
                except (OSError, TimeoutError):
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Scanner is not READY; see {logs / 'scanner.log'}")
                time.sleep(0.1)
            print("ClamAV Scanner READY; starting FinReport API", flush=True)
            # Use uvicorn's CLI so normal host/port/log options remain available.
            from uvicorn.main import main as uvicorn_main

            uvicorn_main(args=["finreport_api.main:app", "--host", "127.0.0.1", "--port", "8000", *uvicorn_args])
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)


if __name__ == "__main__":
    main()

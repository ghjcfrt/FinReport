"""Security adapters for uploaded report files."""

from .scanner import (
    ScanDisposition,
    ScanResult,
    scanner,
    supervisor_get_health,
    supervisor_request_sync,
)

__all__ = [
    "ScanDisposition",
    "ScanResult",
    "scanner",
    "supervisor_get_health",
    "supervisor_request_sync",
]

"""Durable, credential-safe run events and immutable diagnostic artifacts.

SQLite is authoritative. JSONL, the readable log and manifest are projections,
never inputs to execution. A run reopened for inspection creates no new attempt.
An interrupted HTTP dispatch is deliberately not a retry instruction.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

LEVELS = {"TRACE": 5, "DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40,
          "CRITICAL": 50}
_SENSITIVE = re.compile(
    r"^(authorization|proxyauthorization|xapikey|apikey|accesstoken|refreshtoken|"
    r"password|passwd|secret|secretkey|clientsecret|cookie|setcookie|signature|"
    r"sig|token|key|apitoken|authtoken|sessionid|sessiontoken|sastoken|subscriptionkey|"
    r"credential|credentials|xamzcredential|xamzsignature|"
    r"xamzsecuritytoken|xgoogcredential|xgoogsignature)$", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|"
    r"client[_-]?secret|secret[_-]?key|signature|authorization|cookie|token)"
    r"\s*[:=]\s*)(?:Bearer\s+)?[^\s,;&\"'<>]+")
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;\"'<>]+")
_AUTH_HEADER = re.compile(r"(?i)(\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*[:=]\s*)[^\r\n]+")
_EVENT_DEFAULTS = [
    "task_id", "parent_task_id", "trace_id", "span_id", "parent_span_id", "attempt_id",
    "duration_ms", "execution_status", "document_id", "document_version", "chapter_id",
    "evidence_ids", "claim_ids", "calculation_ids", "input_artifact_ref", "output_artifact_ref",
    "input_hash", "output_hash", "error_code", "message", "retryable", "next_action",
]
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCK_GUARD = threading.Lock()


class JournalPersistenceError(RuntimeError):
    """Key audit data could not be stored; do not dispatch another paid call."""


class JournalIntegrityError(ValueError):
    """A diagnostic artifact is missing, unpublished or has changed."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _fsync_directory(path: Path) -> None:
    # Windows does not expose portable directory fsync. File fsync + replace
    # remain mandatory there; SQLite's FULL commit protects its own journal.
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
    with temporary.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


class RunJournal:
    """One run under ``root/run_id`` with lazy, append-only writer attempts.

    ``artifact`` accepts JSON-compatible values and returns a run-relative path
    and SHA-256 of the *sanitized* bytes. Paths are immutable and attempt scoped.
    Concurrent workers use both an OS lock and SQLite FULL transactions; reads
    always consult SQLite, not a possibly interrupted text projection.
    """

    def __init__(self, root: Path, run_id: str, secrets: Iterable[str] = ()):
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", run_id):
            raise ValueError("JOURNAL_INVALID_RUN_ID")
        if run_id in {".", ".."}:
            raise ValueError("JOURNAL_INVALID_RUN_ID")
        self.run_id = run_id
        self.root = Path(root).resolve()
        self.run_dir = self.root / run_id
        if self.run_dir.is_symlink():
            raise ValueError("JOURNAL_UNSAFE_RUN_PATH")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.run_dir / "journal.sqlite3"
        if self.db_path.is_symlink() or (self.run_dir / ".journal.lock").is_symlink():
            raise ValueError("JOURNAL_UNSAFE_RUN_PATH")
        self._secrets = {value for value in secrets if isinstance(value, str) and value}
        self._attempt_id: str | None = None
        self._dispatch_blocked = False
        new_database = not self.db_path.exists()
        with _THREAD_LOCK_GUARD:
            self._thread_lock = _THREAD_LOCKS.setdefault(str(self.run_dir), threading.RLock())
        try:
            with self._locked(), self._connect() as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS attempts (
                        attempt_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, pid INTEGER NOT NULL);
                    CREATE TABLE IF NOT EXISTS events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id TEXT NOT NULL,
                        level INTEGER NOT NULL, stage TEXT NOT NULL, task_id TEXT, body TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS artifacts (
                        path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, status TEXT NOT NULL,
                        attempt_id TEXT NOT NULL, created_at TEXT NOT NULL, kind TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS manifest_revisions (
                        revision INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                """)
                if new_database:
                    self._manifest_projection(db)
        except Exception as exc:  # noqa: BLE001 - fail closed with independent emergency audit.
            self._failed("initialize", exc)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock, (self.run_dir / ".journal.lock").open("a+b") as handle:
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            deadline = time.monotonic() + 30
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise JournalPersistenceError("JOURNAL_LOCK_TIMEOUT") from None
                    time.sleep(0.02)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def add_secrets(self, values: Iterable[str]) -> None:
        self._secrets.update(value for value in values if isinstance(value, str) and value)

    def sanitize(self, value: Any) -> Any:
        """The same filter is used before every file, DB and public return."""
        if isinstance(value, dict):
            return {self._sanitize_text(str(key)): "[REDACTED]"
                    if _SENSITIVE.fullmatch(re.sub(r"[-_]", "", str(key)))
                    else self.sanitize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self.sanitize(item) for item in value]
        if isinstance(value, str):
            return self._sanitize_text(value)
        if isinstance(value, float) and not math.isfinite(value):
            return {"non_finite_number": str(value)}
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, Path):
            return self._sanitize_text(str(value))
        if hasattr(value, "model_dump"):
            return self.sanitize(value.model_dump(mode="json"))
        return self._sanitize_text(str(value))

    def _sanitize_text(self, value: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            if len(secret) >= 8 or value == secret:
                value = value.replace(secret, "[REDACTED]")
            else:
                value = re.sub(r"(?<!\w)" + re.escape(secret) + r"(?!\w)", "[REDACTED]", value)

        def clean_url(match: re.Match[str]) -> str:
            try:
                parsed = urlsplit(match.group())
                host = parsed.hostname or ""
                if ":" in host:
                    host = f"[{host}]"
                if parsed.port:
                    host += f":{parsed.port}"
                query = [(key, "[REDACTED]" if _SENSITIVE.fullmatch(
                    re.sub(r"[-_]", "", key)) else item)
                         for key, item in parse_qsl(parsed.query, keep_blank_values=True)]
                return urlunsplit((parsed.scheme, host, parsed.path, urlencode(query), ""))
            except ValueError:
                return "[REDACTED_URL]"

        value = _URL.sub(clean_url, value)
        value = _AUTH_HEADER.sub(r"\1[REDACTED]", value)
        value = _BEARER.sub("Bearer [REDACTED]", value)
        return _CREDENTIAL_ASSIGNMENT.sub(r"\1[REDACTED]", value)

    def _failed(self, operation: str, exc: Exception) -> None:
        self._dispatch_blocked = True
        emergency = self.sanitize({"schema_version": "run_journal_v1", "run_id": self.run_id,
                                   "timestamp_utc": _now(), "level": "CRITICAL",
                                   "event_type": "journal.persistence_failed", "operation": operation,
                                   "error_type": type(exc).__name__, "message": str(exc),
                                   "next_action": "stop_new_dispatches_and_inspect_storage"})
        try:
            emergency_root = Path(tempfile.gettempdir()) / "finreport-journal-emergency"
            _atomic(emergency_root / f"{self.run_id}-{uuid.uuid4().hex}.json",
                    _json(emergency).encode("utf-8"))
        except Exception:  # noqa: BLE001 - even the emergency destination can be unavailable.
            # A generic constant is safe even when both persistence paths fail.
            print("CRITICAL JOURNAL_PERSISTENCE_FAILED; new dispatches must stop", file=sys.stderr)
        raise JournalPersistenceError(f"JOURNAL_PERSISTENCE_FAILED:{operation}") from None

    def _ensure_attempt(self, db: sqlite3.Connection) -> str:
        if self._attempt_id is None:
            self._attempt_id = uuid.uuid4().hex
        db.execute("INSERT OR IGNORE INTO attempts VALUES (?,?,?)", (self._attempt_id, _now(), os.getpid()))
        return self._attempt_id

    @property
    def attempt_id(self) -> str | None:
        return self._attempt_id

    @property
    def manifest(self) -> dict[str, Any]:
        with self._connect() as db:
            return self._manifest(db)

    def _manifest(self, db: sqlite3.Connection) -> dict[str, Any]:
        value: dict[str, Any] = {"code_commit": "unknown", "workspace_diff_sha256": "unknown",
                                 "runtime_version": sys.version.split()[0],
                                 "dependency_lock_sha256": "unknown", "provider_build": "unknown"}
        revision = 0
        for row in db.execute("SELECT revision,body FROM manifest_revisions ORDER BY revision"):
            value.update(json.loads(row["body"]))
            revision = row["revision"]
        value.update(schema_version="run_journal_v1", run_id=self.run_id, revision=revision,
                     attempts=[dict(row) for row in db.execute("SELECT * FROM attempts ORDER BY created_at")],
                     artifacts=[dict(row) for row in db.execute(
                         "SELECT path,sha256,status,attempt_id,kind FROM artifacts ORDER BY created_at,path")])
        return self.sanitize(value)

    def _manifest_projection(self, db: sqlite3.Connection) -> None:
        _atomic(self.run_dir / "manifest.json", _json(self._manifest(db)).encode("utf-8"))

    def update_manifest(self, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise TypeError("JOURNAL_MANIFEST_MUST_BE_OBJECT")
        if {"run_id", "schema_version", "attempts", "artifacts", "revision"} & value.keys():
            raise ValueError("JOURNAL_RESERVED_MANIFEST_FIELD")
        try:
            with self._locked(), self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self._ensure_attempt(db)
                db.execute("INSERT INTO manifest_revisions(body) VALUES (?)", (_json(self.sanitize(value)),))
                db.commit()
                self._manifest_projection(db)
                return self._manifest(db)
        except Exception as exc:  # noqa: BLE001 - persistence failures must stop dispatches.
            self._failed("manifest", exc)

    def _projection(self, db: sqlite3.Connection, previous_sequence: int,
                    event: dict[str, Any]) -> None:
        marker = db.execute("SELECT value FROM state WHERE key='projection_sequence'").fetchone()
        projection_damaged = any(not (self.run_dir / name).is_file() or
                                 not (self.run_dir / name).read_bytes().endswith(b"\n")
                                 for name in ("events.jsonl", "run.log"))
        if marker is None or int(marker[0]) != previous_sequence or projection_damaged:
            events = [json.loads(row[0]) for row in db.execute("SELECT body FROM events ORDER BY sequence")]
            _atomic(self.run_dir / "events.jsonl", "".join(_json(item) + "\n" for item in events).encode("utf-8"))
            _atomic(self.run_dir / "run.log", "".join(self._log_line(item) for item in events).encode("utf-8"))
        else:
            for name, line in (("events.jsonl", _json(event) + "\n"), ("run.log", self._log_line(event))):
                with (self.run_dir / name).open("ab") as handle:
                    handle.write(line.encode("utf-8"))
                    handle.flush()
                    os.fsync(handle.fileno())
        db.execute("INSERT OR REPLACE INTO state VALUES ('projection_sequence',?)",
                   (str(event["run_sequence"]),))
        db.commit()

    @staticmethod
    def _log_line(event: dict[str, Any]) -> str:
        message = str(event.get("message") or "").replace("\r", "\\r").replace("\n", "\\n")
        return (f"{event['timestamp_utc']} #{event['run_sequence']} {event['level']} "
                f"[{event['stage']}] {event['event_type']} task={event.get('task_id')} "
                f"{message}\n")

    def emit(self, level: str, stage: str, event_type: str, **fields: Any) -> dict[str, Any]:
        level = level.upper()
        if level not in LEVELS or not stage or not event_type:
            raise ValueError("JOURNAL_INVALID_EVENT")
        if {"event_id", "run_sequence", "attempt_sequence", "run_id", "schema_version",
            "timestamp_utc", "level", "stage", "event_type"} & fields.keys():
            raise ValueError("JOURNAL_RESERVED_EVENT_FIELD")
        try:
            with self._locked(), self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                writer = self._ensure_attempt(db)
                attempt = self._sanitize_text(str(fields.get("attempt_id") or writer))
                attempt_sequence = db.execute("SELECT count(*) FROM events WHERE attempt_id=?", (attempt,)).fetchone()[0] + 1
                previous = db.execute("SELECT coalesce(max(sequence),0) FROM events").fetchone()[0]
                event = {key: None for key in _EVENT_DEFAULTS}
                event.update(fields)
                event.update(schema_version="run_journal_v1", event_id=uuid.uuid4().hex,
                             run_id=self.run_id, writer_attempt_id=writer, attempt_id=attempt,
                             timestamp_utc=_now(), attempt_sequence=attempt_sequence,
                             level=level, stage=stage, event_type=event_type)
                event = self.sanitize(event)
                cursor = db.execute("INSERT INTO events(attempt_id,level,stage,task_id,body) VALUES (?,?,?,?,?)",
                                    (attempt, LEVELS[level], event["stage"], event.get("task_id"), "{}"))
                event["run_sequence"] = cursor.lastrowid
                db.execute("UPDATE events SET body=? WHERE sequence=?", (_json(event), cursor.lastrowid))
                db.commit()
                self._projection(db, previous, event)
                self._manifest_projection(db)
            logging.getLogger("finreport.run_journal").log(LEVELS[level], self._log_line(event).rstrip())
            return event
        except Exception as exc:  # noqa: BLE001 - persistence failures must stop dispatches.
            self._failed("event", exc)

    def _artifact_path(self, relative: str) -> Path:
        pure = PurePosixPath(relative.replace("\\", "/"))
        if pure.is_absolute() or not pure.parts or any(part in {".", ".."} or ":" in part for part in pure.parts):
            raise ValueError("JOURNAL_UNSAFE_ARTIFACT_PATH")
        candidate = self.run_dir.joinpath(*pure.parts)
        if not candidate.resolve().is_relative_to(self.run_dir.resolve()):
            raise ValueError("JOURNAL_UNSAFE_ARTIFACT_PATH")
        if any(parent.is_symlink() for parent in [candidate, *candidate.parents]
               if parent.is_relative_to(self.run_dir)):
            raise ValueError("JOURNAL_UNSAFE_ARTIFACT_PATH")
        return candidate

    def artifact(self, relative_path: str, value: Any) -> dict[str, str]:
        self._artifact_path(relative_path)
        relative_path = self._sanitize_text(relative_path)
        self._artifact_path(relative_path)
        body = _json(self.sanitize(value)).encode("utf-8")
        checksum = _digest(body)
        try:
            with self._locked(), self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                attempt = self._ensure_attempt(db)
                relative = f"artifacts/{attempt}/{uuid.uuid4().hex}/{relative_path.replace(chr(92), '/')}"
                target = self._artifact_path(relative)
                db.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?)",
                           (relative, checksum, "prepared", attempt, _now(), "json"))
                db.commit()
                _atomic(target, body)
                db.execute("UPDATE artifacts SET status='available' WHERE path=?", (relative,))
                db.commit()
                self._manifest_projection(db)
                return {"path": relative, "sha256": checksum}
        except Exception as exc:  # noqa: BLE001 - persistence failures must stop dispatches.
            self._failed("artifact", exc)

    def read_artifact(self, ref: dict[str, str] | str) -> Any:
        relative = ref.get("path") if isinstance(ref, dict) else ref
        if not isinstance(relative, str):
            raise JournalIntegrityError("JOURNAL_ARTIFACT_REFERENCE_INVALID")
        target = self._artifact_path(relative)
        with self._connect() as db:
            row = db.execute("SELECT * FROM artifacts WHERE path=?", (relative,)).fetchone()
        if row is None or row["status"] != "available":
            raise JournalIntegrityError("JOURNAL_ARTIFACT_NOT_PUBLISHED")
        try:
            body = target.read_bytes()
        except OSError:
            raise JournalIntegrityError("JOURNAL_ARTIFACT_MISSING") from None
        checksum = _digest(body)
        if checksum != row["sha256"] or (isinstance(ref, dict) and ref.get("sha256") != checksum):
            raise JournalIntegrityError("JOURNAL_ARTIFACT_HASH_MISMATCH")
        try:
            return self.sanitize(json.loads(body))
        except (ValueError, UnicodeError):
            raise JournalIntegrityError("JOURNAL_ARTIFACT_INVALID_JSON") from None

    def read_events(self, after: int = 0, limit: int = 100, min_level: str = "TRACE",
                    stage: str | None = None, task_id: str | None = None) -> dict[str, Any]:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("JOURNAL_INVALID_PAGINATION")
        if min_level.upper() not in LEVELS:
            raise ValueError("JOURNAL_INVALID_LEVEL")
        where, params = ["sequence>?", "level>=?"], [after, LEVELS[min_level.upper()]]
        for name, value in (("stage", stage), ("task_id", task_id)):
            if value is not None:
                where.append(name + "=?")
                params.append(value)
        with self._connect() as db:
            rows = db.execute("SELECT body FROM events WHERE " + " AND ".join(where) +
                              " ORDER BY sequence LIMIT ?", (*params, limit + 1)).fetchall()
            latest = db.execute("SELECT coalesce(max(sequence),0) FROM events").fetchone()[0]
        events = [self.sanitize(json.loads(row[0])) for row in rows[:limit]]
        cursor = events[-1]["run_sequence"] if events else after
        return {"events": events, "next_after": cursor, "next_cursor": cursor,
                "latest_sequence": latest, "has_more": len(rows) > limit}

    def checkpoint(self, value: Any) -> dict[str, str]:
        ref = self.artifact("checkpoints/state.json", value)
        self.emit("INFO", "checkpoint", "checkpoint.saved", output_artifact_ref=ref,
                  output_hash=ref["sha256"], execution_status="persisted")
        return ref

    def load_checkpoint(self) -> Any:
        with self._connect() as db:
            row = db.execute("SELECT body FROM events WHERE json_extract(body,'$.event_type')="
                             "'checkpoint.saved' ORDER BY sequence DESC LIMIT 1").fetchone()
        return self.read_artifact(json.loads(row[0])["output_artifact_ref"]) if row else None

    def request_cancel(self) -> dict[str, Any]:
        # A durable event is the source of truth, avoiding event/state split commits.
        return self.emit("INFO", "control", "run.cancel_requested", execution_status="cancelling",
                         next_action="stop_new_dispatches_and_preserve_inflight_outcomes")

    def cancel_requested(self) -> bool:
        with self._connect() as db:
            return db.execute("SELECT 1 FROM events WHERE json_extract(body,'$.event_type')="
                              "'run.cancel_requested' LIMIT 1").fetchone() is not None

    def inspect_recovery(self) -> dict[str, Any]:
        """Read-only integrity inspection. Never promotes a file or resends a call."""
        with self._locked(), self._connect() as db:
            rows = db.execute("SELECT * FROM artifacts").fetchall()
            events = [json.loads(row[0]) for row in db.execute("SELECT body FROM events ORDER BY sequence")]
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            marker = db.execute("SELECT value FROM state WHERE key='projection_sequence'").fetchone()
            expected_manifest = self._manifest(db)
        issues: list[dict[str, Any]] = []
        registered = {row["path"] for row in rows}
        for row in rows:
            try:
                path = self._artifact_path(row["path"])
                state = ("missing" if not path.exists() else
                         "hash_mismatch" if _digest(path.read_bytes()) != row["sha256"] else
                         "uncommitted" if row["status"] != "available" else None)
            except (OSError, ValueError):
                state = "unreadable_or_unsafe"
            if state:
                issues.append({"path": row["path"], "state": state})
        orphans = [str(path.relative_to(self.run_dir)).replace("\\", "/")
                   for path in (self.run_dir / "artifacts").rglob("*")
                   if path.is_file() and path.relative_to(self.run_dir).as_posix() not in registered]
        calls: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for event in events:
            if event.get("call_id"):
                calls.setdefault((event["call_id"], event["attempt_id"]), []).append(event)
        unknown, awaiting = [], []
        for (call_id, attempt), records in calls.items():
            types = {event["event_type"].removeprefix("model.") for event in records}
            persisted = "response_persisted" in types
            item = {"call_id": call_id, "attempt_id": attempt,
                    "last_event": records[-1]["event_type"], "next_action": "inspect_saved_evidence_do_not_resend"}
            if "dispatch_started" in types and not persisted:
                unknown.append(item)
            elif persisted and not ({"validated", "rejected"} & types):
                awaiting.append(item)
        projection_issues = []
        latest = events[-1]["run_sequence"] if events else 0
        if latest and (marker is None or int(marker[0]) != latest):
            projection_issues.append("projection_sequence_incomplete")
        expected = {"events.jsonl": "".join(_json(item) + "\n" for item in events),
                    "run.log": "".join(self._log_line(item) for item in events)}
        for name in ("events.jsonl", "run.log"):
            path = self.run_dir / name
            if latest and (not path.exists() or (path.stat().st_size and not path.read_bytes().endswith(b"\n"))):
                projection_issues.append(f"{name}:missing_or_partial")
            elif path.exists() and path.read_bytes() != expected[name].encode("utf-8"):
                projection_issues.append(f"{name}:content_mismatch")
        try:
            if json.loads((self.run_dir / "manifest.json").read_bytes()) != expected_manifest:
                projection_issues.append("manifest.json:content_mismatch")
        except (OSError, ValueError, UnicodeError):
            projection_issues.append("manifest.json:missing_or_partial")
        safe = integrity == "ok" and not (unknown or awaiting or issues or orphans or projection_issues or self._dispatch_blocked)
        return self.sanitize({"schema_version": "run_journal_recovery_v1", "run_id": self.run_id,
                              "sqlite_integrity": integrity, "provider_outcome_unknown": unknown,
                              "persisted_unvalidated": awaiting, "incomplete_artifacts": issues,
                              "orphan_artifacts": orphans, "projection_issues": projection_issues,
                              "safe_to_resume": safe, "can_auto_resume": safe,
                              "automatic_resend_allowed": False,
                              "dispatch_blocked": self._dispatch_blocked,
                              "cancel_requested": self.cancel_requested()})


@contextmanager
def journal_scope(journal: RunJournal, task_id: str | None = None,
                  stage: str = "model") -> Iterator[RunJournal]:
    token = _SCOPE.set((journal, task_id, stage))
    try:
        yield journal
    finally:
        _SCOPE.reset(token)


_SCOPE: ContextVar[tuple[RunJournal, str | None, str] | None] = ContextVar("run_journal_scope", default=None)
_CALL: ContextVar[JournalModelCall | None] = ContextVar("run_journal_model_call", default=None)


class JournalModelCall:
    """A single HTTP attempt. No implicit retry or provider-success inference."""

    def __init__(self, journal: RunJournal, task_id: str | None, stage: str,
                 metadata: dict[str, Any]):
        self.journal, self.task_id, self.stage = journal, task_id, stage
        self.call_id, self.attempt_id = uuid.uuid4().hex, uuid.uuid4().hex
        self.base = f"calls/{self.call_id}/{self.attempt_id}"
        self.started = time.monotonic()
        self.dispatched = self.persisted = self.received = False
        self.stream_sequence = 0
        request_ref = journal.artifact(self.base + "/request.json", metadata)
        self.event("prepared", input_artifact_ref=request_ref, input_hash=request_ref["sha256"],
                   request_model=metadata.get("model"), provider=metadata.get("provider"),
                   actual_provider="unknown", provider_reported_cost=None, estimated_cost=None)
        self._token = _CALL.set(self)

    def event(self, name: str, level: str = "INFO", **fields: Any) -> dict[str, Any]:
        return self.journal.emit(level, self.stage, "model." + name, task_id=self.task_id,
                                 call_id=self.call_id, attempt_id=self.attempt_id,
                                 duration_ms=round((time.monotonic() - self.started) * 1000, 3), **fields)

    def dispatch(self) -> None:
        if self.journal.cancel_requested():
            self.event("cancelled_before_dispatch", execution_status="cancelled")
            raise JournalPersistenceError("JOURNAL_CANCEL_REQUESTED")
        self.event("dispatch_started", execution_status="running")
        self.dispatched = True

    def response_received(self, status_code: int | None, headers: Any) -> None:
        self.event("response_received", status_code=status_code, response_headers=headers,
                   execution_status="receiving")
        self.received = True

    def persist_response(self, value: Any, **fields: Any) -> None:
        ref = self.journal.artifact(self.base + "/response.json", value)
        self.event("response_persisted", output_artifact_ref=ref, output_hash=ref["sha256"],
                   execution_status="persisted", **fields)
        self.persisted = True

    def stream_event(self, value: Any) -> None:
        self.stream_sequence += 1
        ref = self.journal.artifact(self.base + f"/stream/{self.stream_sequence:08d}.json", value)
        self.event("stream_event", "TRACE", stream_sequence=self.stream_sequence,
                   output_artifact_ref=ref, output_hash=ref["sha256"])

    def finish(self, metadata: dict[str, Any], error: str | None = None) -> None:
        try:
            ref = self.journal.artifact(self.base + "/metadata.json", metadata)
            if error:
                state = "provider_outcome_unknown" if self.dispatched and not self.persisted else "rejected"
                self.event(state, "ERROR", error_code=error, output_artifact_ref=ref,
                           execution_status="failed", retryable=False,
                           next_action="inspect_saved_evidence_do_not_resend" if state == "provider_outcome_unknown" else "inspect_protocol_error")
            else:
                self.event("validated", output_artifact_ref=ref, execution_status="succeeded",
                           response_model=metadata.get("model"), usage=metadata.get("usage"),
                           finish_reason=metadata.get("finish_reason"), actual_provider="unknown",
                           provider_reported_cost=None, estimated_cost=None)
        finally:
            self.close()

    def close(self) -> None:
        if self._token is not None:
            _CALL.reset(self._token)
            self._token = None


def start_model_call(metadata: dict[str, Any], secrets: Iterable[str] = ()) -> JournalModelCall | None:
    scope = _SCOPE.get()
    if scope is None:
        return None
    journal, task_id, stage = scope
    if journal._dispatch_blocked:
        raise JournalPersistenceError("JOURNAL_DISPATCH_BLOCKED_AFTER_PERSISTENCE_FAILURE")
    journal.add_secrets(secrets)
    return JournalModelCall(journal, task_id, stage, metadata)


def journal_stream_event(value: Any) -> None:
    call = _CALL.get()
    if call is not None:
        call.stream_event(value)

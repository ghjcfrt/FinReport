"""Replay an exported grounded run offline, without PDF, database or model access.

Usage: uv run --no-sync python tools/replay-grounded-analysis.py --input run.json
An optional --output creates a new JSON receipt and never replaces a file.
Successful replay proves frozen program reconstruction, not financial accuracy
or reproducibility of a new model invocation. A faithfully recorded rejected
model response can itself replay successfully.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from finance_adapters.persistence.models import AnalysisRun
from finance_adapters.providers.grounded_analysis import (
    GROUNDED_REPLAY_VERSIONS,
    replay_grounded_analysis,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

RECEIPT_VERSION = "grounded_analysis_replay_cli_v1"


class ReplayCliError(ValueError):
    """A stable, content-free CLI diagnostic."""


class GroundedReplayExport(BaseModel):
    """Only fields used by replay are mapped onto the detached ORM object.

    Other exported metadata, including fixture notices, is deliberately ignored;
    no unknown field is passed into SQLAlchemy's constructor.
    """

    model_config = ConfigDict(extra="ignore", strict=True)
    id: int = Field(gt=0)
    filing_version_id: int = Field(gt=0)
    status: str = Field(min_length=1)
    orchestration_version: str = Field(min_length=1)
    input_snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    tool_trace_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    tool_trace: dict
    output: dict | None
    invalidated_at: datetime | None = None

    @field_validator("invalidated_at", mode="before")
    @classmethod
    def parse_timestamp(cls, value):
        return datetime.fromisoformat(value) if isinstance(value, str) else value


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ReplayCliError("GROUNDED_REPLAY_ARGUMENTS_INVALID")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReplayCliError("GROUNDED_REPLAY_JSON_INVALID")
        result[key] = value
    return result


def _nonfinite(value):
    raise ReplayCliError("GROUNDED_REPLAY_JSON_INVALID")


def replay_grounded_export(payload: dict) -> dict:
    """Map a saved run onto a detached AnalysisRun and rebuild it locally."""
    try:
        exported = GroundedReplayExport.model_validate(payload)
    except ValidationError as exc:
        raise ReplayCliError("GROUNDED_REPLAY_EXPORT_INVALID") from exc
    if exported.orchestration_version not in GROUNDED_REPLAY_VERSIONS:
        raise ReplayCliError("GROUNDED_REPLAY_PROTOCOL_UNREGISTERED")
    frozen_document = exported.tool_trace.get("input_snapshot", {}).get("document", {})
    if frozen_document.get("filing_version_id") != exported.filing_version_id:
        raise ReplayCliError("GROUNDED_REPLAY_FILING_MISMATCH")
    run = AnalysisRun(**exported.model_dump())
    result = replay_grounded_analysis(run)
    return {**result, "schema_version": RECEIPT_VERSION, "network_used": False,
            "run_id": exported.id, "recorded_run_status": exported.status,
            "orchestration_version": exported.orchestration_version,
            "proof_scope": "frozen_program_reconstruction_not_financial_accuracy"}


def _rejected(code):
    return {"schema_version": RECEIPT_VERSION, "status": "rejected", "error": code,
            "model_called": False, "database_used": False, "network_used": False}


def main(argv=None) -> int:
    parser = JsonArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Exported run.json")
    parser.add_argument("--output", type=Path, help="New receipt file; existing paths are rejected")
    output_path = None
    exit_code = 0
    try:
        args = parser.parse_args(argv)
        source_path = args.input.resolve()
        if args.output is not None:
            proposed = args.output.resolve()
            if proposed == source_path:
                raise ReplayCliError("GROUNDED_REPLAY_OUTPUT_IS_INPUT")
            if args.output.exists() or args.output.is_symlink():
                raise ReplayCliError("GROUNDED_REPLAY_OUTPUT_EXISTS")
            output_path = proposed
        try:
            content = source_path.read_bytes()
        except OSError as exc:
            raise ReplayCliError("GROUNDED_REPLAY_INPUT_UNREADABLE") from exc
        try:
            payload = json.loads(content.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                                 parse_constant=_nonfinite)
        except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ReplayCliError("GROUNDED_REPLAY_JSON_INVALID") from exc
        result = replay_grounded_export(payload)
        result["export_sha256"] = hashlib.sha256(content).hexdigest()
    except ReplayCliError as exc:
        result, exit_code = _rejected(str(exc)), 2
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, RecursionError) as exc:
        # Never echo arbitrary source/model text or local paths from exceptions.
        code = str(exc)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,127}", code):
            code = "GROUNDED_REPLAY_TRACE_INVALID"
        result, exit_code = _rejected(code), 1
    except OSError:
        result, exit_code = _rejected("GROUNDED_REPLAY_INPUT_UNREADABLE"), 2
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if output_path is not None:
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with output_path.open("x", encoding="utf-8") as handle:
                handle.write(encoded)
        except FileExistsError:
            result, exit_code = _rejected("GROUNDED_REPLAY_OUTPUT_EXISTS"), 2
        except OSError:
            result, exit_code = _rejected("GROUNDED_REPLAY_OUTPUT_UNWRITABLE"), 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the versioned workflow and inspect its journal without starting another UI."""
from __future__ import annotations

import argparse
import json
import logging

from finance_adapters.persistence.database import SessionLocal, init_database
from finance_adapters.persistence.models import AnalysisRun
from finance_adapters.providers.chapter_analysis import (
    CHAPTER_ANALYSIS_VERSION,
    chapter_journal,
    create_chapter_analysis,
    replay_chapter_analysis,
    run_chapter_analysis,
)
from sqlalchemy import update


def main() -> int:
    journal_logger = logging.getLogger("finreport.run_journal")
    journal_logger.setLevel(logging.INFO)
    if not journal_logger.handlers:
        journal_logger.addHandler(logging.StreamHandler())
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("filing_id", type=int)
    run.add_argument("--documents", nargs="+", type=int)
    run.add_argument("--max-model-calls", type=int, default=40)
    run.add_argument("--max-wall-seconds", type=float, default=900)
    run.add_argument("--resume", type=int)
    for name in ("events", "replay", "cancel"):
        sub = commands.add_parser(name)
        sub.add_argument("run_id", type=int)
        if name == "events":
            sub.add_argument("--after", type=int, default=0)
            sub.add_argument("--level", choices=["TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], default="INFO")
    args = parser.parse_args()
    init_database()
    with SessionLocal() as session:
        if args.command == "run":
            record = create_chapter_analysis(session, args.filing_id, args.documents,
                limits={"max_model_calls": args.max_model_calls, "max_wall_seconds": args.max_wall_seconds},
                previous_run_id=args.resume)
            print(f"analysis_run_id={record.id}", flush=True)
            run_chapter_analysis(session, record.id)
            value = {"analysis_run_id": record.id, "status": record.status,
                     "failure_reason": record.failure_reason, "output": record.output}
        else:
            record = session.get(AnalysisRun, args.run_id)
            if record is None or record.orchestration_version != CHAPTER_ANALYSIS_VERSION:
                parser.error("CHAPTER_ANALYSIS_NOT_FOUND")
            journal = chapter_journal(record.id)
            if args.command == "replay":
                value = replay_chapter_analysis(record)
            elif args.command == "cancel":
                journal.request_cancel()
                session.execute(update(AnalysisRun).where(
                    AnalysisRun.id == record.id, AnalysisRun.status == "queued").values(status="cancelled"))
                session.commit()
                value = {"cancellation_requested": True, "analysis_run_id": record.id}
            else:
                value = journal.read_events(after=args.after, limit=1000, min_level=args.level)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 1 if value.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())

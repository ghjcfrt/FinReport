"""Versioned database boundary for chapter analysis; legacy runs stay untouched."""
from __future__ import annotations

import hashlib
import platform
import re
import subprocess
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pymupdf
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from finance_adapters.config import settings
from finance_adapters.persistence.audit import stable_hash
from finance_adapters.persistence.models import (
    AnalysisRun,
    EvidenceFragment,
    ExtractionJob,
    FilingVersion,
)
from finance_adapters.providers import evidence_retrieval as retrieval
from finance_adapters.security.file_gate import scanned_object_path

CHAPTER_ANALYSIS_VERSION = "chapter-evidence-analysis-v1"
_HEADING = re.compile(r"^(?:第[一二三四五六七八九十百\d]+[章节部]|[一二三四五六七八九十]+、).{2,60}$")


class ChapterConfiguredClient:
    """Honor the gateway format without weakening the local role contracts."""

    def __init__(self, client, response_mode: str):
        if response_mode not in {"json_object", "json_schema"}:
            raise ValueError("CHAPTER_RESPONSE_MODE_UNSUPPORTED")
        self.client, self.response_mode, self.config = client, response_mode, client.config

    def complete(self, *, system: str, user: str, response_schema: dict):
        if self.response_mode == "json_object":
            return self.client.complete(system=system, user=user, response_json=True)
        return self.client.complete(system=system, user=user, response_schema=response_schema)


def chapter_journal(run_id: int):
    from finance_adapters.providers.run_journal import RunJournal
    return RunJournal(settings.storage_dir / "runs", f"chapter-{run_id}",
                      secrets=[settings.llm_api_key or "", settings.ocr_api_key or ""])


def _request_profile() -> dict:
    return {"response_mode": settings.llm_structured_output_mode,
            "schema_builder": settings.llm_schema_builder, "provider": settings.llm_provider,
            "endpoint_sha256": stable_hash(settings.llm_base_url), "model": settings.llm_model,
            "stream": settings.llm_stream, "thinking_enabled": settings.llm_enable_thinking,
            "reasoning_effort": settings.llm_reasoning_effort, "provider_sort": settings.llm_provider_sort,
            "timeout_seconds": settings.llm_timeout_seconds}


def _selected_documents(session: Session, filing_id: int, selected: list[int] | None):
    anchor = session.get(FilingVersion, filing_id)
    if anchor is None:
        raise ValueError("FILING_NOT_FOUND")
    ids = {filing_id, *(selected or [])}
    if selected is not None and (not selected or len(set(selected)) != len(selected)
            or any(type(i) is not int or i <= 0 for i in selected)):
        raise ValueError("CHAPTER_DOCUMENT_IDS_INVALID")
    documents = list(session.scalars(select(FilingVersion).where(
        FilingVersion.id.in_(ids), FilingVersion.company_id == anchor.company_id,
        FilingVersion.scope == anchor.scope,
        FilingVersion.accounting_standard == anchor.accounting_standard,
    ).order_by(FilingVersion.id)))
    if {doc.id for doc in documents} != ids:
        raise ValueError("CHAPTER_DOCUMENT_NOT_AUTHORIZED")
    return documents


def _automatic_facts(session: Session, filing: FilingVersion) -> tuple[dict[int, list], str | None]:
    from finance_adapters.persistence.auto_validation_gateway import (
        auto_validated_facts,
        uses_automatic_policy,
    )
    if not uses_automatic_policy(filing):
        return {}, "AUTO_FACT_POLICY_NOT_SELECTED"
    try:
        facts = [fact for comparative in (False, True)
                 for fact in auto_validated_facts(session, filing, comparative=comparative).values()]
    except (ValueError, OSError) as exc:
        return {}, str(exc).split(":", 1)[0]
    pages: dict[int, list] = {}
    for fact in facts:
        item = {"fact_id": str(fact.id), "concept_code": fact.concept_code,
                "label": fact.concept_name, "value": str(fact.value), "status": "auto_passed",
                "raw_value": fact.raw_value, "unit_scale": str(fact.unit_scale),
                "document_id": str(filing.id), "unit": fact.currency, "currency": fact.currency,
                "period_start": fact.period_start.isoformat(), "period_end": fact.period_end.isoformat(),
                "scope": filing.scope, "period_basis": filing.period_basis,
                "comparative_period": fact.comparative_period, "is_restatement": fact.is_restatement}
        evidence = list(session.scalars(select(EvidenceFragment).where(EvidenceFragment.fact_id == fact.id)))
        for e in evidence:
            pages.setdefault(e.page_no, []).append({**deepcopy(item),
                "evidence_fragment_id": e.id, "raw_label": e.raw_label,
                "evidence_raw_value": e.raw_value})
    return pages, None


def build_chapter_input(session: Session, filing_id: int, selected: list[int] | None = None,
                        *, materialize: bool = True) -> dict:
    """Freeze authorized local corpus, retaining extraction gaps and strict fact admission."""
    filings = _selected_documents(session, filing_id, selected)
    documents, units = [], []
    for filing in filings:
        path = scanned_object_path(session, filing)
        job = session.scalar(select(ExtractionJob).where(
            ExtractionJob.filing_version_id == filing.id).order_by(ExtractionJob.id.desc()).limit(1))
        if job is None or job.status != "completed":
            raise ValueError("CHAPTER_COMPLETED_EXTRACTION_REQUIRED")
        if materialize:
            retrieval.materialize_document_text_chunks(session, filing.id)
        with pymupdf.open(path) as pdf:
            total_pages = pdf.page_count  # Metadata only, including on the OCR path.
        expected = sorted(job.page_numbers) if job.page_numbers is not None else list(range(1, total_pages + 1))
        text_snapshot = retrieval.snapshot(session, [filing.id])["documents"][0]
        chunks = [c for c in text_snapshot["chunks"]
                  if c["page_no"] in expected and c.get("review_status") != "rejected"
                  and (c.get("extraction_method") == "ocr" if job.extraction_method == "ocr"
                       else c.get("extraction_method") in {"pdf_text", "docling"})]
        facts, fact_failure = _automatic_facts(session, filing)
        present = {c["page_no"] for c in chunks}
        doc = {"id": str(filing.id), "document_id": str(filing.id), "filing_version_id": filing.id,
               "company_id": filing.company_id, "report_type": filing.report_type,
               "period_start": filing.period_start.isoformat(), "period_end": filing.period_end.isoformat(),
               "period_basis": filing.period_basis, "scope": filing.scope,
               "accounting_standard": filing.accounting_standard, "source_sha256": filing.file_sha256,
               "extraction_method": job.extraction_method, "extraction_job_id": job.id,
               "fact_validation_policy": filing.fact_validation_policy,
               "strict_fact_failure": fact_failure,
               "coverage": {"total_pages": total_pages, "expected_pages": expected,
                            "available_pages": sorted(present), "pages_without_material": sorted(set(expected)-present),
                            "unselected_pages": sorted(set(range(1, total_pages+1))-set(expected)),
                            "full_document_selected": job.page_numbers is None}}
        documents.append(doc)
        chapter = f"{filing.id}:unclassified"
        chapter_title = "未确定章节（按原页组织）"
        bound_facts = set()
        page_context: dict[int, list[str]] = {}
        for chunk in chunks:
            page_context.setdefault(chunk["page_no"], []).append(
                chunk.get("source_id") or f"text-chunk:{chunk['id']}")
        for chunk in chunks:
            headings = [line.strip() for line in chunk["text"].splitlines()
                        if _HEADING.match(line.strip())]
            if headings:
                chapter_title = headings[0]
                chapter = f"{filing.id}:section:{hashlib.sha256(chapter_title.encode()).hexdigest()[:12]}"
            unit_id = chunk.get("source_id") or f"text-chunk:{chunk['id']}"
            metadata = {k: v for k, v in doc.items() if k not in {"coverage", "id", "document_id"}}
            attached = []
            for fact in facts.get(chunk["page_no"], []):
                if (fact["fact_id"] not in bound_facts and fact.get("raw_label")
                        and fact["raw_label"] in chunk["text"] and fact.get("evidence_raw_value")
                        and fact["evidence_raw_value"] in chunk["text"]):
                    attached.append({**fact, "source_ids": [unit_id]})
                    bound_facts.add(fact["fact_id"])
            metadata.update(extractor_version=chunk.get("extractor_version"),
                            page_sha256=chunk.get("page_sha256"),
                            source_review_status=chunk.get("review_status"),
                            section_assignment="heading_candidate" if headings else "inherited_or_unknown",
                            boundary_verified=False, question_completeness=chunk.get("question_completeness"),
                            required_context_ids=page_context[chunk["page_no"]],
                            context_policy="whole_selected_source_page_v1",
                            facts=attached)
            units.append({"unit_id": unit_id, "document_id": str(filing.id), "chapter_id": chapter,
                          "title": chapter_title, "text": chunk["text"], "page": chunk["page_no"],
                          "locator": {"chunk_id": chunk["id"], "page": chunk["page_no"],
                                      "bbox": chunk.get("bounding_box")}, "metadata": metadata,
                          "content_hash": chunk["text_sha256"]})
    if not units:
        raise ValueError("CHAPTER_SEARCHABLE_MATERIAL_REQUIRED")
    return {"version": CHAPTER_ANALYSIS_VERSION, "anchor_document_id": str(filing_id),
            "selection_mode": "independent" if selected is None else "explicit_joint",
            "selected_document_ids": [f.id for f in filings], "documents": documents, "units": units}


def _provenance() -> dict:
    root = next((p for p in Path(__file__).resolve().parents if (p / "uv.lock").is_file()), None)
    result = {"python_version": platform.python_version(), "code_commit": None,
              "dirty_diff_sha256": None, "dependency_lock_sha256": None}
    if root is None:
        return result
    result["dependency_lock_sha256"] = hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest()
    try:
        result["code_commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, timeout=10).decode().strip()
        diff = subprocess.check_output(["git", "diff", "HEAD", "--binary"], cwd=root, timeout=10)
        untracked = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=root, timeout=10)
        digests = []
        for raw in sorted(filter(None, untracked.split(b"\0"))):
            p = root / raw.decode("utf-8")
            if p.is_file() and not p.is_symlink():
                digests.append([raw.decode("utf-8"), hashlib.sha256(p.read_bytes()).hexdigest()])
        result["dirty_diff_sha256"] = stable_hash({"diff_sha256": hashlib.sha256(diff).hexdigest(), "untracked": digests})
    except (OSError, subprocess.SubprocessError):
        result["provenance_status"] = "partial"
    return result


def _import_checkpoint_artifacts(source, target, checkpoint: dict) -> dict:
    """Make an authorized parent checkpoint self-contained in its new run.

    Read only parent-registered, hash-matching artifacts. Nested references are
    imported before their enclosing artifact; the parent is never modified.
    """
    imported: dict[tuple[str, str], dict] = {}
    visiting: set[tuple[str, str]] = set()

    def copy(value):
        if isinstance(value, list):
            return [copy(item) for item in value]
        if not isinstance(value, dict):
            return value
        if (set(value) == {"path", "sha256"} and isinstance(value["path"], str)
                and isinstance(value["sha256"], str)):
            identity = value["path"], value["sha256"]
            if identity in visiting:
                raise ValueError("CHAPTER_RESUME_ARTIFACT_CYCLE")
            if identity not in imported:
                visiting.add(identity)
                content = copy(source.read_artifact(value))
                imported[identity] = target.artifact(f"resume/imported/{identity[1]}.json", content)
                visiting.remove(identity)
            return imported[identity]
        return {key: copy(item) for key, item in value.items()}

    restored = copy(checkpoint)
    target.emit("INFO", "resume", "resume.artifacts_imported", parent_run_id=source.run_id,
                artifact_count=len(imported), input_hash=stable_hash(checkpoint),
                output_hash=stable_hash(restored))
    return restored


def create_chapter_analysis(session: Session, filing_id: int, selected: list[int] | None = None,
                            *, limits: dict | None = None, previous_run_id: int | None = None) -> AnalysisRun:
    from finance_adapters.providers.chapter_workflow import WorkflowLimits
    effective = WorkflowLimits(**(limits or {})).__dict__
    package = build_chapter_input(session, filing_id, selected)
    previous_ref = None
    if previous_run_id is not None:
        previous = session.get(AnalysisRun, previous_run_id)
        if previous is None or previous.orchestration_version != CHAPTER_ANALYSIS_VERSION:
            raise ValueError("CHAPTER_PREVIOUS_RUN_INVALID")
        if previous.status not in {"failed", "cancelled", "partial_result", "waiting_for_documents"} or previous.filing_version_id != filing_id:
            raise ValueError("CHAPTER_PREVIOUS_RUN_NOT_RESUMABLE")
        if previous.invalidated_at is not None or previous.input_snapshot_hash != stable_hash(package):
            raise ValueError("CHAPTER_PREVIOUS_INPUT_CHANGED")
        if previous.tool_trace.get("request_profile") != _request_profile():
            raise ValueError("CHAPTER_PREVIOUS_REQUEST_PROFILE_CHANGED")
        recovery = chapter_journal(previous.id).inspect_recovery()
        if not recovery.get("safe_to_resume", False):
            raise ValueError("CHAPTER_PROVIDER_OUTCOME_UNKNOWN_OR_LOG_INCOMPLETE")
        old_status = previous.status
        claimed = session.execute(update(AnalysisRun).where(
            AnalysisRun.id == previous.id, AnalysisRun.status == old_status,
            AnalysisRun.invalidated_at.is_(None)).values(status="continued"))
        if claimed.rowcount != 1:
            raise ValueError("CHAPTER_PREVIOUS_ALREADY_RESUMED")
        previous_ref = previous_run_id
    trace = {"version": CHAPTER_ANALYSIS_VERSION, "input_snapshot": package,
             "limits": effective, "previous_run_id": previous_ref,
             "request_profile": _request_profile(),
             "created_at": datetime.now(UTC).isoformat()}
    run = AnalysisRun(filing_version_id=filing_id, input_snapshot_hash=stable_hash(package),
                      orchestration_version=CHAPTER_ANALYSIS_VERSION, prompt_version="chapter-prompts-v1",
                      validator_version="chapter-evidence-validator-v1", status="initializing",
                      tool_trace=trace, tool_trace_hash=stable_hash(trace))
    session.add(run)
    # Reserve the database ID before filesystem work. A failed initialization
    # must not leave an orphan journal that a rolled-back ID could later reuse.
    session.commit()
    try:
        journal = chapter_journal(run.id)
        journal.update_manifest({"version": CHAPTER_ANALYSIS_VERSION, "database_run_id": run.id,
            "input_hash": run.input_snapshot_hash,
            "selected_document_ids": package["selected_document_ids"], **_provenance()})
        ref = journal.artifact("input/snapshot.json", package)
        journal.emit("INFO", "import", "run.queued", input_artifact_ref=ref,
                     input_hash=run.input_snapshot_hash, previous_run_id=previous_ref, limits=effective)
        run.status = "queued"
        session.commit()
    except Exception:  # noqa: BLE001 - keep the reserved ID after storage failure
        session.rollback()
        run = session.get(AnalysisRun, run.id)
        run.status, run.failure_reason = "failed", "CHAPTER_JOURNAL_INITIALIZATION_FAILED"
        session.commit()
        from finance_adapters.providers.run_journal import JournalPersistenceError
        raise JournalPersistenceError("CHAPTER_JOURNAL_INITIALIZATION_FAILED") from None
    return run


def run_chapter_analysis(session: Session, run_id: int) -> None:
    from finance_adapters.providers.chapter_workflow import run_chapter_workflow
    from finance_adapters.providers.model_clients import ModelClient, ModelConfig
    run = session.get(AnalysisRun, run_id)
    if run is None or run.orchestration_version != CHAPTER_ANALYSIS_VERSION or run.status != "queued":
        return
    claimed = session.execute(update(AnalysisRun).where(
        AnalysisRun.id == run_id, AnalysisRun.status == "queued").values(status="running"))
    if claimed.rowcount != 1:
        session.rollback()
        return
    session.commit()
    journal = None
    trace = deepcopy(run.tool_trace or {})
    try:
        journal = chapter_journal(run_id)
        if stable_hash(trace) != run.tool_trace_hash:
            raise ValueError("CHAPTER_TRACE_HASH_MISMATCH")
        package = trace["input_snapshot"]
        if stable_hash(package) != run.input_snapshot_hash:
            raise ValueError("CHAPTER_INPUT_HASH_MISMATCH")
        selected = package["selected_document_ids"] if package["selection_mode"] == "explicit_joint" else None
        try:
            current = build_chapter_input(session, run.filing_version_id, selected, materialize=False)
        except (ValueError, OSError):
            raise ValueError("CHAPTER_SOURCE_CHANGED") from None
        if current != package:
            raise ValueError("CHAPTER_SOURCE_CHANGED")
        if not settings.llm_base_url or not settings.llm_api_key or not settings.llm_model:
            raise ValueError("LLM_CONFIGURATION_REQUIRED")
        profile = trace["request_profile"]
        if profile != _request_profile():
            raise ValueError("CHAPTER_MODEL_CONFIGURATION_CHANGED")
        client = ChapterConfiguredClient(ModelClient(ModelConfig(provider=settings.llm_provider, base_url=settings.llm_base_url,
            api_key=settings.llm_api_key, model=settings.llm_model, timeout=settings.llm_timeout_seconds,
            enable_thinking=settings.llm_enable_thinking, reasoning_effort=settings.llm_reasoning_effort,
            stream=settings.llm_stream, provider_sort=settings.llm_provider_sort,
            schema_builder=profile["schema_builder"])), profile["response_mode"])
        previous = None
        if trace.get("previous_run_id"):
            parent_journal = chapter_journal(trace["previous_run_id"])
            if not parent_journal.inspect_recovery().get("safe_to_resume", False):
                raise ValueError("CHAPTER_PREVIOUS_LOG_NOT_SAFE")
            checkpoint = parent_journal.load_checkpoint()
            if checkpoint is not None:
                previous = _import_checkpoint_artifacts(parent_journal, journal, checkpoint)
        journal.emit("INFO", "planning", "run.started", input_hash=run.input_snapshot_hash)
        output = journal.sanitize(run_chapter_workflow(package["units"], package["documents"], client, journal,
                                      limits=trace["limits"], previous=previous))
        session.refresh(run)
        if run.invalidated_at is not None or build_chapter_input(
                session, run.filing_version_id, selected, materialize=False) != package:
            raise ValueError("CHAPTER_SOURCE_CHANGED_DURING_RUN")
        trace["result_artifact"] = journal.artifact("report/result.json", output)
        trace["output_hash"] = stable_hash(output)
        run.output = output
        run.status = output.get("status", "partial_result")
        run.model = run.model_version = settings.llm_model
        run.failure_reason = None
        journal.emit("INFO", "export", "run.finished", execution_status=run.status,
                     output_hash=trace["output_hash"], output_artifact_ref=trace["result_artifact"])
    except Exception as exc:  # noqa: BLE001 - persist background failure, not a financial judgment
        session.rollback()
        run = session.get(AnalysisRun, run_id)
        error = journal.sanitize(str(exc)) if journal is not None else "CHAPTER_JOURNAL_UNAVAILABLE"
        run.status, run.failure_reason = "failed", error
        trace["error"] = error
        trace["finished_at"] = datetime.now(UTC).isoformat()
        run.tool_trace, run.tool_trace_hash = trace, stable_hash(trace)
        # DB failure status is authoritative even if the error event cannot be
        # persisted. Journal itself records its independent emergency notice.
        session.commit()
        if journal is not None:
            try:
                journal.emit("ERROR", "execution", "run.failed", error_code=type(exc).__name__, message=error)
            except Exception:  # noqa: BLE001, S110 - emergency audit belongs to the failed journal
                pass
        return
    trace["finished_at"] = datetime.now(UTC).isoformat()
    run.tool_trace, run.tool_trace_hash = trace, stable_hash(trace)
    session.commit()


def replay_chapter_analysis(run: AnalysisRun) -> dict:
    """Read-only saved-artifact validation; never calls a provider."""
    trace = run.tool_trace or {}
    if run.orchestration_version != CHAPTER_ANALYSIS_VERSION or stable_hash(trace) != run.tool_trace_hash:
        raise ValueError("CHAPTER_TRACE_HASH_MISMATCH")
    if stable_hash(trace.get("input_snapshot")) != run.input_snapshot_hash:
        raise ValueError("CHAPTER_INPUT_HASH_MISMATCH")
    journal = chapter_journal(run.id)
    recovery = journal.inspect_recovery()
    if not recovery.get("safe_to_resume", False):
        return {"status": "audit_integrity_failed", "provider_called": False,
                "publication_allowed": False, "recovery": recovery}
    if run.output is None:
        return {"status": "incomplete", "provider_called": False, "recovery": recovery}
    saved = journal.read_artifact(trace["result_artifact"])
    if saved != run.output or stable_hash(saved) != trace.get("output_hash"):
        raise ValueError("CHAPTER_OUTPUT_HASH_MISMATCH")
    from finance_adapters.providers.chapter_workflow import validate_chapter_result
    package = trace["input_snapshot"]
    issues = validate_chapter_result(saved, package["units"], documents=package["documents"])
    return {"status": "saved_artifacts_match", "provider_called": False,
            "publication_allowed": not issues, "publication_issues": issues,
            "external_model_regeneration_verified": False, "output_hash": trace["output_hash"],
            "input_hash": run.input_snapshot_hash, "recovery": recovery}

"""Versioned, bounded chapter/evidence workflow.

The model can request data, but cannot execute code, promote facts, write receipts,
or publish unchecked prose.  All I/O crosses a journal checkpoint.  This module
is deliberately separate from the historical evidence-agent replay policy.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

WORKFLOW_VERSION = "chapter-evidence-workflow-v1"
DEFAULT_LIMITS = {
    "max_model_calls": 40,
    "max_task_rounds": 4,
    "max_dynamic_tasks": 12,
    "max_tools_per_round": 6,
    "max_context_chars": 24000,
    "max_unit_chars": 10000,
    "max_task_units": 6,
    "verification_batch_size": 6,
    "max_repair_rounds": 1,
    "max_wall_seconds": 900,
}


@dataclass(frozen=True)
class WorkflowLimits:
    max_model_calls: int = 40
    max_task_rounds: int = 4
    max_dynamic_tasks: int = 12
    max_tools_per_round: int = 6
    max_context_chars: int = 24000
    max_unit_chars: int = 10000
    max_task_units: int = 6
    verification_batch_size: int = 6
    max_repair_rounds: int = 1
    max_wall_seconds: float = 900

    def __post_init__(self):
        for key, value in self.__dict__.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"CHAPTER_INVALID_LIMIT:{key}")
            if not math.isfinite(value) or (key != "max_wall_seconds" and type(value) is not int):
                raise ValueError(f"CHAPTER_INVALID_LIMIT:{key}")
        if self.max_context_chars < 3000:
            raise ValueError("CHAPTER_CONTEXT_LIMIT_TOO_SMALL")
FINANCIAL_TOPICS = (
    ("yoy_qoq", "同比与环比：核对比较期间、累计转单季、重述、零或负基数及缺件。"),
    ("non_recurring", "非经常性损益及扣非：核对归属、税后口径、构成及利润影响。"),
    ("accounting_changes", "会计政策、估计变更与前期重述：核对可比性及可量化影响。"),
    ("consolidation", "合并范围变化：检查主体变化及对期间比较的限制。"),
    ("eps", "每股收益口径：核对基本/稀释、加权股数、期间及重述。"),
    ("going_concern", "持续经营：查找相关原文、条件及审计意见；无命中不证明不存在。"),
    ("profit_cash_divergence", "利润与经营现金流背离：计算观察与原因推断分开，跨章查应收、存货等。"),
    ("cashflow_reconciliation", "利润至经营现金流调节表：逐项检查调节及口径，不能以业务说明替代。"),
    ("cashflow_drivers", "经营现金流变动原因：区分公司解释、程序计算及尚未验证的因果。"),
)


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), default=str).encode()).hexdigest()


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return asdict(value)
    return value


def _merged_ranges(ranges: list[list[int]]) -> list[list[int]]:
    result: list[list[int]] = []
    for start, end in sorted(ranges):
        if end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1][1] = max(end, result[-1][1])
        else:
            result.append([start, end])
    return result


def _covers(ranges: list[list[int]], start: int, end: int) -> bool:
    return any(left <= start and right >= end for left, right in _merged_ranges(ranges))


def _raw_decimal(raw: str) -> Decimal:
    value = raw.strip().replace(",", "").replace("，", "").replace("−", "-")
    if (value.startswith("(") and value.endswith(")")) or (
            value.startswith("（") and value.endswith("）")):
        value = "-"+value[1:-1]
    return Decimal(value)


def _saved_verifier_role(state: dict) -> str:
    # Runs persisted before role selection was versioned used v1. Never infer
    # their policy from today's default, including before the first verifier call.
    role = state.get("verifier_role", "claim_evidence_verify_v1")
    if role not in {"claim_evidence_verify_v1", "claim_evidence_verify_v2"}:
        raise ValueError("CHAPTER_INVALID_VERIFIER_ROLE")
    return role


class _Stopped(Exception):
    def __init__(self, reason: str):
        self.reason = reason


class _ProtocolError(ValueError):
    pass


class _Engine:
    def __init__(self, units: list[dict], documents: list[dict], client: Any,
                 journal: Any, limits: dict | None, calculations: list[dict] | None,
                 previous: dict | None):
        from .chapter_contracts import EvidenceUnit
        from .chapter_prompts import DEFAULT_VERIFIER_ROLE

        self.journal = journal
        self.client = client
        self.verifier_role = _saved_verifier_role(previous) if previous else DEFAULT_VERIFIER_ROLE
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        for key, value in self.limits.items():
            if key not in DEFAULT_LIMITS or isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"CHAPTER_INVALID_LIMIT:{key}")
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"CHAPTER_INVALID_LIMIT:{key}")
            if key != "max_wall_seconds" and type(value) is not int:
                raise ValueError(f"CHAPTER_INVALID_LIMIT:{key}")
        if self.limits["max_context_chars"] < 3000:
            raise ValueError("CHAPTER_CONTEXT_LIMIT_TOO_SMALL")
        self.documents = {str(d.get("document_id") or d.get("id")): dict(d) for d in documents}
        if not self.documents or "None" in self.documents or len(self.documents) != len(documents):
            raise ValueError("CHAPTER_INVALID_DOCUMENT_SET")
        self.units = {}
        for raw in units:
            unit = EvidenceUnit.model_validate(raw)
            if unit.unit_id in self.units or unit.document_id not in self.documents:
                raise ValueError("CHAPTER_INVALID_SOURCE_IDENTITY")
            context_ids = unit.metadata.get("required_context_ids", [])
            if not isinstance(context_ids, list) or any(not isinstance(key, str) for key in context_ids):
                raise ValueError("CHAPTER_INVALID_CONTEXT_METADATA")
            if not isinstance(unit.metadata.get("facts", []), list):
                raise TypeError("CHAPTER_INVALID_FACT_METADATA")
            self.units[unit.unit_id] = unit
        from .chapter_contracts import CalculationRecord

        values = calculations.values() if isinstance(calculations, dict) else calculations or []
        self.calculations = {}
        for value in values:
            record = CalculationRecord.model_validate(_dump(value))
            self.calculations[record.calculation_id] = _dump(record)
        self.facts: dict[str, dict] = {}
        for unit in self.units.values():
            for fact in unit.metadata.get("facts", []):
                fact_id = str(fact.get("fact_id", ""))
                if not fact_id:
                    continue
                candidate = dict(fact)
                candidate.setdefault("document_id", unit.document_id)
                candidate.setdefault("source_ids", [unit.unit_id])
                if fact_id in self.facts and self.facts[fact_id] != candidate:
                    old = self.facts[fact_id]
                    if ({k: v for k, v in old.items() if k != "source_ids"}
                            != {k: v for k, v in candidate.items() if k != "source_ids"}):
                        raise ValueError("CHAPTER_DUPLICATE_FACT_ID")
                    candidate["source_ids"] = list(dict.fromkeys(old["source_ids"]+candidate["source_ids"]))
                self.facts[fact_id] = candidate
        self.started = time.monotonic()
        self.call_count = 0
        self.blocked_unknown = False
        self.profiles: dict[str, dict] = {}
        self.input_hash = _hash({
            "version": WORKFLOW_VERSION,
            "documents": documents,
            "units": [_dump(unit) for unit in self.units.values()],
            "calculations": list(self.calculations.values()),
        })
        self.state: dict[str, Any] = {
            "workflow_version": WORKFLOW_VERSION, "input_hash": self.input_hash,
            "verifier_role": self.verifier_role,
            "execution_status": "running", "documents": documents,
            "source_manifest": {key: unit.content_hash for key, unit in self.units.items()},
            "tasks": [], "claims": [], "receipts": [], "calculations": [],
            "material_ledger": {key: {"document_id": unit.document_id,
                "chapter_id": unit.chapter_id, "retrieved_by": [], "read_ranges": {},
                "checked_ranges": [], "dispositions": [], "referenced_by": [],
                "fully_checked": False} for key, unit in self.units.items()},
            "missing_documents": [], "errors": [], "limitations": [], "calls": [],
            "synthesis": None, "publication_errors": [], "publishable_claim_ids": [],
            "report_markdown": "", "prompt_profiles": {}, "limits": self.limits,
        }
        recovery = journal.inspect_recovery()
        self.integrity_blocked = bool(recovery.get("incomplete_artifacts")
            or recovery.get("orphan_artifacts") or recovery.get("integrity_errors"))
        if self.integrity_blocked:
            self.state["limitations"].append("运行工件完整性检查未通过，停止新增模型调用。")
        if recovery.get("provider_outcome_unknown"):
            self.blocked_unknown = True
            self.state["limitations"].append("存在已派发但响应未可靠保存的请求，禁止自动重发。")
        if previous:
            self._restore(previous)
        self.calculation_issues = (_rebuild_calculation_issues(list(self.calculations.values()),
            [_dump(unit) for unit in self.units.values()], list(self.documents.values()))
            if self.calculations else [])

    def _restore(self, previous: dict) -> None:
        if previous.get("workflow_version") != WORKFLOW_VERSION:
            raise ValueError("CHAPTER_RESUME_POLICY_MISMATCH")
        if previous.get("input_hash") != self.input_hash:
            raise ValueError("CHAPTER_RESUME_INPUT_CHANGED")
        # A new attempt may increase resource bounds, never silently change the
        # contracts or the frozen source identity.
        from .chapter_prompts import get_prompt_profile

        for role, saved in previous.get("prompt_profiles", {}).items():
            current = _dump(get_prompt_profile(role))
            if _hash(current) != _hash(saved):
                raise ValueError("CHAPTER_RESUME_PROMPT_CHANGED")
        self.state.update(json.loads(json.dumps(previous, ensure_ascii=False)))
        self.state["verifier_role"] = self.verifier_role
        self.calculations.update({c["calculation_id"]: c for c in previous.get("calculations", [])})
        self.state["limits"] = self.limits
        self.state["execution_status"] = "running"
        self.profiles = dict(self.state.get("prompt_profiles", {}))
        self.state["resumed_from_hash"] = _hash(previous)
        if any(call.get("status") in {"dispatch_started", "provider_outcome_unknown"}
               for call in self.state.get("calls", [])):
            self.blocked_unknown = True
        for task in self.state["tasks"]:
            if task["status"] in {"running", "partial"} and not self.blocked_unknown:
                task["status"] = "queued"
                task["rounds_this_attempt"] = 0
        self._event("INFO", "恢复", "workflow.resumed", input_hash=self.input_hash)

    def _event(self, level: str, stage: str, event: str, **fields: Any) -> dict:
        return self.journal.emit(level, stage, event, **fields)

    def _checkpoint(self) -> None:
        self.state["calculations"] = list(self.calculations.values())
        self.state["prompt_profiles"] = self.profiles or self.state["prompt_profiles"]
        self.state["model_calls_this_attempt"] = self.call_count
        self.journal.checkpoint(self.state)

    def _stop_check(self) -> None:
        if self.journal.cancel_requested():
            raise _Stopped("cancelled")
        if self.integrity_blocked:
            raise _Stopped("journal_integrity_failure")
        if self.calculation_issues:
            self.state["errors"].append({"stage": "calculation", "error_code": "CHAPTER_CALCULATION_INTEGRITY",
                                         "issues": self.calculation_issues})
            raise _Stopped("calculation_integrity_failure")
        if self.blocked_unknown:
            raise _Stopped("provider_outcome_unknown")
        if self.call_count >= self.limits["max_model_calls"]:
            raise _Stopped("model_call_budget")
        if time.monotonic() - self.started >= self.limits["max_wall_seconds"]:
            raise _Stopped("wall_time_budget")

    def _profile(self, role: str) -> tuple[Any, dict]:
        from .chapter_prompts import get_prompt_profile

        profile = get_prompt_profile(role)
        dumped = _dump(profile)
        self.profiles[role] = dumped
        self.journal.artifact(f"prompts/{role}-{_hash(dumped)}.json", dumped)
        return profile, dumped

    def _call(self, role: str, payload: dict, task: dict) -> dict:
        from .chapter_prompts import parse_role_output, render_prompt

        profile, profile_data = self._profile(role)
        system, user, prompt_meta = render_prompt(role, payload)
        if len(user) > self.limits["max_context_chars"]:
            raise _ProtocolError("CHAPTER_CONTEXT_BUDGET_EXCEEDED")
        request_hash = _hash([system, user])
        # A response persisted before a crash is parsed locally.  Never charge
        # again solely because the task acceptance checkpoint was not written.
        for old in reversed(self.state["calls"]):
            if (old.get("task_id") == task["task_id"] and old.get("role") == role
                    and old.get("request_hash") == request_hash and old.get("status") == "protocol_failed"):
                raise _ProtocolError(old.get("error_code", "CHAPTER_RESPONSE_PROTOCOL"))
            if (old.get("task_id") == task["task_id"] and old.get("role") == role
                    and old.get("request_hash") == request_hash and not old.get("consumed")
                    and old.get("status") in {"response_persisted", "validated"}):
                saved = self.journal.read_artifact(old["response_artifact"])
                saved_metadata = saved.get("metadata", {})
                if isinstance(saved_metadata, dict) and (
                        saved_metadata.get("finish_reason") in {"length", "max_tokens"}
                        or saved_metadata.get("stream_complete") is False):
                    old["status"] = "protocol_failed"
                    old["error_code"] = "CHAPTER_RESPONSE_TRUNCATED"
                    raise _ProtocolError("CHAPTER_RESPONSE_TRUNCATED")
                parsed = _dump(parse_role_output(role, saved["content"]))
                old["status"] = "validated"
                old["consumed"] = True
                self._record_presented(payload, task)
                self._event("INFO", role, "task.model_response_reused", task_id=task["task_id"],
                            call_id=old["call_id"])
                return parsed
        self._stop_check()
        call_id, attempt_id = uuid.uuid4().hex, uuid.uuid4().hex
        call = {"call_id": call_id, "attempt_id": attempt_id, "task_id": task["task_id"],
                "role": role, "status": "prepared", "request_hash": _hash([system, user])}
        schema = getattr(profile, "output_schema", None) or profile_data.get("output_schema")
        request = {"system": system, "user": user, "response_schema": schema,
                   "prompt_metadata": prompt_meta, "task_id": task["task_id"]}
        call["request_artifact"] = self.journal.artifact(
            f"calls/{call_id}/{attempt_id}/request.json", request)
        self.state["calls"].append(call)
        self._event("INFO", role, "task.model_prepared", **call,
                    input_artifact_ref=call["request_artifact"])
        self._checkpoint()
        call["status"] = "dispatch_started"
        self._event("INFO", role, "task.model_invocation_started", call_id=call_id,
                    attempt_id=attempt_id, task_id=task["task_id"])
        self._checkpoint()
        self.call_count += 1
        start = time.monotonic()
        try:
            from .run_journal import journal_scope

            with journal_scope(self.journal, task_id=task["task_id"], stage=role):
                content, metadata = self.client.complete(system=system, user=user, response_schema=schema)
        except BaseException as exc:
            metadata = getattr(exc, "metadata", {})
            has_response = (isinstance(metadata, dict) and metadata.get("response") is not None
                            and metadata.get("stream_complete") is not False)
            call["status"] = "failed" if has_response else "provider_outcome_unknown"
            call["error_code"] = type(exc).__name__
            call["error_artifact"] = self.journal.artifact(
                f"calls/{call_id}/{attempt_id}/error.json",
                {"error": str(exc), "type": type(exc).__name__, "metadata": metadata})
            self._event("ERROR", role, "task.model_" + call["status"], call_id=call_id,
                        attempt_id=attempt_id, task_id=task["task_id"],
                        error_code=call["error_code"], retryable=False,
                        duration_ms=round((time.monotonic() - start) * 1000),
                        output_artifact_ref=call["error_artifact"])
            if not has_response:
                self.blocked_unknown = True
            self._checkpoint()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise _Stopped(call["status"]) from exc
        self._event("DEBUG", role, "task.model_response_received", call_id=call_id,
                    attempt_id=attempt_id, task_id=task["task_id"])
        call["response_artifact"] = self.journal.artifact(
            f"calls/{call_id}/{attempt_id}/response.json", {"content": content, "metadata": metadata})
        call["status"] = "response_persisted"
        call["metadata"] = metadata
        self._event("INFO", role, "task.model_response_persisted", call_id=call_id,
                    attempt_id=attempt_id, task_id=task["task_id"],
                    output_artifact_ref=call["response_artifact"],
                    duration_ms=round((time.monotonic() - start) * 1000))
        self._checkpoint()
        if isinstance(metadata, dict) and (metadata.get("finish_reason") in {"length", "max_tokens"}
                                           or metadata.get("stream_complete") is False):
            call["status"] = "protocol_failed"
            call["error_code"] = "CHAPTER_RESPONSE_TRUNCATED"
            self._checkpoint()
            raise _ProtocolError("CHAPTER_RESPONSE_TRUNCATED")
        try:
            parsed = _dump(parse_role_output(role, content))
        except Exception as exc:
            call["status"] = "protocol_failed"
            call["error_code"] = "CHAPTER_RESPONSE_PROTOCOL"
            self._event("ERROR", role, "model.protocol_failed", call_id=call_id,
                        task_id=task["task_id"], error_code="CHAPTER_RESPONSE_PROTOCOL",
                        message=str(exc), retryable=False)
            self._checkpoint()
            raise _ProtocolError("CHAPTER_RESPONSE_PROTOCOL") from exc
        call["status"] = "validated"
        call["consumed"] = True
        self._record_presented(payload, task)
        self._event("INFO", role, "task.model_validated", call_id=call_id,
                    attempt_id=attempt_id, task_id=task["task_id"])
        return parsed

    def _directory(self, document_ids: list[str] | None = None) -> list[dict]:
        docs = set(document_ids or self.documents)
        grouped: dict[tuple, dict] = {}
        for unit in self.units.values():
            if unit.document_id not in docs:
                continue
            key = (unit.document_id, unit.chapter_id)
            entry = grouped.setdefault(key, {"document_id": key[0], "chapter_id": key[1],
                "title": unit.title, "unit_count": 0, "characters": 0})
            entry["unit_count"] += 1
            entry["characters"] += len(unit.text)
        return list(grouped.values())

    def _packet(self, unit_id: str, task: dict, start: int = 0, end: int | None = None,
                *, character_limit: int | None = None) -> dict:
        unit = self.units[unit_id]
        if not 0 <= start <= len(unit.text) or (end is not None and end < start):
            raise ValueError("CHAPTER_INVALID_EVIDENCE_RANGE")
        requested_end = min(end if end is not None else len(unit.text), len(unit.text))
        returned_end = min(requested_end, start + (character_limit or self.limits["max_unit_chars"]))
        ledger = self.state["material_ledger"][unit_id]
        if task["task_id"] not in ledger["retrieved_by"]:
            ledger["retrieved_by"].append(task["task_id"])
        packet = {"evidence_id": unit_id, "document_id": unit.document_id,
            "chapter_id": unit.chapter_id, "title": unit.title, "page": unit.page,
            "locator": unit.locator, "metadata": unit.metadata,
            "document_identity": self.documents[unit.document_id],
            "text": unit.text[start:returned_end], "start": start, "end": returned_end,
            "full_length": len(unit.text), "truncated": returned_end < requested_end,
            "partial_unit": start > 0 or returned_end < len(unit.text),
            "context_warning": "片段边界不保证表头、续表或脚注完整；必要时补读上下文。"
                if start > 0 or returned_end < len(unit.text) else None,
            "content_hash": unit.content_hash}
        return packet

    def _task(self, spec: dict, slices: list[dict] | None = None) -> dict:
        return {**spec, "status": "queued", "rounds": 0, "evidence_slices": slices or [],
                "tool_results": [], "claim_ids": [], "limitations": [], "context_revisions": []}

    def _seed_tasks(self) -> None:
        if self.state["tasks"]:
            return
        grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
        size = min(self.limits["max_unit_chars"], self.limits["max_context_chars"] // 3)
        for unit in self.units.values():
            for start in range(0, max(1, len(unit.text)), int(size)):
                grouped[(unit.document_id, unit.chapter_id)].append({
                    "evidence_id": unit.unit_id, "start": start,
                    "end": min(start + int(size), len(unit.text))})
        for (doc_id, chapter_id), slices in grouped.items():
            batches: list[list[dict]] = []
            batch: list[dict] = []
            characters = 0
            for item in slices:
                n = item["end"] - item["start"]
                if batch and (len(batch) >= self.limits["max_task_units"] or
                              characters + n > self.limits["max_context_chars"] // 2):
                    batches.append(batch)
                    batch, characters = [], 0
                batch.append(item)
                characters += n
            if batch:
                batches.append(batch)
            for index, items in enumerate(batches):
                self.state["tasks"].append(self._task({
                    "task_id": "chapter-" + _hash([doc_id, chapter_id, index])[:16],
                    "kind": "chapter", "objective": "检查本组完整披露、数字、条件和风险线索；必要时跨章取证。",
                    "document_ids": list(self.documents), "chapter_ids": [chapter_id],
                    "evidence_ids": list(dict.fromkeys(s["evidence_id"] for s in items)),
                    "origin_document_id": doc_id, "parent_task_id": None,
                    "dependencies": [], "topic": None}, items))
        for topic, objective in FINANCIAL_TOPICS:
            # Reports are separate by default. Explicit report sets remain
            # available to tools; topic conclusions still retain a focal report.
            for doc_id in self.documents:
                self.state["tasks"].append(self._task({
                    "task_id": f"topic-{topic}-{_hash(doc_id)[:10]}", "kind": "financial_topic",
                    "objective": objective, "document_ids": list(self.documents),
                    "origin_document_id": doc_id, "chapter_ids": [], "evidence_ids": [],
                    "parent_task_id": None, "dependencies": [], "topic": topic}))
        self._event("INFO", "规划", "tasks.seeded", task_count=len(self.state["tasks"]))
        self._checkpoint()

    def _tool(self, request: dict, task: dict) -> dict:
        name = request["name"]
        arguments = request.get("arguments", {})
        requested_docs = arguments.get("document_ids") or list(self.documents)
        if not set(requested_docs).issubset(self.documents):
            raise ValueError("CHAPTER_UNAUTHORIZED_DOCUMENT")
        chapter_ids = set(arguments.get("chapter_ids") or [])
        candidates = [u for u in self.units.values() if u.document_id in requested_docs
                      and (not chapter_ids or u.chapter_id in chapter_ids)]
        if name == "directory":
            return {"entries": self._directory(requested_docs), "complete": True}
        if name in {"read_evidence", "read_context"}:
            ids = arguments.get("evidence_ids") or []
            allowed = {u.unit_id for u in candidates}
            if not ids or not set(ids).issubset(allowed):
                raise ValueError("CHAPTER_EVIDENCE_NOT_AUTHORIZED_OR_NOT_FOUND")
            if name == "read_context":
                expanded = list(ids)
                for unit_id in ids:
                    unit = self.units[unit_id]
                    neighbors = [u.unit_id for u in candidates if u.document_id == unit.document_id
                                 and u.chapter_id == unit.chapter_id]
                    index = neighbors.index(unit_id)
                    radius = min(int(arguments.get("context_radius", 1)), 3)
                    expanded.extend(neighbors[max(0, index-radius):index+radius+1])
                ids = list(dict.fromkeys(expanded))
            max_ids = self.limits["max_task_units"]
            selected = ids[:max_ids]
            budget = self.limits["max_context_chars"] // 2
            packets = []
            for unit_id in selected:
                limit = max(1, budget // max(1, len(selected) - len(packets)))
                packet = self._packet(unit_id, task, int(arguments.get("start", 0)),
                                      arguments.get("end"), character_limit=int(limit))
                packets.append(packet)
                budget -= len(packet["text"])
            return {"evidence": packets, "omitted_evidence_ids": ids[max_ids:]}
        if name == "search_evidence":
            query = (arguments.get("query") or "").strip()
            if not query:
                raise ValueError("CHAPTER_EMPTY_SEARCH")
            tokens = [s.lower() for s in re.split(r"[\s,，;；]+", query) if s]
            ranked = [(sum(u.text.lower().count(t) for t in tokens), u) for u in candidates]
            ranked = sorted((item for item in ranked if item[0]), key=lambda item: (-item[0], item[1].unit_id))
            top_k = min(int(arguments.get("top_k", 8)), self.limits["max_task_units"])
            packets = []
            for score, unit in ranked[:top_k]:
                start = min((unit.text.lower().find(t) for t in tokens if t in unit.text.lower()), default=0)
                packet = self._packet(unit.unit_id, task, max(0, start-200),
                                      character_limit=int(self.limits["max_context_chars"] // (2*max(top_k, 1))))
                packet["lexical_score"] = score
                packets.append(packet)
            return {"backend": "frozen_local_lexical_v1", "query": query, "evidence": packets,
                "match_count": len(ranked), "complete_search_of_indexed_units": True,
                "absence_is_not_nonexistence": True}
        if name == "query_facts":
            query = (arguments.get("query") or "").lower()
            facts = [f for f in self.facts.values() if f.get("document_id") in requested_docs
                     and (not query or query in json.dumps(f, ensure_ascii=False).lower())]
            top_k = min(int(arguments.get("top_k", 8)), 50)
            return {"facts": facts[:top_k], "omitted_count": max(0, len(facts)-top_k),
                    "llm_cannot_change_admission": True}
        if name == "calculate":
            return self._calculate(arguments, set(requested_docs))
        raise ValueError("CHAPTER_TOOL_NOT_ALLOWED")

    def _calculate(self, args: dict, document_ids: set[str]) -> dict:
        if args.get("calculation_id"):
            result = self.calculations.get(args["calculation_id"])
            if not result:
                raise ValueError("CHAPTER_CALCULATION_NOT_FOUND")
            source_ids = [ref["evidence_id"] for operand in result["operands"]
                          for ref in operand["evidence_refs"]]
            if any(key not in self.units or self.units[key].document_id not in document_ids
                   for key in source_ids):
                raise ValueError("CHAPTER_CALCULATION_NOT_AUTHORIZED")
            return result
        ids = args.get("fact_ids") or []
        if not ids or len(ids) != len(set(ids)) or any(i not in self.facts for i in ids):
            raise ValueError("CHAPTER_CALCULATION_OPERANDS_MISSING")
        facts = [self.facts[i] for i in ids]
        if any(f.get("status") != "auto_passed" for f in facts):
            raise ValueError("CHAPTER_CALCULATION_OPERAND_NOT_AUTO_PASSED")
        if any(f.get("document_id") not in document_ids for f in facts):
            raise ValueError("CHAPTER_CALCULATION_NOT_AUTHORIZED")
        for field in ("unit", "currency", "scope"):
            values = {f.get(field) for f in facts}
            if len(values) != 1 or None in values or "" in values:
                raise ValueError(f"CHAPTER_CALCULATION_INCOMPATIBLE_{field.upper()}")
        companies = {self.documents[f["document_id"]].get("company_id") or
                     self.documents[f["document_id"]].get("company") for f in facts}
        if len(companies) != 1 or None in companies:
            raise ValueError("CHAPTER_CALCULATION_COMPANY_UNKNOWN_OR_MIXED")
        formula = (args.get("formula") or "").lower()
        aliases = {"add": "sum", "subtract": "difference", "divide": "ratio",
                   "yoy_growth": "yoy", "qoq_growth": "qoq"}
        formula = aliases.get(formula, formula)
        if formula in {"growth_rate", "difference", "ratio", "yoy", "qoq", "single_quarter"} and len(facts) != 2:
            raise ValueError("CHAPTER_CALCULATION_ARITY")
        if formula == "growth_rate":
            try:
                self._validate_periods(facts, "yoy")
                formula = "yoy"
            except ValueError:
                self._validate_periods(facts, "qoq")
                formula = "qoq"
        if formula not in {"sum", "difference", "ratio", "yoy", "qoq", "single_quarter"}:
            raise ValueError("CHAPTER_CALCULATION_FORMULA_NOT_ALLOWED")
        if formula != "sum" and len(facts) != 2:
            raise ValueError("CHAPTER_CALCULATION_ARITY")
        if formula in {"yoy", "qoq"}:
            self._validate_periods(facts, formula)
        elif formula == "single_quarter":
            if (len({f.get("concept_code") for f in facts}) != 1
                    or not facts[0].get("concept_code")
                    or facts[0].get("period_start") != facts[1].get("period_start")
                    or any(f.get("period_basis") != "cumulative" for f in facts)):
                raise ValueError("CHAPTER_CALCULATION_CUMULATIVE_SCOPE_MISMATCH")
            try:
                current_end, previous_end = (date.fromisoformat(str(f["period_end"])) for f in facts)
            except (KeyError, ValueError) as exc:
                raise ValueError("CHAPTER_CALCULATION_PERIOD_UNKNOWN") from exc
            if (not 75 <= (current_end-previous_end).days <= 100
                    or (current_end.month, current_end.day) not in {(3, 31), (6, 30), (9, 30), (12, 31)}
                    or (previous_end.month, previous_end.day) not in {(3, 31), (6, 30), (9, 30)}
                    or not str(facts[0]["period_start"]).endswith("-01-01")):
                raise ValueError("CHAPTER_CALCULATION_SINGLE_QUARTER_BOUNDARY_REQUIRED")
        elif formula in {"sum", "ratio", "difference"}:
            periods = {(f.get("period_start"), f.get("period_end")) for f in facts}
            if len(periods) != 1 or any(None in pair for pair in periods):
                raise ValueError("CHAPTER_CALCULATION_PERIOD_MISMATCH")
        restated = {f.get("is_restatement", f.get("restated")) for f in facts}
        calculation_assumptions = []
        if len(restated) > 1:
            same_report_comparison = (
                formula == "yoy" and len({f["document_id"] for f in facts}) == 1
                and facts[1].get("is_restatement", facts[1].get("restated")) is True
                and self.documents[facts[0]["document_id"]].get("period_start") == facts[0].get("period_start")
                and self.documents[facts[0]["document_id"]].get("period_end") == facts[0].get("period_end"))
            if not same_report_comparison:
                raise ValueError("CHAPTER_CALCULATION_RESTATEMENT_COMPARABILITY_UNKNOWN")
            calculation_assumptions.append("使用同一报告披露的重述比较期数据；不将其与重述前数据混用。")
        try:
            values = [Decimal(str(f["value"])) for f in facts]
        except (InvalidOperation, KeyError) as exc:
            raise ValueError("CHAPTER_CALCULATION_INVALID_VALUE") from exc
        if not all(value.is_finite() for value in values):
            raise ValueError("CHAPTER_CALCULATION_NONFINITE")
        with localcontext() as ctx:
            ctx.prec = 40
            if formula == "sum":
                value = sum(values, Decimal(0))
            elif formula in {"difference", "single_quarter"}:
                value = values[0] - values[1]
            else:
                if not values[1]:
                    raise ValueError("CHAPTER_CALCULATION_ZERO_BASE")
                if formula in {"yoy", "qoq"} and values[1] < 0:
                    raise ValueError("CHAPTER_CALCULATION_NEGATIVE_BASE_REQUIRES_ASSUMPTION")
                value = ((values[0] - values[1]) / values[1] * 100
                         if formula in {"yoy", "qoq"} else values[0] / values[1])
        from .chapter_contracts import CalculationRecord

        operands = []
        for fact in facts:
            refs = []
            for source_id in fact.get("source_ids", []):
                unit = self.units.get(source_id)
                if not unit:
                    continue
                if unit.document_id != fact["document_id"]:
                    raise ValueError("CHAPTER_CALCULATION_OPERAND_SOURCE_DOCUMENT_MISMATCH")
                raw_value = str(fact.get("raw_value", fact["value"]))
                scale = Decimal(str(fact.get("unit_scale", "1")))
                if not scale.is_finite() or scale <= 0:
                    raise ValueError("CHAPTER_CALCULATION_UNIT_SCALE_INVALID")
                try:
                    normalized = _raw_decimal(raw_value) * scale
                except InvalidOperation as exc:
                    raise ValueError("CHAPTER_CALCULATION_RAW_VALUE_INVALID") from exc
                if normalized != Decimal(str(fact["value"])):
                    raise ValueError("CHAPTER_CALCULATION_UNIT_SCALE_MISMATCH")
                alternatives = [raw_value]
                try:
                    amount = Decimal(raw_value)
                    alternatives += [format(amount, ",f"), format(amount, "f")]
                except InvalidOperation:
                    pass
                for spelling in dict.fromkeys(alternatives):
                    pattern = r"(?<![\d.,，])"+re.escape(spelling)+r"(?![\d.,，])"
                    matches = list(re.finditer(pattern, unit.text))
                    label = str(fact.get("raw_label") or fact.get("label") or "")
                    label_offsets = [m.end() for m in re.finditer(re.escape(label), unit.text)] if label else []
                    near_label = [(m.start()-end, m.start()) for m in matches for end in label_offsets
                                  if 0 <= m.start()-end <= 1200]
                    if near_label:
                        offset = min(near_label)[1]
                    elif len(matches) == 1:
                        offset = matches[0].start()
                    else:
                        continue
                    if offset >= 0:
                        refs.append({"evidence_id": source_id, "start": offset,
                            "end": offset+len(spelling), "quote": spelling, "role": "support"})
                        break
            if not refs:
                raise ValueError("CHAPTER_CALCULATION_OPERAND_SOURCE_SPAN_MISSING")
            operands.append({"fact_id": fact["fact_id"], "value": str(fact["value"]),
                "validation_status": "auto_passed", "evidence_refs": refs,
                "scope": {"subject": str(next(iter(companies))), "period":
                    f"{fact.get('period_start')}/{fact.get('period_end')}",
                    "basis": fact.get("scope"), "unit": fact["unit"],
                    "currency": fact.get("currency"),
                    "cumulative": fact.get("period_basis") == "cumulative"
                        if fact.get("period_basis") else None,
                    "restated": fact.get("is_restatement", fact.get("restated"))}})
        result_period_start = facts[0].get("period_start")
        if formula == "single_quarter":
            result_period_start = (date.fromisoformat(str(facts[1]["period_end"])) + timedelta(days=1)).isoformat()
        result = {"formula": formula, "operands": operands, "value": str(value),
            "status": "succeeded", "scope": {"subject": str(next(iter(companies))),
                "period": f"{result_period_start}/{facts[0].get('period_end')}",
                "basis": facts[0].get("scope"), "currency": facts[0].get("currency"),
                "cumulative": False if formula == "single_quarter" else
                    facts[0].get("period_basis") == "cumulative" if facts[0].get("period_basis") else None,
                "unit": "%" if formula in {"yoy", "qoq"} else
                         "ratio" if formula == "ratio" else facts[0]["unit"]},
            "assumptions": calculation_assumptions, "policy_version": "chapter_decimal_v1"}
        result["calculation_id"] = "calc-" + _hash(result)[:24]
        result = _dump(CalculationRecord.model_validate(result))
        self.calculations[result["calculation_id"]] = result
        ref = self.journal.artifact(f"calculations/{result['calculation_id']}.json", result)
        self._event("INFO", "计算", "calculation.completed", calculation_ids=[result["calculation_id"]],
                    output_artifact_ref=ref)
        return result

    @staticmethod
    def _validate_periods(facts: list[dict], formula: str) -> None:
        if len(facts) != 2:
            raise ValueError("CHAPTER_CALCULATION_ARITY")
        if len({f.get("concept_code") for f in facts}) != 1 or not facts[0].get("concept_code"):
            raise ValueError("CHAPTER_CALCULATION_CONCEPT_MISMATCH")
        try:
            a_start, a_end = (date.fromisoformat(str(facts[0][key])) for key in ("period_start", "period_end"))
            b_start, b_end = (date.fromisoformat(str(facts[1][key])) for key in ("period_start", "period_end"))
        except (KeyError, ValueError) as exc:
            raise ValueError("CHAPTER_CALCULATION_PERIOD_UNKNOWN") from exc
        if a_start > a_end or b_start > b_end or a_end <= b_end:
            raise ValueError("CHAPTER_CALCULATION_PERIOD_ORDER")
        if formula == "yoy":
            if (a_start.year-b_start.year != 1 or a_end.year-b_end.year != 1
                    or (a_start.month, a_start.day) != (b_start.month, b_start.day)
                    or (a_end.month, a_end.day) != (b_end.month, b_end.day)):
                raise ValueError("CHAPTER_CALCULATION_YOY_PERIOD_MISMATCH")
        else:
            if (not 75 <= (a_end-a_start).days <= 100
                    or not 75 <= (b_end-b_start).days <= 100
                    or (a_start-b_end).days != 1
                    or (a_start.month, a_start.day) not in {(1, 1), (4, 1), (7, 1), (10, 1)}
                    or (b_start.month, b_start.day) not in {(1, 1), (4, 1), (7, 1), (10, 1)}
                    or (a_end.month, a_end.day) not in {(3, 31), (6, 30), (9, 30), (12, 31)}
                    or (b_end.month, b_end.day) not in {(3, 31), (6, 30), (9, 30), (12, 31)}):
                raise ValueError("CHAPTER_CALCULATION_QOQ_REQUIRES_ADJACENT_SINGLE_QUARTERS")

    def _execute_tools(self, requests: list[dict], task: dict) -> None:
        if len(requests) > self.limits["max_tools_per_round"]:
            raise _ProtocolError("CHAPTER_TOOL_BATCH_BUDGET")
        if len({r["call_id"] for r in requests}) != len(requests):
            raise _ProtocolError("CHAPTER_TOOL_DUPLICATE_CALL_ID")
        for request in requests:
            if self.journal.cancel_requested():
                raise _Stopped("cancelled")
            request_ref = self.journal.artifact(
                f"retrievals/{task['task_id']}/{uuid.uuid4().hex}-request.json", request)
            self._event("INFO", "检索", "tool.started", task_id=task["task_id"],
                        tool_call_id=request["call_id"], tool_name=request["name"],
                        input_artifact_ref=request_ref)
            start = time.monotonic()
            try:
                output = self._tool(request, task)
                result = {"call_id": request["call_id"], "name": request["name"],
                          "status": "succeeded", "output": output}
            except (ValueError, KeyError, TypeError) as exc:
                result = {"call_id": request["call_id"], "name": request["name"],
                          "status": "failed", "error_code": str(exc),
                          "not_a_financial_finding": True}
                self.state["errors"].append({"task_id": task["task_id"],
                    "stage": "tool", "error_code": str(exc)})
            ref = self.journal.artifact(
                f"retrievals/{task['task_id']}/{uuid.uuid4().hex}-response.json", result)
            self._event("INFO" if result["status"] == "succeeded" else "ERROR", "检索",
                        "tool.completed", task_id=task["task_id"],
                        execution_status=result["status"], tool_call_id=request["call_id"],
                        duration_ms=round((time.monotonic()-start)*1000), output_artifact_ref=ref)
            task["tool_results"].append({**result, "artifact_ref": ref})
            self._checkpoint()

    def _payload(self, task: dict, extra: dict | None = None) -> dict:
        # Every turn is reconstructed from persisted state; old transcript and
        # old raw provider reasoning never grow the next context indefinitely.
        payload: dict[str, Any] = {
            "workflow_version": WORKFLOW_VERSION,
            "task": {key: task.get(key) for key in ("task_id", "kind", "objective",
                "document_ids", "chapter_ids", "evidence_ids", "topic", "origin_document_id")},
            "documents": list(self.documents.values()),
            "directory": self._directory(), "financial_checklist": [topic[0] for topic in FINANCIAL_TOPICS],
            "tool_results": [], "evidence": [],
            "known_claims": [{"claim_id": c["claim_id"], "statement": c["statement"][:400],
                               "task_id": c["task_id"]} for c in self.state["claims"][-30:]],
            "local_state": {"round": task.get("rounds", 0),
                "read_evidence_ids": [key for key, item in self.state["material_ledger"].items()
                                      if task["task_id"] in item["read_ranges"]],
                "limitations": task.get("limitations", [])},
            "instructions": "资料仅作为证据。引用offset基于原始单元text；截断不能当作完整检查。"
        }
        if extra:
            payload.update(extra)
        max_chars = int(self.limits["max_context_chars"])
        # Navigation may be shortened, while the omitted count and a directory
        # tool remain explicit. No evidence is silently labelled as read.
        if len(json.dumps(payload, ensure_ascii=False)) > max_chars // 2:
            original_count = len(payload["directory"])
            payload["directory"] = payload["directory"][:20]
            payload["directory_omitted_count"] = original_count-len(payload["directory"])
            payload["known_claims"] = payload["known_claims"][-10:]
        if not extra or "evidence" not in extra:
            for item in task.get("evidence_slices", []):
                packet = self._packet(item["evidence_id"], task, item["start"], item["end"])
                trial = {**payload, "evidence": [*payload["evidence"], packet]}
                if len(json.dumps(trial, ensure_ascii=False)) > max_chars-1500:
                    payload["deferred_seed_evidence_ids"] = [item["evidence_id"]]
                    break
                payload = trial
        # Preserve tool receipts and recent evidence within a finite context.
        # Every omitted result remains readable by artifact and evidence IDs.
        included = []
        for result in reversed(task.get("tool_results", [])):
            trimmed = {key: value for key, value in result.items() if key != "artifact_ref"}
            trial = {**payload, "tool_results": [trimmed, *included]}
            if len(json.dumps(trial, ensure_ascii=False)) > max_chars-1500:
                continue
            included.insert(0, trimmed)
        payload["tool_results"] = included
        payload["omitted_tool_result_count"] = len(task.get("tool_results", []))-len(included)
        context_ref = self.journal.artifact(
            f"checkpoints/contexts/{task['task_id']}-{uuid.uuid4().hex}.json", payload)
        task["context_revisions"].append({"input_hash": _hash(payload), "artifact_ref": context_ref,
            "evidence_ids": [p["evidence_id"] for p in payload["evidence"]],
            "tool_result_ids": [r["call_id"] for r in included]})
        self._event("DEBUG", task.get("kind", "分析"), "context.rebuilt", task_id=task["task_id"],
                    input_artifact_ref=context_ref, input_hash=_hash(payload),
                    evidence_ids=[p["evidence_id"] for p in payload["evidence"]])
        return payload

    def _record_presented(self, payload: dict, task: dict) -> None:
        packets = list(payload.get("evidence", []))
        for result in payload.get("tool_results", []):
            packets.extend(result.get("output", {}).get("evidence", []))
        for packet in packets:
            ledger = self.state["material_ledger"][packet["evidence_id"]]
            ranges = ledger["read_ranges"].get(task["task_id"], [])
            ledger["read_ranges"][task["task_id"]] = _merged_ranges(
                ranges+[[packet["start"], packet["end"]]])

    def _add_tasks(self, specs: list[dict], parent: str) -> None:
        from .chapter_contracts import TaskSpec

        existing = {t["task_id"] for t in self.state["tasks"]}
        dynamic_count = sum(bool(t.get("dynamically_created")) for t in self.state["tasks"])
        for raw in specs:
            spec = _dump(TaskSpec.model_validate(raw))
            if spec["task_id"] in existing:
                self.state["limitations"].append(f"重复任务未派发：{spec['task_id']}")
                continue
            if (not set(spec["document_ids"]).issubset(self.documents)
                    or not set(spec["evidence_ids"]).issubset(self.units)
                    or not set(spec["dependencies"]).issubset(existing)):
                raise _ProtocolError("CHAPTER_DYNAMIC_TASK_IDENTITY_INVALID")
            if any(self.units[key].document_id not in spec["document_ids"] for key in spec["evidence_ids"]):
                raise _ProtocolError("CHAPTER_DYNAMIC_TASK_EVIDENCE_OUTSIDE_DOCUMENT_SET")
            task = self._task(spec, [{"evidence_id": key, "start": 0,
                "end": min(len(self.units[key].text), self.limits["max_unit_chars"])}
                for key in spec["evidence_ids"]])
            task["parent_task_id"] = parent
            task["dynamically_created"] = True
            if dynamic_count >= self.limits["max_dynamic_tasks"]:
                task["status"] = "partial"
                task["stop_reason"] = "dynamic_task_budget"
            else:
                dynamic_count += 1
            existing.add(task["task_id"])
            self.state["tasks"].append(task)
            self._event("INFO", "规划", "task.registered", task_id=task["task_id"],
                        parent_task_id=parent, execution_status=task["status"])

    def _accept_claims(self, raw_claims: list[dict], task: dict) -> None:
        from .chapter_contracts import ChapterClaim, validate_claim

        current = {c["claim_id"]: c for c in self.state["claims"]}
        for raw in raw_claims:
            claim = ChapterClaim.model_validate(raw)
            if claim.task_id != task["task_id"]:
                raise _ProtocolError("CHAPTER_CLAIM_TASK_MISMATCH")
            old = current.get(claim.claim_id)
            if old and (old["task_id"] != claim.task_id or claim.revision != old["revision"]+1):
                raise _ProtocolError("CHAPTER_CLAIM_REVISION_INVALID")
            issues = validate_claim(claim, self.units, list(self.documents), self.calculations)
            for ref in claim.evidence_refs:
                ranges = self.state["material_ledger"].get(ref.evidence_id, {}).get("read_ranges", {}).get(task["task_id"], [])
                if not _covers(ranges, ref.start, ref.end):
                    issues.append(f"GENERATOR_DID_NOT_READ_SPAN:{ref.evidence_id}")
            for key in claim.checked_evidence_ids:
                unit = self.units.get(key)
                ranges = self.state["material_ledger"].get(key, {}).get("read_ranges", {}).get(task["task_id"], [])
                if unit and not _covers(ranges, 0, len(unit.text)):
                    issues.append(f"GENERATOR_CHECKED_SCOPE_NOT_FULLY_READ:{key}")
            item = _dump(claim)
            if old:
                self.state.setdefault("claim_history", []).append(old)
                self.state["claims"].remove(old)
                self._invalidate_claim(claim.claim_id, "claim_revised")
            self.state["claims"].append(item)
            current[claim.claim_id] = item
            task["claim_ids"].append(claim.claim_id)
            if issues:
                self.state.setdefault("claim_program_issues", {})[claim.claim_id] = issues
            else:
                self.state.setdefault("claim_program_issues", {}).pop(claim.claim_id, None)
            for ref in claim.evidence_refs:
                if ref.evidence_id in self.state["material_ledger"]:
                    refs = self.state["material_ledger"][ref.evidence_id]["referenced_by"]
                    if claim.claim_id not in refs:
                        refs.append(claim.claim_id)
            artifact = self.journal.artifact(
                f"claims/{claim.claim_id}/revision-{claim.revision}-{claim.revision_hash}.json",
                {"claim": item, "program_issues": issues})
            self._event("INFO", "程序校验", "claim.registered", task_id=task["task_id"],
                        claim_ids=[claim.claim_id], evidence_ids=[r.evidence_id for r in claim.evidence_refs],
                        output_artifact_ref=artifact, execution_status="candidate",
                        program_issues=issues)

    def _invalidate_claim(self, claim_id: str, reason: str) -> None:
        for receipt in self.state["receipts"]:
            if receipt["claim_id"] == claim_id and reason not in receipt["invalidated_by"]:
                receipt["invalidated_by"].append(reason)
        self._event("INFO", "语义核验", "claim.receipt_invalidated", claim_ids=[claim_id], message=reason)

    def _accept_answer(self, answer: dict, task: dict) -> None:
        keys = ("claims", "receipts", "material_ledger", "missing_documents", "limitations",
                "claim_history", "claim_program_issues", "reopened_claim_ids", "claim_required_counterevidence")
        before = {key: deepcopy(self.state.get(key)) for key in keys}
        tasks_before = list(self.state["tasks"])
        task_before = deepcopy(task)
        try:
            self._accept_answer_impl(answer, task)
        except (ValueError, KeyError, TypeError):
            for key, value in before.items():
                if value is None:
                    self.state.pop(key, None)
                else:
                    self.state[key] = value
            self.state["tasks"] = tasks_before
            task.clear()
            task.update(task_before)
            raise

    def _accept_answer_impl(self, answer: dict, task: dict) -> None:
        self._accept_claims(answer["claims"], task)
        for item in answer["material_dispositions"]:
            key = item["evidence_id"]
            if key not in self.units:
                raise _ProtocolError("CHAPTER_DISPOSITION_UNKNOWN_EVIDENCE")
            ledger = self.state["material_ledger"][key]
            ranges = ledger["read_ranges"].get(task["task_id"], [])
            if not ranges:
                raise _ProtocolError("CHAPTER_DISPOSITION_EVIDENCE_NOT_READ")
            if item["disposition"] == "duplicate":
                representative = item.get("representative_evidence_id")
                if representative not in self.units:
                    raise _ProtocolError("CHAPTER_DUPLICATE_REPRESENTATIVE_UNKNOWN")
            for claim_id in item["claim_ids"]:
                if not any(c["claim_id"] == claim_id for c in self.state["claims"]):
                    raise _ProtocolError("CHAPTER_DISPOSITION_CLAIM_UNKNOWN")
                if item["disposition"] in {"counter", "qualification"}:
                    self._invalidate_claim(claim_id, f"{item['disposition']}:{key}")
                    self.state.setdefault("reopened_claim_ids", []).append(claim_id)
                    required = self.state.setdefault("claim_required_counterevidence", {}).setdefault(claim_id, [])
                    if key not in required:
                        required.append(key)
            ledger["dispositions"].append({**item, "task_id": task["task_id"], "checked_ranges": ranges})
            if item["disposition"] != "unresolved":
                ledger["checked_ranges"] = _merged_ranges(ledger["checked_ranges"] + ranges)
                ledger["fully_checked"] = _covers(ledger["checked_ranges"], 0, len(self.units[key].text))
        self._add_tasks(answer["new_tasks"], task["task_id"])
        for missing in answer["missing_documents"]:
            record = {**missing, "task_id": task["task_id"]}
            if record not in self.state["missing_documents"]:
                self.state["missing_documents"].append(record)
        task["limitations"].extend(answer["limitations"])
        task["status"] = {"completed": "succeeded", "partial": "partial",
                          "waiting_for_documents": "waiting_for_documents"}[answer["completion"]]
        if task["kind"] == "financial_topic":
            grounding = (any(c["evidence_refs"] or c["calculation_ids"] for c in answer["claims"])
                         or any(d["disposition"] != "unresolved" for d in answer["material_dispositions"]))
            if answer["missing_documents"]:
                task["status"] = "waiting_for_documents"
            elif not grounding:
                task["status"] = "partial"
                task["stop_reason"] = "financial_topic_check_basis_missing"
        task["answer"] = answer
        task["answer_artifact"] = self.journal.artifact(
            f"checkpoints/tasks/{task['task_id']}-{uuid.uuid4().hex}.json", answer)

    def _run_task(self, task: dict) -> None:
        role = {"chapter": "chapter_analysis_v1", "financial_topic": "financial_topic_v1",
                "uncovered_material": "uncovered_material_v1"}[task["kind"]]
        task["status"] = "running"
        self._event("INFO", task["kind"], "task.started", task_id=task["task_id"])
        self._checkpoint()
        try:
            for _ in range(int(self.limits["max_task_rounds"])):
                task["rounds"] += 1
                turn = self._call(role, self._payload(task), task)
                if turn["tool_requests"]:
                    self._execute_tools(turn["tool_requests"], task)
                else:
                    self._accept_answer(turn["answer"], task)
                    break
            else:
                task["status"] = "partial"
                task["stop_reason"] = "task_round_budget"
        except _ProtocolError as exc:
            task["status"] = "failed"
            task["stop_reason"] = str(exc)
            self.state["errors"].append({"task_id": task["task_id"], "stage": role,
                                         "error_code": str(exc)})
            self._event("ERROR", role, "task.protocol_failed", task_id=task["task_id"], error_code=str(exc))
        except _Stopped as exc:
            task["status"] = "cancelled" if exc.reason == "cancelled" else "partial"
            task["stop_reason"] = exc.reason
            raise
        finally:
            self._event("INFO", role, "task.finished", task_id=task["task_id"],
                        execution_status=task["status"], claim_ids=task["claim_ids"])
            self._checkpoint()

    def _run_pending(self) -> None:
        while True:
            queued = [t for t in self.state["tasks"] if t["status"] == "queued"]
            if not queued:
                return
            ready = [t for t in queued if all(
                any(d["task_id"] == key and d["status"] == "succeeded" for d in self.state["tasks"])
                for key in t.get("dependencies", []))]
            if not ready:
                for task in queued:
                    task["status"] = "partial"
                    task["stop_reason"] = "dependency_not_succeeded"
                return
            self._run_task(ready[0])

    def _plan(self) -> None:
        if self.state.get("planning_finished"):
            return
        task = self._task({"task_id": "planner", "kind": "planning",
            "objective": "审阅预登记章节与必查专题，仅补充必要任务，不能删除必查项。",
            "document_ids": list(self.documents), "evidence_ids": [], "chapter_ids": []})
        for _ in range(int(self.limits["max_task_rounds"])):
            turn = self._call("chapter_plan_v1", self._payload(task, {
                "registered_tasks": [{"task_id": t["task_id"], "kind": t["kind"], "topic": t.get("topic")}
                                     for t in self.state["tasks"]]}), task)
            if turn["tool_requests"]:
                self._execute_tools(turn["tool_requests"], task)
                continue
            self._add_tasks(turn["plan"]["tasks"], task["task_id"])
            self.state["missing_documents"].extend({**m, "task_id": task["task_id"]}
                                                   for m in turn["plan"]["missing_documents"])
            self.state["limitations"].extend(turn["plan"]["limitations"])
            self.state["planning_finished"] = True
            self._checkpoint()
            return
        self.state["limitations"].append("规划达到轮次限制；保留程序预登记的章节与专题任务。")
        self.state["planning_finished"] = True

    def _uncovered(self) -> None:
        if self.state.get("uncovered_sweep_registered"):
            return
        max_size = int(min(self.limits["max_unit_chars"], self.limits["max_context_chars"] // 3))
        for key, ledger in self.state["material_ledger"].items():
            if ledger["fully_checked"]:
                continue
            unit = self.units[key]
            checked = _merged_ranges(ledger["checked_ranges"])
            for start in range(0, len(unit.text), max_size):
                end = min(start+max_size, len(unit.text))
                if _covers(checked, start, end):
                    continue
                task_id = "uncovered-"+_hash([key, start, end])[:20]
                if any(t["task_id"] == task_id for t in self.state["tasks"]):
                    continue
                self.state["tasks"].append(self._task({"task_id": task_id,
                    "kind": "uncovered_material", "objective":
                    "中性检查本组材料增加的支持、反证、限定、新问题或缺件；允许没有新增线索。",
                    "document_ids": list(self.documents), "chapter_ids": [unit.chapter_id],
                    "evidence_ids": [key], "parent_task_id": None, "dependencies": [], "topic": None},
                    [{"evidence_id": key, "start": start, "end": end}]))
        self.state["uncovered_sweep_registered"] = True
        self._checkpoint()

    def _claim_maps(self) -> tuple[dict, dict]:
        from .chapter_contracts import ChapterClaim, VerificationReceipt

        claims = {c["claim_id"]: ChapterClaim.model_validate(c) for c in self.state["claims"]}
        receipts = {}
        for raw in self.state["receipts"]:
            receipt = VerificationReceipt.model_validate(raw)
            claim = claims.get(receipt.claim_id)
            if claim and receipt.claim_revision_hash == claim.revision_hash:
                receipts[receipt.claim_id] = receipt
        return claims, receipts

    def _verification_payload(self, task: dict, claims: list[Any]) -> dict:
        dependencies: dict[str, list[list[int]]] = defaultdict(list)
        for claim in claims:
            for key in self.state.get("claim_required_counterevidence", {}).get(claim.claim_id, []):
                if key in self.units:
                    dependencies[key].append([0, len(self.units[key].text)])
            for ref in claim.evidence_refs:
                unit = self.units.get(ref.evidence_id)
                if unit:
                    dependencies[ref.evidence_id].append([
                        max(0, ref.start-400), min(len(unit.text), ref.end+400)])
            for key in claim.checked_evidence_ids:
                if key in self.units:
                    dependencies[key].append([0, len(self.units[key].text)])
            for calculation_id in claim.calculation_ids:
                for operand in self.calculations.get(calculation_id, {}).get("operands", []):
                    for ref in operand["evidence_refs"]:
                        if ref["evidence_id"] in self.units:
                            unit = self.units[ref["evidence_id"]]
                            dependencies[ref["evidence_id"]].append([
                                max(0, ref["start"]-400), min(len(unit.text), ref["end"]+400)])
            for key in list(dependencies):
                for required in self.units[key].metadata.get("required_context_ids", []):
                    if required in self.units:
                        dependencies[required].append([0, len(self.units[required].text)])
        packets = []
        budget = int(self.limits["max_context_chars"] // 2)
        for key, ranges in dependencies.items():
            for start, end in _merged_ranges(ranges):
                if budget <= 0:
                    break
                packet = self._packet(key, task, start, end, character_limit=budget)
                budget -= len(packet["text"])
                packets.append(packet)
        task["required_verification_ranges"] = dependencies
        return self._payload(task, {"claims": [_dump(c) for c in claims],
            "evidence": packets, "program_checks": {
                c.claim_id: self.state.get("claim_program_issues", {}).get(c.claim_id, []) for c in claims},
            "counterevidence_dispositions": [disposition
                for ledger in self.state["material_ledger"].values()
                for disposition in ledger["dispositions"]
                if disposition["disposition"] in {"counter", "qualification"}
                and set(disposition["claim_ids"]) & {c.claim_id for c in claims}],
            "recorded_cross_claim_conflicts": [item for item in self.state.get("conflict_history", [])
                if set(item["claim_ids"]) & {c.claim_id for c in claims}],
            "calculations": [self.calculations[key] for c in claims for key in c.calculation_ids
                             if key in self.calculations],
            "known_claims": [],
            "verification_rule": "逐条判断原文支持程度；不能以引用存在代替关联成立。"
        })

    def _verify(self) -> None:
        from .chapter_contracts import (
            SemanticAssessment,
            build_receipt,
            canonical_hash,
        )

        role = self.verifier_role
        profile, _ = self._profile(role)
        claims, existing = self._claim_maps()
        candidates = [c for c in claims.values() if c.claim_id not in existing
                      or existing[c.claim_id].invalidated_by]
        # Batches are also bounded by claim text and source requirements. One
        # oversized dependency remains partial; it is never silently passed.
        batches: list[list[Any]] = []
        batch: list[Any] = []
        estimate = 0
        source_ids: set[str] = set()
        for claim in candidates:
            required = {ref.evidence_id for ref in claim.evidence_refs} | set(claim.checked_evidence_ids)
            for key in list(required):
                if key in self.units:
                    required.update(self.units[key].metadata.get("required_context_ids", []))
            incremental = len(json.dumps(_dump(claim), ensure_ascii=False)) + sum(
                min(len(self.units[key].text), 6000)+700 for key in required-source_ids if key in self.units)
            if batch and (len(batch) >= self.limits["verification_batch_size"]
                          or estimate+incremental > self.limits["max_context_chars"]*0.65):
                batches.append(batch)
                batch, estimate, source_ids = [], 0, set()
            batch.append(claim)
            estimate += incremental
            source_ids.update(required)
        if batch:
            batches.append(batch)
        for batch in batches:
            task = self._task({"task_id": "verify-"+canonical_hash([c.revision_hash for c in batch])[:20],
                "kind": "verification", "objective": "逐条核验结论与原文的支持关系及反证。",
                "document_ids": list(self.documents), "chapter_ids": [], "evidence_ids": []})
            try:
                for _ in range(self.limits["max_task_rounds"]):
                    turn = self._call(role, self._verification_payload(task, batch), task)
                    if turn["tool_requests"]:
                        self._execute_tools(turn["tool_requests"], task)
                        continue
                    assessments = {a["claim_id"]: a for a in turn["assessments"]}
                    if set(assessments) != {c.claim_id for c in batch}:
                        raise _ProtocolError("CHAPTER_VERIFIER_MISSING_OR_EXTRA_CLAIMS")
                    read_ranges = {key: ledger["read_ranges"][task["task_id"]]
                        for key, ledger in self.state["material_ledger"].items()
                        if ledger["read_ranges"].get(task["task_id"])}
                    for claim in batch:
                        receipt = build_receipt(
                            SemanticAssessment.model_validate(assessments[claim.claim_id]), claim,
                            self.units, list(self.documents), list(read_ranges), self.calculations,
                            prompt_hash=profile.system_hash, read_ranges=read_ranges,
                            verifier_model=getattr(getattr(self.client, "config", None), "model", None))
                        dumped = _dump(receipt)
                        generator_issues = self.state.get("claim_program_issues", {}).get(claim.claim_id, [])
                        counter_issues = _counterevidence_issues(claim.claim_id, dumped,
                            self.state.get("claim_required_counterevidence", {}), self.units)
                        dumped["program_issues"] = sorted(set(
                            dumped["program_issues"]+generator_issues+counter_issues))
                        # Receipt histories are immutable. A newly issued
                        # receipt supersedes, rather than overwrites, the old.
                        self.state["receipts"].append(dumped)
                        ref = self.journal.artifact(
                            f"claims/{claim.claim_id}/{receipt.receipt_id}.json", dumped)
                        self._event("INFO", "语义核验", "claim.verified", task_id=task["task_id"],
                            claim_ids=[claim.claim_id], verdict=receipt.verdict,
                            execution_status=receipt.execution_status,
                            program_issues=dumped["program_issues"], output_artifact_ref=ref)
                    self._checkpoint()
                    break
                else:
                    self.state["limitations"].append("核验达到工具轮次边界："+task["task_id"])
                    self._verification_failure(batch, "failed", "CHAPTER_VERIFIER_ROUND_BUDGET", profile)
            except _ProtocolError as exc:
                self.state["errors"].append({"task_id": task["task_id"], "stage": role,
                    "error_code": str(exc), "claim_ids": [c.claim_id for c in batch]})
                self._event("ERROR", "语义核验", "verification.protocol_failed",
                    task_id=task["task_id"], error_code=str(exc), claim_ids=[c.claim_id for c in batch])
                self._verification_failure(batch, "protocol_failed", str(exc), profile)
                self._checkpoint()
            except _Stopped as exc:
                self._verification_failure(batch, "failed", exc.reason, profile)
                raise

    def _verification_failure(self, claims: list[Any], status: str, code: str, profile: Any) -> None:
        from datetime import UTC, datetime

        from .chapter_contracts import (
            POLICY_VERSION,
            VerificationReceipt,
            canonical_hash,
            source_snapshot_hash,
        )

        snapshot = source_snapshot_hash(self.units, list(self.documents))
        for claim in claims:
            receipt = VerificationReceipt(receipt_id="receipt-failure-"+uuid.uuid4().hex,
                claim_id=claim.claim_id, claim_revision=claim.revision,
                claim_revision_hash=claim.revision_hash, execution_status=status,
                verdict=None, reason=code, evidence_hashes={}, calculation_hashes={},
                read_evidence_ids=[], read_ranges={}, program_issues=[code], issue_fields=[],
                follow_up_queries=[], policy_version=POLICY_VERSION,
                prompt_hash=profile.system_hash, source_snapshot_hash=snapshot,
                verifier_model=getattr(getattr(self.client, "config", None), "model", None),
                created_at_utc=datetime.now(UTC).isoformat())
            value = _dump(receipt)
            self.state["receipts"].append(value)
            ref = self.journal.artifact(f"claims/{claim.claim_id}/{receipt.receipt_id}.json", value)
            self._event("ERROR", "语义核验", "claim.verification_failed", claim_ids=[claim.claim_id],
                        execution_status=status, error_code=code, output_artifact_ref=ref,
                        output_hash=canonical_hash(value))

    def _admitted(self) -> tuple[dict, dict]:
        from .chapter_contracts import validate_receipt

        profile, _ = self._profile(self.verifier_role)
        claims, receipts = self._claim_maps()
        admitted = {}
        for key, receipt in receipts.items():
            if (not validate_receipt(receipt, claims[key], self.units, list(self.documents),
                                     self.calculations, profile.system_hash)
                    and not _counterevidence_issues(key, _dump(receipt),
                        self.state.get("claim_required_counterevidence", {}), self.units)):
                admitted[key] = claims[key]
        self.state["publishable_claim_ids"] = list(admitted)
        return admitted, receipts

    def _repair(self) -> None:
        claims, receipts = self._claim_maps()
        repair_tasks = []
        for claim_id, receipt in receipts.items():
            if (receipt.verdict == "supported" and not receipt.program_issues
                    and not receipt.invalidated_by):
                continue
            old = claims[claim_id]
            if old.revision > self.limits["max_repair_rounds"]:
                continue
            task = self._task({"task_id": old.task_id, "kind": "chapter",
                "objective": "只修正指定判断、补取反证或保留不足；同claim_id且revision加一。",
                "document_ids": list(self.documents), "chapter_ids": [],
                "evidence_ids": [], "topic": None},
                [{"evidence_id": ref.evidence_id, "start": max(0, ref.start-300),
                  "end": min(len(self.units[ref.evidence_id].text), ref.end+300)}
                 for ref in old.evidence_refs if ref.evidence_id in self.units])
            task["repair_of"] = claim_id
            task["limitations"] = [receipt.reason, *receipt.follow_up_queries]
            task["objective"] += f" claim_id={claim_id}, revision={old.revision+1}；原判断：{old.statement}"
            if receipt.follow_up_queries:
                self._execute_tools([{"call_id": "repair-search-"+uuid.uuid4().hex,
                    "name": "search_evidence", "arguments": {"query": query,
                    "document_ids": list(self.documents), "top_k": self.limits["max_task_units"]}}
                    for query in receipt.follow_up_queries[:self.limits["max_tools_per_round"]]], task)
            repair_tasks.append(task)
        for task in repair_tasks:
            self._run_task(task)
            self.state.setdefault("repair_tasks", []).append(task)

    def _synthesize(self) -> None:
        from .chapter_contracts import SynthesisReport, validate_publication

        admitted, receipts = self._admitted()
        if not admitted:
            self.state["limitations"].append("没有通过逐条核验的判断；不生成成立结论。")
            return
        task = self._task({"task_id": "synthesis", "kind": "synthesis",
            "objective": "汇总已核验判断，检查跨任务冲突；新增判断单列待核验。",
            "document_ids": list(self.documents), "chapter_ids": [], "evidence_ids": []})
        profile, _ = self._profile(self.verifier_role)
        for revision in range(self.limits["max_repair_rounds"]+1):
            for _ in range(self.limits["max_task_rounds"]):
                payload = self._payload(task, {"admitted_claims": [_dump(c) for c in admitted.values()],
                    "coverage": self._coverage(), "missing_documents": self.state["missing_documents"],
                    "previous_conflicts": self.state.get("conflict_history", []),
                    "unresolved_claims": [{"claim_id": c["claim_id"], "statement": c["statement"]}
                        for c in self.state["claims"] if c["claim_id"] not in admitted],
                    "known_claims": [], "limitations": self.state["limitations"]})
                turn = self._call("report_synthesis_v1", payload, task)
                if turn["tool_requests"]:
                    self._execute_tools(turn["tool_requests"], task)
                    continue
                synthesis = SynthesisReport.model_validate(turn["synthesis"])
                if synthesis.conflicts:
                    known_claims, _ = self._claim_maps()
                    for conflict in synthesis.conflicts:
                        if (not set(conflict.claim_ids).issubset(known_claims)
                                or not set(conflict.evidence_ids).issubset(self.units)):
                            raise _ProtocolError("CHAPTER_SYNTHESIS_CONFLICT_IDENTITY_INVALID")
                        for key in conflict.claim_ids:
                            self._invalidate_claim(key, "synthesis_conflict:"+_hash(_dump(conflict)))
                            required = self.state.setdefault("claim_required_counterevidence", {}).setdefault(key, [])
                            required.extend(e for e in conflict.evidence_ids if e not in required)
                    self.state.setdefault("conflict_history", []).extend(_dump(c) for c in synthesis.conflicts)
                    self.state["synthesis_candidate"] = _dump(synthesis)
                    self._checkpoint()
                    if revision >= self.limits["max_repair_rounds"]:
                        self.state["publication_errors"] = ["SYNTHESIS_UNRESOLVED_CLAIM_CONFLICTS"]
                        return
                    self._verify()
                    self._repair()
                    self._run_pending()
                    self._verify()
                    admitted, receipts = self._admitted()
                    break
                if synthesis.new_claims:
                    if revision >= self.limits["max_repair_rounds"]:
                        self.state["limitations"].append("汇总新增判断达到核验轮次边界，未作为成立结论发布。")
                        self.state["publication_errors"] = ["SYNTHESIS_NEW_CLAIMS_REQUIRE_VERIFICATION"]
                        self.state["synthesis_candidate"] = _dump(synthesis)
                        return
                    # New synthesis claims have their own task and are still
                    # subject to exact-source reads, program checks and receipts.
                    for raw in synthesis.new_claims:
                        if raw.task_id != task["task_id"]:
                            raise _ProtocolError("CHAPTER_SYNTHESIS_NEW_CLAIM_TASK_MISMATCH")
                    self._accept_claims([_dump(c) for c in synthesis.new_claims], task)
                    self._verify()
                    admitted, receipts = self._admitted()
                    break
                claims, receipts = self._claim_maps()
                issues = validate_publication(synthesis, claims, receipts, self.units,
                    list(self.documents), self.calculations, profile.system_hash)
                issues.extend(_rebuild_calculation_issues(list(self.calculations.values()),
                    [_dump(unit) for unit in self.units.values()], list(self.documents.values())))
                self.state["synthesis_candidate"] = _dump(synthesis)
                self.state["publication_errors"] = issues
                if issues:
                    self.state["errors"].append({"stage": "synthesis", "error_code": "CHAPTER_PUBLICATION_GATE",
                                                "issues": issues})
                    return
                reported_ids = {key for section in synthesis.sections for judgment in section.judgments
                                for key in judgment.claim_ids}
                if set(admitted) - reported_ids:
                    self.state["publication_errors"] = [
                        "REPORT_SUPPORTED_CLAIMS_OMITTED:"+key for key in sorted(set(admitted)-reported_ids)]
                    return
                self.state["synthesis"] = _dump(synthesis)
                self.state["report_markdown"] = self._render_report(synthesis)
                ref = self.journal.artifact("report/report-"+_hash(_dump(synthesis))+".json", _dump(synthesis))
                self._event("INFO", "汇总", "report.validated", output_artifact_ref=ref,
                    claim_ids=[key for s in synthesis.sections for j in s.judgments for key in j.claim_ids])
                return
            else:
                self.state["limitations"].append("汇总达到工具轮次边界。")
                return

    def _coverage(self) -> dict:
        ledger = self.state["material_ledger"]
        topics = []
        for task in self.state["tasks"]:
            if task["kind"] == "financial_topic":
                topics.append({"task_id": task["task_id"], "topic": task["topic"],
                    "document_id": task.get("origin_document_id"), "status": task["status"],
                    "claim_ids": task["claim_ids"], "stop_reason": task.get("stop_reason")})
        page_coverage = [{"document_id": key, "coverage": value.get("coverage", {})}
                         for key, value in self.documents.items()]
        return {"unit_count": len(ledger), "retrieved_unit_count": sum(bool(v["retrieved_by"]) for v in ledger.values()),
            "fully_checked_unit_count": sum(v["fully_checked"] for v in ledger.values()),
            "cited_unit_count": sum(bool(v["referenced_by"]) for v in ledger.values()),
            "unprocessed_evidence_ids": [key for key, v in ledger.items() if not v["fully_checked"]],
            "topics": topics, "page_extraction": page_coverage,
            "coverage_is_not_understanding_certification": True}

    def _render_report(self, synthesis: Any) -> str:
        return _render_chapter_markdown(_dump(synthesis), self.state["claims"], self.units,
            self.calculations, self.state["missing_documents"], self._coverage())


    def run(self) -> dict:
        self._event("INFO", "规划", "workflow.started", input_hash=self.input_hash,
                    workflow_version=WORKFLOW_VERSION)
        self._checkpoint()
        stop_reason = None
        try:
            self._stop_check()
            self._seed_tasks()
            self._plan()
            self._run_pending()
            self._uncovered()
            self._run_pending()
            self._verify()
            self._repair()
            self._run_pending()
            self._verify()
            self._synthesize()
        except _Stopped as exc:
            stop_reason = exc.reason
        except _ProtocolError as exc:
            self.state["errors"].append({"stage": "workflow", "error_code": str(exc)})
            stop_reason = "protocol_failed"
        self.state["coverage"] = self._coverage()
        self.state["stop_reason"] = stop_reason
        if stop_reason == "cancelled":
            status = "cancelled"
        elif stop_reason in {"provider_outcome_unknown", "journal_integrity_failure", "protocol_failed",
                            "calculation_integrity_failure"}:
            status = "partial_result"
        elif self.state["missing_documents"]:
            status = "waiting_for_documents"
        elif (stop_reason or self.state["errors"] or self.state["publication_errors"]
                or any(t["status"] != "succeeded" for t in self.state["tasks"])
                or self.state["coverage"]["unprocessed_evidence_ids"]
                or any(d.get("coverage", {}).get("pages_without_material") for d in self.documents.values())
                or len(self.state["publishable_claim_ids"]) != len(self.state["claims"])
                or not self.state.get("synthesis")):
            status = "partial_result"
        else:
            status = "completed"
        self.state["status"] = status
        self.state["execution_status"] = status
        self.state["provider_calls"] = [{"call_id": call["call_id"], "task_id": call["task_id"],
            "provider_metadata": call.get("metadata", {}), "status": call["status"]}
            for call in self.state["calls"]]
        self.state["summary"] = {"status": status, "claims": len(self.state["claims"]),
            "supported_claims": len(self.state["publishable_claim_ids"]),
            "missing_documents": len(self.state["missing_documents"]),
            "unprocessed_units": len(self.state["coverage"]["unprocessed_evidence_ids"]),
            "stop_reason": stop_reason}
        self._event("INFO" if status == "completed" else "WARNING", "汇总", "workflow.finished",
                    execution_status=status, message=stop_reason,
                    claim_ids=self.state["publishable_claim_ids"])
        self._checkpoint()
        return self.state


def _render_chapter_markdown(synthesis: dict, raw_claims: list[dict], units: dict,
                         calculations: dict, missing_documents: list[dict], coverage: dict) -> str:
    claims = {c["claim_id"]: c for c in raw_claims}
    lines = ["# 财报分析结果", "", "逐条判断经本流程证据核验；不构成审计认证。", ""]
    lines.extend(["## 分析范围", "",
        "完成状态仅针对本次冻结的选页、材料和任务，不表示整份财报或全部财务问题已核验。", ""])
    for document in coverage.get("page_extraction", []):
        pages = document.get("coverage", {})
        lines.extend([f"报告：`{document['document_id']}`；总页数：{pages.get('total_pages', pages.get('page_count', 'unknown'))}",
            (f"选择页：{_page_ranges(pages.get('expected_pages', []))}；"
            f"可用页：{_page_ranges(pages.get('available_pages', pages.get('extracted_pages', [])))}；"
            f"未选页：{_page_ranges(pages.get('unselected_pages', []))}；"
             f"选页内无材料：{_page_ranges(pages.get('pages_without_material', []))}"), ""])
    lines.extend(["## 专题执行状态", ""])
    for topic in coverage.get("topics", []):
        lines.append(f"- `{topic['document_id']}` / `{topic['topic']}`：{topic['status']}"
                     +(f"；原因：{topic['stop_reason']}" if topic.get("stop_reason") else ""))
    lines.append("")
    for section in synthesis["sections"]:
        # Model-provided titles are retained in the candidate artifact but
        # not used as an unverified channel for financial conclusions.
        lines.extend(["## 分析事项", ""])
        for judgment in section["judgments"]:
            key = judgment["claim_ids"][0]
            claim = claims[key]
            lines.extend([judgment["text"], "", f"判断：`{key}`；修订：{claim['revision']}", ""])
            scope = {key: value for key, value in claim.get("scope", {}).items()
                     if value is not None and value != []}
            if scope:
                lines.extend(["适用口径："+json.dumps(scope, ensure_ascii=False, sort_keys=True), ""])
            for assumption in claim.get("assumptions", []):
                lines.extend(["假设："+assumption, ""])
            for limitation in claim.get("limitations", []):
                lines.extend(["限定："+limitation, ""])
            for ref in claim["evidence_refs"]:
                unit = units[ref["evidence_id"]]
                lines.extend([f"> {ref['quote']}", "",
                    (f"来源：{unit.document_id} / {unit.chapter_id} / 页 {unit.page or 'unknown'} / "
                     f"`{unit.unit_id}` [{ref['start']},{ref['end']})"), ""])
            for calculation_id in claim["calculation_ids"]:
                calculation = calculations[calculation_id]
                lines.extend([f"程序计算：`{calculation_id}`；{calculation['formula']} = {calculation['value']}", ""])
                lines.extend(["计算口径："+json.dumps(calculation.get("scope", {}), ensure_ascii=False), ""])
                for assumption in calculation.get("assumptions", []):
                    lines.extend(["计算条件："+assumption, ""])
    if synthesis.get("unresolved_items") or missing_documents or coverage.get("unprocessed_evidence_ids"):
        lines.extend(["## 未解决事项", ""])
        # Free-form model limitations are candidates, so the report only
        # renders structured missing-document records and state-derived gaps.
        for missing in missing_documents:
            lines.append(f"- 待补材料：{missing['report_name']}；期间：{missing.get('period') or 'unknown'}；"
                         f"所需内容：{missing['required_content']}")
        lines.append(f"- 尚未完成检查的材料单元：{len(coverage.get('unprocessed_evidence_ids', []))}")
        if synthesis.get("unresolved_items"):
            lines.append(f"- 汇总模型登记未核验待办：{len(synthesis['unresolved_items'])}项；"
                         "具体内容保存在汇总候选工件，不作为成立结论。")
    return "\n".join(lines)


def _page_ranges(pages: list[int]) -> str:
    if not pages:
        return "无或未登记"
    result = []
    start = previous = min(pages)
    for page in sorted(set(pages))[1:]:
        if page == previous+1:
            previous = page
        else:
            result.append(str(start) if start == previous else f"{start}-{previous}")
            start = previous = page
    result.append(str(start) if start == previous else f"{start}-{previous}")
    return ", ".join(result)


def _counterevidence_issues(claim_id: str, receipt: dict, required: dict, units: dict) -> list[str]:
    issues = []
    for key in required.get(claim_id, []):
        if key not in units:
            issues.append(f"COUNTEREVIDENCE_UNKNOWN:{key}")
        elif not _covers(receipt.get("read_ranges", {}).get(key, []), 0, len(units[key].text)):
            issues.append(f"COUNTEREVIDENCE_NOT_FULLY_READ:{key}")
    return issues


class _ReplayJournal:
    """Memory-only sink for deterministic recalculation, never model dispatch."""

    @staticmethod
    def inspect_recovery() -> dict:
        return {}

    @staticmethod
    def emit(*args: Any, **kwargs: Any) -> dict:
        return {}

    @staticmethod
    def artifact(path: str, value: Any) -> dict:
        return {"path": path, "sha256": _hash(value)}


def _rebuild_calculation_issues(records: list[dict], units: list[dict], documents: list[dict]) -> list[str]:
    if not records:
        return []
    issues = []
    try:
        engine = _Engine(units, documents, None, _ReplayJournal(), None, None, None)
        for record in records:
            key = record.get("calculation_id", "unknown")
            try:
                rebuilt = engine._calculate({"formula": record["formula"],
                    "fact_ids": [operand["fact_id"] for operand in record["operands"]]}, set(engine.documents))
                if _hash(rebuilt) != _hash(record):
                    issues.append(f"REPLAY_CALCULATION_MISMATCH:{key}")
            except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
                issues.append(f"REPLAY_CALCULATION_CANNOT_REBUILD:{key}:{exc}")
    except (ValueError, KeyError, TypeError) as exc:
        issues.append("REPLAY_CALCULATION_SOURCE_INVALID:"+str(exc))
    return issues



def run_chapter_workflow(units: list[dict], documents: list[dict], client: Any,
                         journal: Any, limits: dict | None = None,
                         calculations: list[dict] | None = None,
                         previous: dict | None = None) -> dict:
    """Run only frozen authorized source units, using the caller's model client.

    Unknown provider outcomes block automatic continuation. ``previous`` is a
    persisted checkpoint from this exact input/prompt policy, not a new source
    import. Added files must start a new version and invalidate old receipts.
    """
    return _Engine(units, documents, client, journal, limits, calculations, previous).run()


def validate_chapter_result(result: dict, units: list[dict], calculations: list[dict] | None = None,
                            documents: list[dict] | None = None) -> list[str]:
    """Offline publication recheck. No external model calls or state mutation."""
    from .chapter_contracts import (
        ChapterClaim,
        EvidenceUnit,
        SynthesisReport,
        VerificationReceipt,
        validate_publication,
    )
    from .chapter_prompts import get_prompt_profile

    issues = []
    if result.get("workflow_version") != WORKFLOW_VERSION:
        return ["CHAPTER_REPLAY_WORKFLOW_VERSION_MISMATCH"]
    try:
        registry = {u.unit_id: u for raw in units if (u := EvidenceUnit.model_validate(raw))}
        claims = {c.claim_id: c for raw in result.get("claims", []) if (c := ChapterClaim.model_validate(raw))}
        receipts = {}
        for raw in result.get("receipts", []):
            receipt = VerificationReceipt.model_validate(raw)
            claim = claims.get(receipt.claim_id)
            if claim and receipt.claim_revision_hash == claim.revision_hash:
                receipts[receipt.claim_id] = receipt
        current_docs = documents or result.get("documents", [])
        ids = [str(d.get("document_id") or d.get("id")) for d in current_docs]
        records_for_replay = calculations if calculations is not None else result.get("calculations", [])
        issues.extend(_rebuild_calculation_issues(records_for_replay, units, current_docs))
        saved_manifest = result.get("source_manifest", {})
        if saved_manifest != {key: unit.content_hash for key, unit in registry.items()}:
            issues.append("CHAPTER_REPLAY_SOURCE_MANIFEST_CHANGED")
        if _hash(current_docs) != _hash(result.get("documents", [])):
            issues.append("CHAPTER_REPLAY_DOCUMENT_IDENTITY_CHANGED")
        if not result.get("synthesis"):
            issues.append("CHAPTER_REPLAY_NO_PUBLISHED_SYNTHESIS")
        else:
            synthesis = SynthesisReport.model_validate(result["synthesis"])
            issues.extend(validate_publication(synthesis, claims, receipts, registry, ids,
                calculations if calculations is not None else result.get("calculations", []),
                get_prompt_profile(_saved_verifier_role(result)).system_hash))
            for section in synthesis.sections:
                for judgment in section.judgments:
                    for claim_id in judgment.claim_ids:
                        if claim_id in receipts:
                            issues.extend(_counterevidence_issues(claim_id, _dump(receipts[claim_id]),
                                result.get("claim_required_counterevidence", {}), registry))
            records = {c["calculation_id"]: c for c in (calculations if calculations is not None
                       else result.get("calculations", []))}
            expected_markdown = _render_chapter_markdown(_dump(synthesis), result.get("claims", []),
                registry, records, result.get("missing_documents", []), result.get("coverage", {}))
            if result.get("report_markdown") != expected_markdown:
                issues.append("CHAPTER_REPLAY_REPORT_RENDER_MISMATCH")
    except (ValueError, KeyError, TypeError) as exc:
        issues.append("CHAPTER_REPLAY_INVALID_CONTRACT:"+str(exc))
    return sorted(set(issues))



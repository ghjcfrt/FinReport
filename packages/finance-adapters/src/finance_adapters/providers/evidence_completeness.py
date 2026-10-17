"""Deterministic, source-bound coverage of disclosed financial table rows.

This is a coverage inventory, never an admission of OCR/text amounts as audited
facts. Unsupported/ambiguous layouts remain explicit gaps, not empty successes.
The first supported family is non-recurring-item composition, including its
deductions and total; there are no issuer-specific labels or expected row counts.
"""
from __future__ import annotations

import hashlib
import re
from decimal import Decimal
from itertools import pairwise

from finance_adapters.persistence.audit import stable_hash

MANIFEST_VERSION = "source_bound_financial_rows_v1"
REPAIR_LIMIT = 2
OBLIGATION = "non_recurring_composition"
_DIGITS = r"(?:\d{1,3}(?:[,，]\d{3})+|\d+)(?:\.\d+)?"
_NUMBER = re.compile(rf"[-−＋+]?{_DIGITS}|\({_DIGITS}\)|（{_DIGITS}）|[-—–]")
_FAMILY = re.compile(r"非经常性损益(?:项目|明细)|非经常性损益项目及金额")
_TOTAL = re.compile(r"(?:非经常性损益)?(?:合计|净额|总额)(?:[（(].*?[）)])?")


def _amount(raw: str) -> str | None:
    raw = raw.strip()
    if raw in {"", "-", "—", "–"}:
        return None
    negative = raw.startswith(("(", "（")) and raw.endswith((")", "）"))
    value = Decimal(raw.strip("()（）").replace(",", "").replace("，", "").replace("−", "-").replace("＋", "+"))
    return str(-value if negative else value)


def _records(text: str) -> list[tuple[int, int, str]]:
    records, offset = [], 0
    for line in text.splitlines(keepends=True):
        raw = line.rstrip("\r\n")
        if raw.strip():
            records.append((offset, offset + len(raw), raw.strip()))
        offset += len(line)
    return records


def package_documents(package: dict) -> list[dict]:
    """Reconstruct only immutable period/scope descriptors used by item IDs."""
    descriptors = [dict(item) for item in (package.get("report_measure_bundle") or {}).get("documents", [])]
    for item in package.get("document_contexts", []):
        if not any(value["filing_version_id"] == item["filing_version_id"] for value in descriptors):
            descriptors.append(dict(item))
    anchor = {"filing_version_id": package["filing_version_id"], **package.get("analysis_scope", {})}
    existing = next((item for item in descriptors if item["filing_version_id"] == anchor["filing_version_id"]), None)
    if existing is not None:
        existing.update({key: value for key, value in anchor.items() if value is not None})
    else:
        descriptors.append(anchor)
    return descriptors


def _family(source: dict) -> bool:
    box = source.get("bounding_box") or {}
    headings = [source.get("section", ""), *box.get("heading_path", [])]
    return bool(any(_FAMILY.search(str(value)) for value in headings)
                or re.search(r"非经常性损益(?:项目及金额|明细表)", source.get("text", "")))


def _explicit_declaration(source: dict) -> bool:
    """Only a whole, closed negative question/statement; never 'no others'."""
    text = source["text"].strip()
    if (hashlib.sha256(source["text"].encode()).hexdigest() != source.get("text_sha256")
            or source.get("review_status") == "rejected"
            or source.get("question_completeness") == "incomplete_question"
            or source.get("selection_status") == "ambiguous"):
        return False
    if re.fullmatch(r"(?:(?:本公司|公司|本集团)(?:于)?(?:本报告期|报告期内|报告期|本期)?|"
                    r"(?:本报告期|报告期内|报告期|本期)(?:本公司|公司|本集团)?)"
                    r"(?:不存在|无|没有)非经常性损益(?:项目)?[。]", re.sub(r"\s+", "", text)):
        return True
    lines = text.splitlines()
    if (len(lines) != 2 or not re.fullmatch(r"(?:[一二三四五六七八九十\d]+、)?非经常性损益项目及金额[？?]?", lines[0].strip())
            or source.get("question_completeness") not in {"complete", "complete_question"}):
        return False
    option = re.compile(r"([□☐☑✓✔√\uf052\uf0fe])\s*(不适用|适用)")
    matches = list(option.finditer(lines[1]))
    return (len(matches) == 2 and not option.sub("", lines[1]).strip()
            and {match[2] for match in matches} == {"适用", "不适用"}
            and [match[2] for match in matches if match[1] not in {"□", "☐"}] == ["不适用"])


def _parse_rows(source: dict) -> tuple[list[dict], list[str], list[str]]:
    """Recognize line-oriented/Markdown tables; reject unexplained leftovers."""
    text = source["text"]
    if (hashlib.sha256(text.encode()).hexdigest() != source.get("text_sha256")
            or source.get("review_status") == "rejected"):
        return [], ["source_hash_or_review_invalid"], []
    records = _records(text)
    rows, issues, headers = [], [], []
    labels = []
    markdown_header, amount_positions = None, []
    index = 0
    for _, _, line in records:
        cells = [cell.strip() for cell in line.strip().removeprefix("|").removesuffix("|").split("|")]
        if "|" in line and cells[0] == "项目":
            markdown_header = cells
            amount_positions = [position for position, cell in enumerate(cells[1:], 1)
                                if re.search(r"金额|发生额|20\d{2}\s*年", cell)]
            if amount_positions:
                headers = [cells[position] for position in amount_positions]
            continue
        if "|" in line:
            continue
        if not _NUMBER.fullmatch(line) and ("金额" in line or re.fullmatch(r"20\d{2}\s*年(?:度)?", line)):
            headers.extend(cell for cell in cells if re.search(r"金额|20\d{2}\s*年", cell))
    while index < len(records):
        start, end, line = records[index]
        if "|" in line:
            cells = [cell.strip() for cell in line.strip().removeprefix("|").removesuffix("|").split("|")]
            if not cells[0] or cells[0] == "项目" or all(re.fullmatch(r"[:\- ]+", cell) for cell in cells):
                index += 1
                continue
            positions = amount_positions or list(range(1, len(cells)))
            if markdown_header is not None and len(cells) != len(markdown_header):
                issues.append("markdown_column_count_mismatch")
            # Keep the physical position of empty cells. Dropping blanks moves
            # comparative amounts into a different fiscal period.
            numeric = [cells[position] if position < len(cells) else "" for position in positions]
            if numeric and all(not cell or _NUMBER.fullmatch(cell) for cell in numeric) and re.search(r"[\u4e00-\u9fff]", cells[0]):
                if labels:
                    issues.append("unparsed_label_before_markdown_row")
                    labels = []
                rows.append({"start": start, "end": end, "raw_label": cells[0], "raw_amounts": numeric,
                             "amount_columns": [position - 1 for position in positions]})
            else:
                issues.append("unsupported_markdown_row")
            index += 1
            continue
        if (_FAMILY.search(line) or line in {"项目", "金额", "说明", "说 明", "本期发生额", "本期金额", "上期金额"}
                or re.fullmatch(r"20\d{2}\s*年(?:度)?(?:金额)?", line)
                or re.fullmatch(r"单位[：:].{1,20}", line)):
            index += 1
            continue
        if _NUMBER.fullmatch(line):
            if not labels:
                issues.append("amount_without_row_label")
                index += 1
                continue
            numeric = []
            while index < len(records) and _NUMBER.fullmatch(records[index][2]):
                numeric.append(records[index][2])
                end = records[index][1]
                index += 1
            rows.append({"start": labels[0][0], "end": end,
                         "raw_label": "\n".join(label for _, label in labels), "raw_amounts": numeric})
            labels = []
            # A short explicit explanation is a separate column, not a new row.
            if index < len(records) and records[index][2] in {"无", "不适用", "无说明"}:
                rows[-1]["end"] = records[index][1]
                index += 1
            continue
        if re.fullmatch(r"[\d,，.()（）+−—–-]+", line):
            issues.append("malformed_amount")
            index += 1
            continue
        if line.endswith(("。", "？", "?", "：", ":")) or "□" in line or "是否" in line:
            if labels:
                issues.append("unparsed_label_or_note")
            # Prose/question blocks are not silently interpreted as table rows.
            if rows:
                issues.append("table_mixed_with_unbounded_prose")
            break
        labels.append((start, line))
        index += 1
    if labels:
        issues.append("unfinished_row_label")
    return rows, sorted(set(issues)), list(dict.fromkeys(headers))


def row_source_units(source: dict) -> list[dict]:
    """Expose exact full rows as quote units; this does not certify the table."""
    if not _family(source) or source.get("question_completeness") == "incomplete_question":
        return []
    rows, issues, headers = _parse_rows(source)
    if issues:
        return []
    return [{"start": row["start"], "end": row["end"], "kind": "evidence_table_row",
             "policy": MANIFEST_VERSION, "title": row["raw_label"], "column_labels": headers,
             "text_sha256": hashlib.sha256(source["text"][row["start"]:row["end"]].encode()).hexdigest()}
            for row in rows]


def build_manifest(sources: list[dict], documents: list[dict] | None = None) -> dict:
    documents = documents or []
    by_document = {item["filing_version_id"]: item for item in documents}
    document_ids = sorted(set(by_document) | {source["filing_version_id"] for source in sources})
    groups, items = [], []
    for document_id in document_ids:
        candidates = [source for source in sources if source["filing_version_id"] == document_id and _family(source)]
        declarations = [source for source in sources if source["filing_version_id"] == document_id
                        and _explicit_declaration(source)]
        # A title or an unrelated negative declaration is not a composition table.
        tables = [source for source in candidates
                  if source.get("question_completeness") != "incomplete_question"
                  and source.get("selection_status") != "ambiguous"
                  and (any(_NUMBER.fullmatch(line) for _, _, line in _records(source["text"]))
                       or any("|" in line and re.search(r"\d", line) for _, _, line in _records(source["text"])))]
        tables.sort(key=lambda source: (source.get("page_no", 0), source["id"]))
        group_id = "manifest-" + stable_hash({"filing_version_id": document_id, "obligation_id": OBLIGATION})[:24]
        issues, table_rows, column_counts = [], [], set()
        pages = sorted({source["page_no"] for source in tables})
        if not tables and not declarations:
            issues.append("composition_table_not_retrieved")
        if tables and declarations:
            issues.append("negative_declaration_conflicts_with_table")
        if any(right - left > 1 for left, right in pairwise(pages)):
            issues.append("table_pages_not_contiguous")
        document = by_document.get(document_id, {})
        periods = {"period_start": document.get("period_start"), "period_end": document.get("period_end"),
                   "scope": document.get("scope")}
        unit_candidates = []
        for source in candidates:
            if any(abs(source.get("page_no", 0) - page) <= 1 for page in pages):
                unit_candidates.extend(re.findall(r"单位[：:]\s*([^\s\n|]{1,12})", source["text"]))
        units = sorted(set(unit_candidates))
        if len(units) > 1:
            issues.append("conflicting_table_units")
        duplicates = {}
        for source in tables:
            parsed, source_issues, headers = _parse_rows(source)
            issues.extend(f"{source['id']}:{issue}" for issue in source_issues)
            for row in parsed:
                count = len(row["raw_amounts"])
                column_counts.add(count)
                label = re.sub(r"\s+", "", row["raw_label"])
                role = "total" if _TOTAL.fullmatch(label) else "deduction" if re.search(r"所得税影响|少数股东.{0,8}影响|^减[：:]", label) else "component"
                for ordinal, raw in enumerate(row["raw_amounts"]):
                    column = row.get("amount_columns", list(range(count)))[ordinal]
                    column_label = headers[ordinal] if len(headers) == count else "金额" if count == 1 else None
                    item_period = dict(periods)
                    year = re.search(r"(20\d{2})\s*年", column_label or "")
                    if year:
                        # A bare year does not turn an interim column into an
                        # annual period. Preserve the report's month/day span.
                        if item_period["period_start"] and item_period["period_end"]:
                            item_period.update(period_start=year[1] + item_period["period_start"][4:],
                                               period_end=year[1] + item_period["period_end"][4:])
                        else:
                            issues.append("year_column_span_not_resolved")
                    elif count != 1:
                        item_period.update(period_start=None, period_end=None)
                    if count > 1 and not year:
                        issues.append("comparative_column_period_not_resolved")
                    normalized_value = _amount(raw)
                    key = (label, column, Decimal(normalized_value) if normalized_value is not None else None,
                           tuple(item_period.items()))
                    reference = {"source_id": source["id"], "source_text_sha256": source["text_sha256"],
                                 "source_sha256": source.get("source_sha256"), "page_no": source.get("page_no"),
                                 "start": row["start"], "end": row["end"],
                                 "raw_text": source["text"][row["start"]:row["end"]],
                                 "row_text_sha256": hashlib.sha256(source["text"][row["start"]:row["end"]].encode()).hexdigest()}
                    if key in duplicates:
                        duplicates[key]["source_refs"].append(reference)
                        continue
                    item = {"manifest_id": group_id, "obligation_id": OBLIGATION,
                            "filing_version_id": document_id, "raw_label": row["raw_label"],
                            "raw_amount": raw, "value": _amount(raw), "column_index": column,
                            "column_label": column_label, "period": item_period,
                            "unit": units[0] if len(units) == 1 else None, "row_role": role,
                            "source_refs": [reference], "review_status": source.get("review_status", "unreviewed")}
                    # Identity binds the disclosed row; document/period/unit
                    # metadata remain separately frozen in the manifest hash.
                    item["id"] = "evidence-item-" + stable_hash({
                        "filing_version_id": document_id, "source_sha256": source.get("source_sha256"),
                        "label": label, "value": format(Decimal(normalized_value).normalize(), "f") if normalized_value is not None else None,
                        "column_index": column, "row_role": role, "period": item_period})[:28]
                    duplicates[key] = item
                    table_rows.append(item)
        if len(column_counts) > 1:
            issues.append("inconsistent_column_counts")
        if not periods["period_start"] or not periods["period_end"]:
            issues.append("document_period_not_resolved")
        total_rows = [item for item in table_rows if item["row_role"] == "total"]
        if not total_rows and not declarations:
            issues.append("total_row_or_continuation_not_retrieved")
        reconciliation = []
        for column in sorted({item["column_index"] for item in table_rows}):
            rows = [item for item in table_rows if item["column_index"] == column]
            totals = [item for item in rows if item["row_role"] == "total"]
            if len(totals) != 1:
                issues.append("total_row_not_unique")
                continue
            components = [item for item in rows if item["row_role"] != "total"]
            # Empty cells are unknown, not implicit zeros in a proof of closure.
            if any(item["value"] is None for item in rows):
                issues.append("blank_amount_prevents_total_validation")
                continue
            value = sum(((-Decimal(item["value"]) if item["row_role"] == "deduction" else Decimal(item["value"]))
                         for item in components), Decimal(0))
            balanced = value == Decimal(totals[0]["value"])
            reconciliation.append({"column_index": column, "computed_total": str(value),
                                   "disclosed_total": totals[0]["value"], "balanced": balanced})
            if not balanced:
                issues.append("disclosed_total_does_not_reconcile")
        if not any(item["row_role"] == "component" for item in table_rows) and not declarations:
            issues.append("component_rows_not_retrieved")
        label_columns = [(re.sub(r"\s+", "", item["raw_label"]), item["column_index"]) for item in table_rows]
        if len(set(label_columns)) != len(label_columns):
            issues.append("conflicting_values_for_same_row")
        if declarations and not tables:
            # The closed declaration is the required evidence itself. It is
            # not a zero amount and does not erase a contradictory table.
            declaration = declarations[0]
            raw = declaration["text"]
            item = {"manifest_id": group_id, "obligation_id": OBLIGATION,
                    "filing_version_id": document_id, "raw_label": raw, "raw_amount": None,
                    "value": None, "column_index": 0, "column_label": None, "period": periods,
                    "unit": None, "row_role": "declaration", "review_status": declaration.get("review_status", "unreviewed"),
                    "source_refs": [{"source_id": declaration["id"], "source_text_sha256": declaration["text_sha256"],
                                     "source_sha256": declaration.get("source_sha256"), "page_no": declaration.get("page_no"),
                                     "start": 0, "end": len(raw), "raw_text": raw,
                                     "row_text_sha256": declaration["text_sha256"]}]}
            item["id"] = "evidence-item-" + stable_hash({"filing_version_id": document_id,
                "source_sha256": declaration.get("source_sha256"), "declaration_sha256": declaration["text_sha256"],
                "period": periods})[:28]
            table_rows.append(item)
            pages = sorted({source["page_no"] for source in declarations})
        items.extend(table_rows)
        groups.append({"id": group_id, "filing_version_id": document_id, "obligation_id": OBLIGATION,
                       "status": "verified_in_retrieved_scope" if not issues else "needs_confirmation",
                       "issues": sorted(set(issues)), "item_ids": [item["id"] for item in table_rows],
                       "page_numbers": pages, "reconciliation": reconciliation,
                       "unit_status": "located" if len(units) == 1 else "unknown" if not units else "conflicting",
                       "next_read_arguments": [{"filing_version_id": document_id, "page_numbers": [pages[-1] + 1]}
                                               ] if pages and not total_rows and not declarations else []})
    manifest = {"version": MANIFEST_VERSION, "groups": groups, "items": items,
                "boundary": "清单仅证明已取证范围的表格行闭合与覆盖；金额未经审核不得进入严格计算，不证明全文无遗漏或语义推断正确。"}
    manifest["hash"] = stable_hash(manifest)
    return manifest


def _primary_label(label: str) -> str:
    """Remove only complete trailing paired qualifications, never inner text."""
    value = re.sub(r"\s+", "", label)
    while value.endswith(("）", ")")):
        closing = value[-1]
        opening = "（" if closing == "）" else "("
        depth = 0
        cut = None
        for position in range(len(value) - 1, -1, -1):
            character = value[position]
            if character == closing:
                depth += 1
            elif character == opening:
                depth -= 1
                if depth == 0:
                    cut = position
                    break
            elif character in "（）()":
                return re.sub(r"\s+", "", label)
        if cut is None or cut == 0:
            break
        value = value[:cut]
    return value


def validate_item_assessments(candidate, manifest: dict, sources: dict[str, dict], *,
                              unique_primary_label: bool = False) -> None:
    assessments = getattr(candidate, "evidence_item_assessments", None) or []
    lookup = {item["id"]: item for item in manifest["items"]}
    seen = set()
    for assessment in assessments:
        value = assessment.model_dump() if hasattr(assessment, "model_dump") else assessment
        item = lookup.get(value["item_id"])
        if item is None or value["item_id"] in seen:
            raise ValueError("AGENT_EVIDENCE_ITEM_UNKNOWN_OR_DUPLICATE")
        seen.add(value["item_id"])
        if candidate.obligation_id != item["obligation_id"] or candidate.topic != "non_recurring_items":
            raise ValueError("AGENT_EVIDENCE_ITEM_OBLIGATION_MISMATCH")
        refs = [ref for ref in item["source_refs"] if ref["source_id"] in candidate.source_ids]
        if not refs:
            raise ValueError("AGENT_EVIDENCE_ITEM_SOURCE_REQUIRED")
        supported = False
        for ref in refs:
            source = sources.get(ref["source_id"], {})
            row = source.get("text", "")[ref["start"]:ref["end"]]
            if (source.get("text_sha256") != ref["source_text_sha256"]
                    or hashlib.sha256(row.encode()).hexdigest() != ref["row_text_sha256"]):
                continue
            if candidate.evidence_kind == "company_disclosure":
                excerpt = candidate.statement.removeprefix("公司披露：")
                if row not in excerpt:
                    continue
            supported = True
        if not supported:
            raise ValueError("AGENT_EVIDENCE_ITEM_NOT_SUPPORTED_BY_QUOTE")
        if value["reason"].strip() in {"已检查", "已完成", "已分析", "不适用", "无法判断", "见原文", "现有材料不足"}:
            raise ValueError("AGENT_EVIDENCE_ITEM_SPECIFIC_REASON_REQUIRED")
        anchor = "非经常性损益" if item["row_role"] == "declaration" else re.sub(r"\s+", "", item["raw_label"])
        reason = re.sub(r"\s+", "", value["reason"])
        primary = _primary_label(item["raw_label"])
        unique = (unique_primary_label and len(primary) >= 6 and primary != anchor
                  and sum(_primary_label(other["raw_label"]) == primary
                          for other in manifest["items"]
                          if other["filing_version_id"] == item["filing_version_id"]
                          and other["column_index"] == item["column_index"]) == 1)
        if (not (anchor in reason or unique and primary in reason)
                or not re.search(r"构成|扣减|税|归属|合计|汇总|核对|形成|来源|持续性|确认|口径|边界|声明|适用", reason)):
            raise ValueError("AGENT_EVIDENCE_ITEM_SPECIFIC_REASON_REQUIRED")
        if value["status"] == "insufficient_evidence" and not (
            re.search(r"缺少|尚需|仍需|还需|无法|不能|不足", reason)
            and re.search(r"形成原因|业务背景|持续性|确认条件|交易|税务|股权|归属|会计口径|依据|凭证|明细|补充说明|披露", reason)
        ):
            raise ValueError("AGENT_EVIDENCE_ITEM_SPECIFIC_GAP_REQUIRED")


def coverage_report(manifest: dict, findings: list, *, visible_ids=(), previously_sent_ids=()) -> dict:
    presented, disposed, disposition_statuses = {}, {}, {}
    visible, sent = set(visible_ids), set(previously_sent_ids) | set(visible_ids)
    for finding in findings:
        if getattr(finding, "obligation_id", None) != OBLIGATION:
            continue
        for item in manifest["items"]:
            refs = [ref for ref in item["source_refs"] if ref["source_id"] in finding.source_ids]
            if finding.evidence_kind == "company_disclosure" and any(
                ref["raw_text"] in finding.statement.removeprefix("公司披露：") for ref in refs
            ):
                presented.setdefault(item["id"], []).append(finding.id)
            # Exact row quotation was checked at admission; raw label + raw
            # amount alone never grants coverage without the bound item ID.
            for assessment in getattr(finding, "evidence_item_assessments", None) or []:
                value = assessment.model_dump() if hasattr(assessment, "model_dump") else assessment
                if value["item_id"] != item["id"] or not refs:
                    continue
                disposed.setdefault(item["id"], []).append(finding.id)
                disposition_statuses.setdefault(item["id"], set()).add(value["status"])
    rows = []
    for item in manifest["items"]:
        refs = {ref["source_id"] for ref in item["source_refs"]}
        state = "explicitly_addressed" if item["id"] in disposed else "sent_not_addressed" if refs & sent else "retrieved_not_sent"
        rows.append({"item_id": item["id"], "state": state, "presented_finding_ids": presented.get(item["id"], []),
                     "assessment_finding_ids": disposed.get(item["id"], []),
                     "disposition_statuses": sorted(disposition_statuses.get(item["id"], [])),
                     "currently_visible": bool(refs & visible), "ever_sent": bool(refs & sent),
                     "review_status": item["review_status"]})
    pending = [row["item_id"] for row in rows if not row["assessment_finding_ids"] or not row["presented_finding_ids"]]
    verified = bool(manifest["groups"]) and all(group["status"] == "verified_in_retrieved_scope" for group in manifest["groups"])
    return {"version": MANIFEST_VERSION, "manifest_hash": manifest["hash"], "manifest": manifest,
            "item_states": rows, "required_count": len(rows), "presented_count": len(presented),
            "explicitly_addressed_count": len(disposed), "pending_item_ids": pending,
            "discussed_count": sum("discussed" in values for values in disposition_statuses.values()),
            "insufficient_evidence_count": sum("insufficient_evidence" in values for values in disposition_statuses.values()),
            "semantic_analysis_status": "requires_semantic_review",
            "manifest_verified": verified, "complete": verified and bool(rows) and not pending,
            "coverage_ratio": len(set(disposed) & set(presented)) / len(rows) if rows and verified else None,
            "boundary": "已呈现/显式处置是可观察输出指标，不代表模型内部注意力、独立语义核验或财务人工审核通过。"}

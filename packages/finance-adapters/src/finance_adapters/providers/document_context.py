from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pymupdf
from sqlalchemy.orm import Session

from finance_adapters.parsing.statement_extractor import page_rows
from finance_adapters.persistence.audit import stable_hash
from finance_adapters.persistence.models import FilingVersion
from finance_adapters.security.file_gate import scanned_object_path

CONTEXT_VERSION = "native-context-v2"
MAX_CONTEXT_CHARACTERS = 18000
MAX_EXCERPTS = 48
MAX_EXCERPT_CHARACTERS = 1600
MAX_QUESTION_LINES = 32
CHECKED_OPTION = re.compile(r"[☑✓✔√\uf052\uf0fe][\s|]*(不适用|适用|否|是)")
QUESTION_START = re.compile(r"是否")
TOPIC = re.compile(
    r"持续经营|现金分红|未提出现金红利|变动原因|主要原因|非经常性|"
    r"会计政策变更|合并范围|资本化|研发投入|汇率风险|敏感性分析|价格竞争"
)
LIMITATIONS = [
    "原生文字上下文未经双人财务复核，不属于严格事实或严格比较期指标。",
    "程序逐页扫描不等于模型逐页阅读；模型只收到excerpts列出的摘录。",
    "摘录是按固定关键词和勾选项选择的局部上下文，不得据此声称全报告未披露。",
    "勾选答案必须连同问项解释；精确引用和选项匹配不等于完整语义核验。",
    "无法定位完整问项起点或跨页未闭合的问项标记incomplete_question，不可作为独立定性证据。",
]


def _page_lines(page, page_no: int) -> list[dict]:
    lines = []
    for row in page_rows(page):
        cells = sorted(row, key=lambda cell: cell.bbox[0])
        text = " | ".join(cell.text for cell in cells)
        lines.append({
            "id": f"p{page_no:03d}-l{len(lines) + 1:04d}", "text": text,
            "bbox": [min(c.bbox[0] for c in cells), min(c.bbox[1] for c in cells),
                     max(c.bbox[2] for c in cells), max(c.bbox[3] for c in cells)],
        })
    return lines


def extract_document_context(path: Path, source_sha256: str) -> dict:
    """Read native geometry, retaining checkbox answers with their question context."""
    candidates = []
    page_index = []
    with pymupdf.open(path) as document:
        for page_no, page in enumerate(document, 1):
            native = page.get_text("text")
            lines = _page_lines(page, page_no)
            page_hash = hashlib.sha256(native.encode("utf-8")).hexdigest()
            page_index.append({"page_no": page_no, "native_characters": len(native),
                               "line_count": len(lines), "native_text_sha256": page_hash})
            consumed = set()
            covered = set()
            windows = []
            checkbox_lines = [index for index, line in enumerate(lines)
                              if CHECKED_OPTION.search(line["text"])]
            for anchor_index, anchor in enumerate(checkbox_lines):
                boundary = checkbox_lines[anchor_index - 1] + 1 if anchor_index else 0
                earliest = max(boundary, anchor - MAX_QUESTION_LINES + 1)
                starts = [i for i in range(earliest, anchor + 1)
                          if QUESTION_START.search(lines[i]["text"])]
                # Retain the entire wrapped condition, rather than the last few lines.
                # Without an explicit question boundary, the fragment remains visible
                # for review but must not support an independent selection assertion.
                start = starts[0] if starts else earliest
                single_line_question = (anchor - boundary == 1
                                        and bool(re.search(r"[？?]$", lines[boundary]["text"]))
                                        and not re.match(r"^(?:且|或|并且|以及|但是|但)", lines[boundary]["text"]))
                complete = bool(starts) or single_line_question
                windows.append((start, anchor + 1, "complete" if complete else "incomplete_question"))
                covered.update(range(start, anchor + 1))
            for index, line in enumerate(lines):
                if index in covered or not TOPIC.search(line["text"]):
                    continue
                start, stop = max(0, index - 1), min(len(lines), index + 3)
                # Never create an answer-free subwindow of a known checkbox question.
                while start < stop and start in covered:
                    start += 1
                while stop > start and stop - 1 in covered:
                    stop -= 1
                if start < stop:
                    completeness = "incomplete_question" if any(
                        QUESTION_START.search(lines[i]["text"]) for i in range(start, stop)
                    ) else "not_a_checkbox"
                    windows.append((start, stop, completeness))
            for start, stop, completeness in windows:
                nearby_checked = [(i, match) for i in range(start, stop)
                                  for match in CHECKED_OPTION.finditer(lines[i]["text"])]
                ids = tuple(item["id"] for item in lines[start:stop])
                if ids in consumed:
                    continue
                consumed.add(ids)
                selected = {match.group(1) for _, match in nearby_checked}
                text = "\n".join(item["text"] for item in lines[start:stop])
                excerpt = {
                    "id": f"p{page_no:03d}-context-{start + 1:04d}-{stop:04d}",
                    "page_no": page_no, "line_ids": list(ids), "context_line_ids": list(ids),
                    "text": text, "bbox": [
                        min(item["bbox"][0] for item in lines[start:stop]),
                        min(item["bbox"][1] for item in lines[start:stop]),
                        max(item["bbox"][2] for item in lines[start:stop]),
                        max(item["bbox"][3] for item in lines[start:stop]),
                    ], "page_sha256": page_hash, "text_sha256": stable_hash(text),
                    "selected_answer": next(iter(selected)) if len(selected) == 1 else None,
                    "selection_status": "ambiguous" if len(nearby_checked) > 1 else
                    "selected" if selected else "not_a_checkbox",
                    "question_completeness": completeness,
                    "review_status": "unreviewed_context",
                }
                # Keep relevant negative questions ahead of unrelated compliance checklists.
                priority = 0 if nearby_checked and TOPIC.search(text) else 1 if TOPIC.search(text) else 2
                candidates.append((priority, page_no, start, excerpt))
    excerpts = []
    characters = 0
    omission_reasons = {"excerpt_character_limit": 0, "context_character_limit": 0, "excerpt_count_limit": 0}
    for _, _, _, excerpt in sorted(candidates, key=lambda item: item[:3]):
        # Count every rejected candidate; omission must be visible in the audit/report.
        if len(excerpt["text"]) > MAX_EXCERPT_CHARACTERS:
            omission_reasons["excerpt_character_limit"] += 1
            continue
        if len(excerpts) >= MAX_EXCERPTS:
            omission_reasons["excerpt_count_limit"] += 1
            continue
        if characters + len(excerpt["text"]) > MAX_CONTEXT_CHARACTERS:
            omission_reasons["context_character_limit"] += 1
            continue
        excerpts.append(excerpt)
        characters += len(excerpt["text"])
    excerpts.sort(key=lambda item: (item["page_no"], item["line_ids"][0]))
    return {
        "status": "available" if excerpts else "no_matching_native_context",
        "source_sha256": source_sha256, "extractor_version": CONTEXT_VERSION,
        "pymupdf_version": pymupdf.VersionBind,
        "total_pages": len(page_index),
        "native_text_pages": sum(p["native_characters"] > 0 for p in page_index),
        "scanned_page_numbers": [p["page_no"] for p in page_index],
        "selected_page_numbers": sorted({item["page_no"] for item in excerpts}),
        "model_input_line_count": sum(len(item["line_ids"]) for item in excerpts),
        "model_input_characters": characters, "candidate_excerpt_count": len(candidates),
        "omitted_excerpt_count": len(candidates) - len(excerpts),
        "omission_reasons": omission_reasons,
        "incomplete_question_count": sum(item["question_completeness"] == "incomplete_question"
                                         for item in excerpts),
        "selection_policy": "topic checkbox question/answer windows first; then topic windows; other checkboxes; "
                            "document order; explicit question boundary or incomplete_question; bounded characters and count",
        "selection_keywords": TOPIC.pattern,
        "selection_budget": {"max_characters": MAX_CONTEXT_CHARACTERS, "max_excerpts": MAX_EXCERPTS,
                             "max_excerpt_characters": MAX_EXCERPT_CHARACTERS,
                             "max_question_lines": MAX_QUESTION_LINES},
        "page_index_sha256": stable_hash(page_index), "excerpts": excerpts,
        "limitations": LIMITATIONS,
    }


def build_document_context(session: Session, filing: FilingVersion) -> dict:
    try:
        path = scanned_object_path(session, filing)
    except ValueError as exc:
        if str(exc) != "FILE_SCAN_REQUIRED":
            raise
        return {
            "status": "unavailable", "reason": "FILE_SCAN_REQUIRED",
            "source_sha256": filing.file_sha256, "extractor_version": CONTEXT_VERSION,
            "total_pages": None, "native_text_pages": None, "selected_page_numbers": [],
            "model_input_line_count": 0, "excerpts": [], "limitations": LIMITATIONS,
        }
    return extract_document_context(path, filing.file_sha256)

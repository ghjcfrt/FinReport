"""Opt-in native-PDF table observations, separate from financial admission.

Only ruled tables with a nearby non-recurring composition heading are supported.
Every cell refers to the untouched full page, never to reconstructed table text.
Geometry and arithmetic are observations, not an entailment or coverage proof.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
    localcontext,
)
from pathlib import Path

import pymupdf

VERSION = "composition_regions_v1"
ALGORITHM_VERSION = "composition_region_flat_arithmetic_v2"
_FAMILY = re.compile(r"非经常性损益项目(?:及|和)?金额")
_SECTION = re.compile(r"^(?:[一二三四五六七八九十百]+、|[（(][一二三四五六七八九十百]+[）)])")
_NUMBER = re.compile(r"[+＋−-]?(?:\d{1,3}(?:[,，]\d{3})+|\d+)(?:\.\d+)?|[（(](?:\d{1,3}(?:[,，]\d{3})+|\d+)(?:\.\d+)?[）)]")
_DASH = {"-", "--", "—", "–", "－"}
_SCALES = {"元": 1, "千元": 1000, "万元": 10000, "百万元": 1000000, "亿元": 100000000}
_BOUNDARY = (
    "原生PDF有线表格的候选观察；未调用模型，不接入v2—v5或正式事实准入。"
    "精确引文、几何匹配和条件算术不证明语义支持、实际会计口径、披露真实性或全文覆盖。"
    "表头仅保留明示期间身份，不推定完整日期或币种。续页无充分表头/续表证据时不跨页闭合。"
)


def _hash(value: bytes | str) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _box(value) -> list[float]:
    result = [float(item) for item in value]
    if len(result) != 4 or not all(math.isfinite(item) for item in result) or result[2] <= result[0] or result[3] <= result[1]:
        raise ValueError("COMPOSITION_INVALID_GEOMETRY")
    return result


def _inside(glyph: dict, box: list[float]) -> bool:
    x0, y0, x1, y1 = glyph["bbox"]
    return box[0] < (x0 + x1) / 2 < box[2] and box[1] < (y0 + y1) / 2 < box[3]


def _span(text: str, start: int, end: int) -> dict:
    quote = text[start:end]
    return {"start": start, "end": end, "quote": quote, "quote_sha256": _hash(quote)}


def _page_geometry(page, text: str) -> tuple[list[dict], list[dict]]:
    """Require exact correspondence before giving glyphs full-page offsets."""
    offset, parts, glyphs, lines = 0, [], [], []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            start = offset
            line_glyphs = []
            for native_span in line["spans"]:
                for char in native_span.get("chars", []):
                    value = char["c"]
                    glyph = {"start": offset, "end": offset + len(value), "bbox": _box(char["bbox"])}
                    glyphs.append(glyph)
                    line_glyphs.append(glyph)
                    parts.append(value)
                    offset += len(value)
            parts.append("\n")
            offset += 1
            if line_glyphs:
                lines.append({"span": _span(text, start, offset - 1), "bbox": _box(line["bbox"])})
    if "".join(parts) != text:
        raise ValueError("COMPOSITION_NATIVE_GLYPH_STREAM_MISMATCH")
    return glyphs, lines


def _cell(text: str, glyphs: list[dict], bbox, extracted: str | None, *, source_id: str) -> dict:
    if bbox is None:
        return {"source_id": source_id, "bbox": None, "spans": [], "binding_status": "merged_or_missing_geometry",
                "geometry_text": extracted, "derived_text": None}
    box = _box(bbox)
    selected = [glyph for glyph in glyphs if _inside(glyph, box)]
    ranges = []
    for glyph in selected:
        if ranges and (glyph["start"] == ranges[-1][1] or text[ranges[-1][1]:glyph["start"]].isspace()):
            ranges[-1][1] = glyph["end"]
        else:
            ranges.append([glyph["start"], glyph["end"]])
    spans = [_span(text, start, end) for start, end in ranges]
    derived = "".join(item["quote"] for item in spans)
    expected = extracted or ""
    status = "exact_geometry_spans" if _compact(derived) == _compact(expected) else "geometry_text_mismatch"
    if not selected and not expected.strip():
        status = "empty_cell"
    return {"source_id": source_id, "bbox": box, "spans": spans, "binding_status": status,
            "geometry_text": extracted, "derived_text": derived}


def _amount(cell: dict) -> dict:
    raw = _compact(cell.get("derived_text") or "")
    result = {"status": "unparsed", "value": None, "raw_token": raw, "sign": "unknown"}
    if cell["binding_status"] not in {"exact_geometry_spans", "empty_cell"}:
        result["status"] = "unbound"
    elif not raw:
        result["status"] = "blank_unknown"
    elif raw in _DASH:
        result["status"] = "dash_unknown"
    elif _NUMBER.fullmatch(raw):
        paired = raw.startswith("(") and raw.endswith(")") or raw.startswith("（") and raw.endswith("）")
        # Mixed ASCII/full-width parentheses are not silently repaired.
        if raw.startswith(("(", "（")) and not paired:
            return result
        normalized = raw.strip("()（）").replace(",", "").replace("，", "").replace("−", "-").replace("＋", "+")
        value = Decimal(normalized)
        if paired:
            value = value.copy_negate()
        if not value.is_finite():
            return result
        result.update(status="numeric", value=format(value, "f"), sign="parenthesized_negative" if paired else
                      "explicit_negative" if raw.startswith(("-", "−")) else
                      "explicit_positive" if raw.startswith(("+", "＋")) else "unsigned")
    return result


def _period(header: dict | None) -> dict:
    raw = _compact((header or {}).get("derived_text") or "")
    result = {"kind": "unknown", "year": None, "start": None, "end": None, "anchor": header}
    if not header or header["binding_status"] != "exact_geometry_spans":
        return result
    year = re.fullmatch(r"(20\d{2})年(?:度)?金额", raw)
    if year:
        result.update(kind="explicit_year", year=int(year[1]))
    elif raw == "本报告期金额":
        result["kind"] = "current_reporting_period"
    elif raw in {"年初至报告期末金额", "年初至报告期期末金额"}:
        result["kind"] = "year_to_date"
    return result


def _nearby_heading(lines: list[dict], table_bbox: list[float]) -> dict | None:
    preceding = [line for line in lines if line["bbox"][3] <= table_bbox[1] and table_bbox[1] - line["bbox"][3] <= 150]
    headings = [line for line in preceding if _FAMILY.search(_compact(line["span"]["quote"]))]
    if not headings:
        return None
    heading = headings[-1]
    if any(_SECTION.match(_compact(line["span"]["quote"])) and line["bbox"][1] > heading["bbox"][3]
           for line in preceding):
        return None
    return heading


def _unit(lines: list[dict], heading: dict | None, table_bbox: list[float], source_id: str) -> dict:
    if not heading:
        return {"status": "unknown", "literal": None, "scale": None, "currency": None, "anchor": None}
    candidates = []
    for line in lines:
        if heading["bbox"][3] <= line["bbox"][1] and line["bbox"][3] <= table_bbox[1]:
            match = re.fullmatch(r"单位[：:]\s*([^\s]+)", line["span"]["quote"].strip())
            if match:
                candidates.append((line, match[1]))
    if len(candidates) != 1:
        return {"status": "unknown" if not candidates else "ambiguous", "literal": None,
                "scale": None, "currency": None, "anchor": None}
    line, literal = candidates[0]
    return {"status": "anchored" if literal in _SCALES else "unsupported_literal", "literal": literal,
            "scale": _SCALES.get(literal), "currency": None, "anchor": {"source_id": source_id, **line}}


def _rows(cells: list[list[dict]], columns: list[dict], header_present: bool) -> list[dict]:
    rows, minus_anchor = [], None
    amount_columns = [column["index"] for column in columns if column["role"] == "amount"]
    for ordinal, row_cells in enumerate(cells[1:] if header_present else cells):
        label = row_cells[0]
        name = _compact(label.get("derived_text") or "")
        bound = label["binding_status"] == "exact_geometry_spans"
        role, operator, basis, assumption = "unknown", None, None, None
        if bound and name == "合计":
            role, basis = "total", "exact_total_label"
        elif bound and re.match(r"^减[：:]", name):
            role, operator, basis = "deduction", -1, "explicit_minus_prefix"
            minus_anchor = label
        elif bound and minus_anchor is not None and re.fullmatch(r"少数股东(?:权益|损益)影响额[（(]税后[）)]", name):
            role, operator, basis = "deduction", -1, "minority_after_explicit_minus_block"
            assumption = "少数股东税后影响额按紧邻减项区的扣减惯例计算；该操作符不是本行明示的减号。"
        elif bound and minus_anchor is None and name:
            role, operator, basis = "component", 1, "signed_component_before_minus_block"
        values = [{"column_index": index, "cell": row_cells[index], "amount": _amount(row_cells[index])}
                  for index in amount_columns]
        rows.append({"row_index": ordinal, "label": label, "role": role, "operator": operator,
                     "operator_basis": basis, "operator_anchor": minus_anchor if role == "deduction" else None,
                     "operator_assumption": assumption, "values": values,
                     "cells": row_cells})
    return rows


def _structure_issues(region: dict) -> list[str]:
    """Bound the flat-sum grammar; equality alone cannot establish this grammar."""
    rows, issues = region["rows"], []
    totals = [index for index, row in enumerate(rows) if row["role"] == "total"]
    if len(totals) != 1:
        issues.append("total_not_unique_or_missing")
    elif totals[0] != len(rows) - 1:
        issues.append("total_must_be_terminal_row")
    if not any(row["role"] == "component" for row in rows):
        issues.append("component_rows_missing")
    deduction_seen = False
    for row in rows:
        label = _compact(row["label"].get("derived_text") or "")
        # Subtotals and part-of rows need a hierarchy-aware calculation, not a
        # flat sum. Reject the structure rather than assigning guessed signs.
        structural_label = re.sub(r"^(?:[（(]?[一二三四五六七八九十百\d]+[）)、.．]|[①-⑳])", "", label)
        if row["role"] != "total" and (re.match(r"^(?:其中|包括|包含|内含|分项)[：:]?", structural_label)
                                         or re.match(r"^\d+[.．]\d+", label)
                                         or re.search(r"小计|合计|总计|总额", structural_label)):
            issues.append("nested_or_aggregate_rows_require_explicit_structure")
        if row["role"] == "deduction":
            deduction_seen = True
        elif row["role"] == "component" and deduction_seen:
            issues.append("component_after_deduction_block")
        if row["role"] == "unknown":
            issues.append("unresolved_row_operator")
    if any(column["role"] == "unknown" for column in region["columns"]):
        issues.append("unknown_column_role")
    identities = [(column["period_identity"]["kind"], column["period_identity"]["year"])
                  for column in region["columns"] if column["role"] == "amount"
                  and column["period_identity"]["kind"] != "unknown"]
    if len(identities) != len(set(identities)):
        issues.append("duplicate_column_period_identity")
    return sorted(set(issues))


def _reconcile(region: dict) -> list[dict]:
    checks = []
    for column in [column for column in region["columns"] if column["role"] == "amount"]:
        reasons, selected = list(region["structure_issues"]), []
        for row in region["rows"]:
            value = next(item for item in row["values"] if item["column_index"] == column["index"])
            selected.append((row, value))
            if value["amount"]["status"] != "numeric":
                reasons.append("unknown_or_unbound_amount")
            elif not Decimal(value["amount"]["value"]).is_finite():
                reasons.append("nonfinite_amount")
            if row["role"] == "unknown":
                reasons.append("unresolved_row_operator")
        totals = [(row, value) for row, value in selected if row["role"] == "total"]
        components = [(row, value) for row, value in selected if row["role"] != "total"]
        if region["continuation_status"] != "standalone_with_header":
            reasons.append("unverified_continuation_chain")
        computed = disclosed = equal = None
        assumptions = sorted({row["operator_assumption"] for row, _ in selected if row["operator_assumption"]})
        if not reasons:
            tokens = [value["amount"]["value"] for _, value in selected]
            precision = max(28, sum(len(token) for token in tokens) + 8)
            context = Context(prec=precision, rounding=ROUND_HALF_EVEN, Emin=-999999999, Emax=999999999,
                              capitals=1, clamp=0, flags=[],
                              traps=[InvalidOperation, DivisionByZero, Overflow, Inexact, Rounded])
            try:
                with localcontext(context):
                    total = sum((Decimal(value["amount"]["value"]) * row["operator"]
                                 for row, value in components), Decimal(0))
                    if not total.is_finite():
                        reasons.append("nonfinite_arithmetic_result")
                    else:
                        computed = format(total, "f")
                if not reasons:
                    disclosed = totals[0][1]["amount"]["value"]
                    equal = Decimal(computed) == Decimal(disclosed)
            except DecimalException:
                reasons.append("exact_arithmetic_context_failure")
        checks.append({"column_index": column["index"], "status": "unresolved" if reasons else
                       "arithmetic_consistent_under_declared_rule" if equal else "arithmetic_mismatch_under_declared_rule",
                       "computed_total_under_rule": computed, "disclosed_total": disclosed,
                       "equal_under_rule": equal, "operator_assumptions": assumptions,
                       "calculation_assumptions": ["选取行假定为可按平坦明细相加的互不重叠项目；该假设未获语义认证。", *assumptions],
                       "issues": sorted(set(reasons)), "period_identity_verified": column["period_identity"]["kind"] != "unknown"
                       and "duplicate_column_period_identity" not in region["structure_issues"],
                       "unit_anchored": region["unit"]["status"] == "anchored",
                       "arithmetic_context_policy": "complete_fixed_context_exact_finite_v2",
                       "structure_applicable": not region["structure_issues"],
                       "structure_applicability_scope": "mechanical_conditions_only_not_semantic_flatness",
                       "flatness_verified": False, "semantic_support_verified": False, "formal_admitted": False})
    return checks


def _continuation(previous: dict, bbox: list[float], column_boxes: list, page_no: int, page_height: float) -> dict | None:
    if previous["page_no"] + 1 != page_no or any(row["role"] == "total" for row in previous["rows"]):
        return None
    old_boxes = previous["physical_column_boxes"]
    if len(old_boxes) != len(column_boxes) or any(box is None for box in [*old_boxes, *column_boxes]):
        return None
    deltas = [abs(old[axis] - new[axis]) for old, new in zip(old_boxes, column_boxes, strict=True) for axis in (0, 2)]
    if max(deltas, default=999) > 1 or bbox[1] > page_height * .25:
        return None
    return {"previous_region_id": previous["id"], "adjacent_physical_pages": True,
            "column_edge_max_delta_points": max(deltas), "repeated_header": False,
            "explicit_continuation_anchor": None, "verified": False,
            "boundary": "相邻页列几何匹配仅建立候选关联；缺少重复表头或明确续表锚点，不继承期间/单位且不跨页闭合。"}


def extract_composition_regions(pdf_path: str | Path, page_numbers: list[int]) -> dict:
    """Observe selected native pages only; never mutate PDF or call providers."""
    if not page_numbers or any(type(item) is not int or item < 1 for item in page_numbers):
        raise ValueError("COMPOSITION_INVALID_PAGE_SELECTION")
    if len(set(page_numbers)) != len(page_numbers):
        raise ValueError("COMPOSITION_DUPLICATE_PAGE_SELECTION")
    raw = Path(pdf_path).read_bytes()
    document_hash = _hash(raw)
    sources, regions, unknowns = [], [], []
    with pymupdf.open(stream=raw, filetype="pdf") as document:
        if max(page_numbers) > len(document):
            raise ValueError("COMPOSITION_PAGE_OUT_OF_RANGE")
        for page_no in sorted(page_numbers):
            page = document[page_no - 1]
            text = page.get_text()
            source_id = f"native-page:{document_hash}:{page_no}"
            source = {"id": source_id, "document_sha256": document_hash, "page_no": page_no,
                      "text": text, "text_sha256": _hash(text), "page_bbox": _box(page.rect)}
            sources.append(source)
            try:
                glyphs, lines = _page_geometry(page, text)
            except ValueError as exc:
                unknowns.append({"source_id": source_id, "code": str(exc)})
                continue
            for table in page.find_tables(strategy="lines_strict").tables:
                bbox, extracted = _box(table.bbox), table.extract()
                if not extracted or not table.rows:
                    continue
                header_present = _compact(extracted[0][0] or "") == "项目"
                heading = _nearby_heading(lines, bbox) if header_present else None
                chain = None
                if not heading:
                    if not header_present and regions:
                        chain = _continuation(regions[-1], bbox, table.rows[0].cells, page_no, page.rect.height)
                    if chain is None:
                        continue
                cells = [[_cell(text, glyphs, cell_bbox, token, source_id=source_id)
                          for cell_bbox, token in zip(row.cells, tokens, strict=True)]
                         for row, tokens in zip(table.rows, extracted, strict=True)]
                if header_present:
                    columns = []
                    for index, cell in enumerate(cells[0]):
                        header = _compact(cell.get("derived_text") or "")
                        role = "label" if index == 0 else "note" if header == "说明" else "amount" if "金额" in header else "unknown"
                        columns.append({"index": index, "role": role, "header": cell, "period_identity": _period(cell) if role == "amount" else None})
                else:
                    columns = [{"index": old["index"], "role": old["role"], "header": None,
                                "period_identity": _period(None) if old["role"] == "amount" else None}
                               for old in regions[-1]["columns"]]
                if not any(column["role"] == "amount" for column in columns):
                    continue
                region = {"id": "composition-region:" + _hash(_canonical({"source": source_id, "bbox": bbox}))[:24],
                          "source_id": source_id, "document_sha256": document_hash, "page_no": page_no,
                          "table_bbox": bbox, "physical_column_boxes": [list(item) if item else None for item in table.rows[0].cells],
                          "heading_anchor": {"source_id": source_id, **heading} if heading else None,
                          "unit": _unit(lines, heading, bbox, source_id), "columns": columns,
                          "continuation_status": "unverified_continuation" if chain else "standalone_with_header",
                          "continuation_candidate": chain, "rows": _rows(cells, columns, header_present),
                          "formal_admitted": False, "semantic_support_verified": False, "full_document_coverage": False}
                issues = []
                if region["unit"]["status"] != "anchored":
                    issues.append("unit_not_anchored")
                if any(column["role"] == "unknown" for column in columns):
                    issues.append("unknown_column_role")
                if any(column["period_identity"]["kind"] == "unknown" for column in columns if column["role"] == "amount"):
                    issues.append("column_period_identity_unknown")
                if any(cell["binding_status"] not in {"exact_geometry_spans", "empty_cell"} for row in cells for cell in row):
                    issues.append("cell_source_binding_incomplete")
                region["structure_issues"] = _structure_issues(region)
                region["flatness_verified"] = False
                region["structure_applicability_scope"] = "mechanical_conditions_only_not_semantic_flatness"
                region["issues"] = sorted(set(issues + region["structure_issues"]))
                region["arithmetic_checks"] = _reconcile(region)
                region["table_closure_certified"] = False
                regions.append(region)
    result = {"schema_version": VERSION, "algorithm_version": ALGORITHM_VERSION,
              "document_sha256": document_hash, "selected_page_numbers": sorted(page_numbers),
              "extractor": {"library": "PyMuPDF", "version": pymupdf.VersionBind, "strategy": "lines_strict",
                            "offset_mapping": "exact_full_native_glyph_stream_before_bbox_selection"},
              "sources": sources, "regions": regions, "unknowns": unknowns, "boundary": _BOUNDARY,
              "model_called": False, "network_used": False, "formal_admitted": False,
              "semantic_support_verified": False, "full_document_coverage": False}
    result["artifact_sha256"] = _hash(_canonical(result))
    return result


def verify_composition_regions(artifact: dict, pdf_path: str | Path) -> dict:
    """Rebuild from the original PDF; a self-recomputed artifact hash is insufficient."""
    if not isinstance(artifact, dict) or artifact.get("schema_version") != VERSION:
        raise ValueError("COMPOSITION_ARTIFACT_VERSION_INVALID")
    if artifact.get("algorithm_version") != ALGORITHM_VERSION:
        raise ValueError("COMPOSITION_ARTIFACT_ALGORITHM_VERSION_INVALID")
    expected = extract_composition_regions(pdf_path, artifact.get("selected_page_numbers", []))
    if _canonical(artifact) != _canonical(expected):
        raise ValueError("COMPOSITION_ORIGINAL_PDF_RECONSTRUCTION_MISMATCH")
    return {"status": "original_pdf_reconstruction_matches", "algorithm_version": ALGORITHM_VERSION,
            "artifact_sha256": expected["artifact_sha256"],
            "document_sha256": expected["document_sha256"], "formal_admitted": False,
            "semantic_support_verified": False, "model_called": False, "network_used": False}

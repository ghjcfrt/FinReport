"""Literal, provenance-bound quotation units for cashflow reconciliation notes."""
from __future__ import annotations

import hashlib
import re

from finance_adapters.persistence.audit import stable_hash


def reconciliation_units(source: dict) -> list[dict]:
    """Recognize a closed two-period indirect-method table, never its causes.

    The supported Docling layout repeats two date headers, then row label /
    current / comparative cells. Unknown layouts fail closed. A following
    noncash subtable is an explicit boundary, not part of the reconciliation.
    No PDF or external file is read when certifying or replaying the unit.
    """
    text = source["text"]
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    box = source.get("bounding_box")
    if (source.get("extraction_method") != "docling" or not isinstance(box, dict)
            or digest != source.get("text_sha256")
            or box.get("node_text_sha256") != digest or box.get("raw_text_sha256") != digest
            or box.get("text_span") != [0, len(text)]
            or not re.fullmatch(r"#/tables/\d+", str(box.get("docling_ref", "")))
            or not re.fullmatch(r"[0-9a-f]{64}", str(box.get("docling_json_sha256", "")))
            or source.get("page_no") not in box.get("original_page_numbers", [])
            or not any(isinstance(p, dict) and p.get("page_no") == source.get("page_no")
                       and isinstance(p.get("bbox"), dict) for p in box.get("provenance", []))
            or not any("现金流量表补充资料" in str(h) for h in box.get("heading_path", []))):
        return []
    lines, offset = [], 0
    for raw in text.splitlines(keepends=True):
        if raw.strip():
            lines.append((offset, raw.rstrip("\r\n")))
        offset += len(raw)
    if len(lines) < 18:
        return []
    date_header = r"截至\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
    dates = [re.fullmatch(date_header, lines[i][1].strip()) for i in (0, 2)]
    if (not all(dates) or dates[0].group(1) == dates[1].group(1)
            or dates[0].groups()[1:] != dates[1].groups()[1:]
            or lines[1][1].strip() != lines[3][1].strip()
            or not re.fullmatch(r"止\s*\d{1,2}\s*个月期间", lines[1][1].strip())
            or any(not re.fullmatch(r"[（(](?:未经审计|经审计)[）)]", lines[i][1].strip()) for i in (4, 5))
            or lines[6][1].strip() != "净利润"):
        return []
    number = re.compile(r"(?:[（(][-−]?\d[\d,，]*(?:\.\d+)?[）)]|[-−]?\d[\d,，]*(?:\.\d+)?|[-—])")
    labels, pos = [], 6
    while pos + 2 < len(lines):
        label = lines[pos][1].strip()
        if (not re.search(r"[\u4e00-\u9fff]", label)
                or not all(number.fullmatch(lines[pos + c][1].strip()) for c in (1, 2))):
            return []
        labels.append(label)
        pos += 3
        if label == "经营活动产生的现金流量净额":
            break
    if (len(labels) < 4 or labels[-1] != "经营活动产生的现金流量净额"
            or (pos < len(lines) and lines[pos][1].strip() != "不涉及现金的重大投资和筹资活动：")):
        return []
    end = lines[pos - 1][0] + len(lines[pos - 1][1])
    provenance = {k: box[k] for k in (
        "docling_ref", "docling_json_sha256", "node_text_sha256", "raw_text_sha256",
        "original_page_numbers", "heading_path", "text_span", "provenance",
    )}
    return [{"start": lines[0][0], "end": end, "kind": "complete_cashflow_reconciliation",
             "policy": "docling_cashflow_reconciliation_v1", "title": "净利润与经营现金流调节",
             "column_labels": [" ".join(lines[i + j][1] for j in (0, 1)) for i in (0, 2)],
             "row_labels": labels, "terminal_row": labels[-1],
             "original_table_provenance": provenance, "provenance_hash": stable_hash(provenance),
             "text_sha256": hashlib.sha256(text[lines[0][0]:end].encode("utf-8")).hexdigest()}]

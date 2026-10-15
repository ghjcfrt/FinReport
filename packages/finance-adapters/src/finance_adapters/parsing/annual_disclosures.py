"""Measured annual summary/R&D disclosures; no audit assurance is inferred."""
from __future__ import annotations

import hashlib
import re
from datetime import date
from itertools import pairwise

from finance_domain.filings.disclosures import (
    DISCLOSURE_DEFINITIONS,
    check_disclosure_currency,
    normalize_disclosure_value,
)

from finance_adapters.parsing.statement_extractor import (
    compact,
    declared_currency,
    page_rows,
    union_bbox,
)

DISCLOSURE_VERSION = "annual-disclosure-text-v1"


def disclosure_label(text: str) -> str:
    return re.sub(r"\((?:人民币)?(?:元/股|元|千元|万元|亿元)\)$", "", compact(text))


def annual_disclosures_from_page(page, page_no: int, page_sha256: str, target_year: int,
                                 currency: str | None, *, native_text: str | None = None,
                                 currency_source: dict | None = None) -> list[dict]:
    if currency != "CNY":
        return []
    rows = page_rows(page)
    text = page.get_text("text") if native_text is None else native_text
    try:
        check_disclosure_currency(text)
    except ValueError:
        return []
    results = []
    for section in {definition["section"] for definition in DISCLOSURE_DEFINITIONS.values()}:
        starts = [index for index, row in enumerate(rows)
                  if re.fullmatch(r"(?:[一二三四五六七八九十\d]+[、.．])?" + re.escape(section),
                                  compact("".join(cell.text for cell in row)))]
        if len(starts) != 1:
            continue
        start = starts[0]
        header = None
        for index in range(start + 1, min(start + 12, len(rows))):
            years = [cell for cell in rows[index] if re.fullmatch(r"20\d{2}年度?", compact(cell.text))]
            if len(years) >= 2 and {target_year, target_year - 1}.issubset({int(compact(c.text)[:4]) for c in years}):
                header = index, sorted(years, key=lambda cell: cell.bbox[0])
                break
        if header is None:
            continue
        header_index, years = header
        centers = [(cell.bbox[0] + cell.bbox[2]) / 2 for cell in years]
        boundaries = [centers[0] - (centers[1] - centers[0]) / 2,
                      *((a + b) / 2 for a, b in pairwise(centers)),
                      centers[-1] + (centers[-1] - centers[-2]) / 2]
        pending = []
        body = rows[header_index + 1:]
        for body_index, row in enumerate(body):
            joined = compact("".join(cell.text for cell in row))
            if (re.search(r"20\d{2}年末|20\d{2}年12月31日|季度|半年|期中|累计|YTD|quarter|sixmonths|[1-9]月|1[01]月", joined, re.IGNORECASE)
                    or re.match(r"[一二三四五六七八九十\d]+[、.．]", joined)
                    or joined.startswith(("公司研发人员", "研发投入总额占", "研发投入资本化率大幅"))):
                break
            labels = [cell for cell in row if cell.bbox[0] < boundaries[0]]
            data = [cell for cell in row if cell.bbox[0] >= boundaries[0]]
            if not data:
                pending = labels if labels else []
                continue
            trailing = []
            if body_index + 1 < len(body):
                next_row = body[body_index + 1]
                if (0 < next_row[0].bbox[1] - row[0].bbox[1] <= 8
                        and all(cell.bbox[0] < boundaries[0] for cell in next_row)):
                    trailing = next_row
            candidates = [labels, pending + labels, pending + labels + trailing]
            matches = []
            for label_cells in candidates:
                label = disclosure_label("".join(cell.text for cell in label_cells))
                for code, definition in DISCLOSURE_DEFINITIONS.items():
                    if definition["section"] == section and label in definition["labels"]:
                        matches.append((code, definition, label_cells))
            pending = []
            if len({code for code, _, _ in matches}) != 1:
                continue
            code, definition, label_cells = matches[-1]
            raw_label = "".join(cell.text for cell in label_cells).strip()
            unit = definition["unit"]
            # Per-share/currency declarations are row-local, never inherited
            # from an unrelated table later on the same page.
            if unit == "currency_per_share" and "元/股" not in compact(raw_label):
                continue
            if unit == "currency" and not re.search(r"\((?:人民币)?元\)$", compact(raw_label)):
                continue
            for column, year_cell in enumerate(years):
                year = int(compact(year_cell.text)[:4])
                if year not in {target_year, target_year - 1}:
                    continue
                values = [cell for cell in data if boundaries[column] <= (cell.bbox[0] + cell.bbox[2]) / 2 < boundaries[column + 1]]
                if len(values) != 1:
                    continue
                raw_value = values[0].text.strip()
                try:
                    value = normalize_disclosure_value(raw_value, unit)
                except ValueError:
                    continue
                results.append({
                    "code": code, "name": definition["name"], "value": str(value), "unit": unit,
                    "currency": "CNY" if unit != "ratio" else None, "unit_scale": 1,
                    "scope": definition["scope"], "raw_value": raw_value, "raw_label": raw_label,
                    "page_no": page_no, "page_sha256": page_sha256,
                    "column_label": year_cell.text.strip(), "period_start": date(year, 1, 1).isoformat(),
                    "period_end": date(year, 12, 31).isoformat(), "period_role": "current" if year == target_year else "comparative",
                    "basis": "as_disclosed", "assurance": "annual_report_disclosed_not_audit_opinion",
                    "currency_source": currency_source,
                    "source_kind": "pdf_text", "section": section,
                    "source_excerpt": text[:12000],
                    "evidence": {"source": "text-layer", "row": list(union_bbox(label_cells + values)),
                                 "value": list(values[0].bbox), "unit_text": raw_label if unit != "ratio" else raw_value},
                })
    return results


def document_annual_disclosures(document, texts: list[str], target_year: int) -> list[dict]:
    """Require a traceable report currency declaration, then row-local units."""
    if declared_currency("\n".join(texts)) != "CNY":
        return []
    source = next(({"page_no": index + 1, "page_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "quote": line.strip(), "currency": "CNY"}
                   for index, text in enumerate(texts) for line in text.splitlines()
                   if declared_currency(line) == "CNY"), None)
    if source is None:
        return []
    return [item for page_no, (page, text) in enumerate(zip(document, texts, strict=True), 1)
            for item in annual_disclosures_from_page(
                page, page_no, hashlib.sha256(text.encode("utf-8")).hexdigest(), target_year,
                "CNY", native_text=text, currency_source=source)]

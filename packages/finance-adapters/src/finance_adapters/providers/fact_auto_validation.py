"""Reconstruct narrowly scoped native financial facts from the original PDF.

The receipt authenticates an input-to-native-disclosure binding under an explicit
calendar-year primary-statement policy. It does not authenticate the issuer,
audit opinion, financial truth, actual consolidation population, or model prose.
The persistence gateway owns source assurance and financial equation admission.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import pairwise
from typing import Any, Literal

import pymupdf
from finance_domain.filings.concepts import CONCEPT_LABELS, CONCEPT_METADATA, STATEMENT_ALIASES
from pydantic import BaseModel, ConfigDict, Field, model_validator

POLICY_VERSION = "native_primary_fact_auto_v2"
ALGORITHM_VERSION = "independent_native_primary_geometry_v2"
_SCALES = {"元": 1, "千元": 1000, "万元": 10000, "百万元": 1000000, "亿元": 100000000}
_NUMBER = re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\)")
_TITLE = re.compile(r"(?:[一二三四五六七八九十\d]+[、.．])?(合并(?:资产负债表|利润表|现金流量表))")
_ANY_TITLE = re.compile(
    r"(?:[一二三四五六七八九十\d]+[、.．])?(?:合并|母公司|公司)?"
    r"(?:资产负债表|利润表|现金流量表|所有者权益变动表)"
)
_HEADER = re.compile(r"(20\d{2})年(?:(12)月(31)日|度)?")
_UNITS = re.compile(r"(?:金额)?单位(?:为)?[:：]?(?:人民币)?(百万元|千元|万元|亿元|元)")
_CURRENCY = re.compile(r"(?:币种[:：]?(?:人民币|CNY|RMB)|人民币(?:百万元|千元|万元|亿元|元))")
_REPORT_DEFAULT_UNIT = re.compile(r"均以人民币(百万元|千元|万元|亿元|元)为单位表示")
_REPORT_DEFAULT_SCOPE = "本公司记账本位币和编制本财务报表所采用的货币均为人民币"
_BOUNDARIES = (
    "native_primary_statements_calendar_annual_consolidated_only",
    "same_page_or_unique_report_default_unit_and_currency_with_contiguous_annual_statement_pages",
    "native_geometry_is_not_independent_visual_or_semantic_gold",
    "source_issuer_publication_authenticity_and_audit_assurance_not_certified",
    "declared_scope_is_not_actual_consolidation_population_or_comparability",
    "source_bound_amounts_are_not_financial_truth_or_free_text_semantics",
    "financial_equations_and_strict_read_time_revalidation_are_external_gates",
)
# Explicit source labels only; this is not a generic prefix-stripping rule.
_SOURCE_LABELS = {**CONCEPT_LABELS,
                  "减：营业成本": ("operating_cost", "利润表", "flow"),
                  "其中：利息费用": ("interest_expense", "利润表", "flow"),
                  "加：期初现金及现金等价物余额": ("cash_begin", "现金流量表", "stock"),
                  "加：年初现金及现金等价物余额": ("cash_begin", "现金流量表", "stock")}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class FilingIdentity(_Strict):
    id: int = Field(gt=0)
    company_id: int = Field(gt=0)
    file_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    period_start: date
    period_end: date
    report_type: str
    period_basis: str
    scope: str
    accounting_standard: str


class DimensionSnapshot(_Strict):
    dimension_name: str
    dimension_value: str


class EvidenceSnapshot(_Strict):
    id: int = Field(gt=0)
    page_no: int = Field(gt=0)
    table_name: str
    row_locator: str
    column_locator: str
    raw_label: str
    raw_value: str
    bounding_box: dict[str, Any]
    page_sha256: str
    confidence: str
    extractor: str


class FactSnapshot(_Strict):
    id: int = Field(gt=0)
    filing_version_id: int = Field(gt=0)
    concept_code: str
    concept_name: str
    statement_type: str
    period_start: date
    period_end: date
    value: str
    currency: str
    unit_scale: int
    flow_or_stock: str
    assurance_level: str
    revision_no: int = Field(gt=0)
    raw_value: str
    comparative_period: bool
    is_restatement: bool
    evidence: tuple[EvidenceSnapshot, ...]
    dimensions: tuple[DimensionSnapshot, ...]


class FactAutoValidationInput(_Strict):
    policy_version: Literal["native_primary_fact_auto_v2"] = POLICY_VERSION
    extraction_job_id: int = Field(gt=0)
    extraction_method: str
    parser_version: str
    page_numbers: tuple[int, ...] | None = None
    filing: FilingIdentity
    facts: tuple[FactSnapshot, ...]

    @model_validator(mode="after")
    def unique_ids(self):
        if len({item.id for item in self.facts}) != len(self.facts):
            raise ValueError("AUTO_FACT_DUPLICATE_FACT_ID")
        evidence = [item.id for fact in self.facts for item in fact.evidence]
        if len(set(evidence)) != len(evidence):
            raise ValueError("AUTO_FACT_DUPLICATE_EVIDENCE_ID")
        return self


class SourceSpan(_Strict):
    page_no: int
    start: int
    end: int
    quote: str
    bbox: tuple[float, float, float, float]


class NativeFactProof(_Strict):
    page_no: int
    page_text_sha256: str
    table_title: tuple[SourceSpan, ...]
    period_header: tuple[SourceSpan, ...]
    unit_anchor: tuple[SourceSpan, ...]
    currency_anchor: tuple[SourceSpan, ...]
    label: tuple[SourceSpan, ...]
    amount: tuple[SourceSpan, ...]
    row_bbox: tuple[float, float, float, float]
    value_bbox: tuple[float, float, float, float]
    concept_code: str
    statement_type: str
    declared_scope: Literal["consolidated"] = "consolidated"
    period_start: date
    period_end: date
    source_unit_scale: int
    currency: Literal["CNY"] = "CNY"
    normalized_value: str
    period_binding_basis: Literal["explicit_primary_header_calendar_year"] = (
        "explicit_primary_header_calendar_year"
    )
    unit_binding_basis: Literal["same_page_explicit", "report_level_default"] = "same_page_explicit"
    currency_binding_basis: Literal["same_page_explicit", "report_level_default"] = "same_page_explicit"
    report_header: tuple[SourceSpan, ...] = ()


class FactAutoValidationDecision(_Strict):
    fact_id: int
    status: Literal["candidate", "needs_review", "rejected", "auto_passed"]
    issue_codes: tuple[str, ...]
    source_proof: NativeFactProof | None = None


class FactAutoValidationReceipt(_Strict):
    policy_version: Literal["native_primary_fact_auto_v2"] = POLICY_VERSION
    algorithm_version: Literal["independent_native_primary_geometry_v2"] = ALGORITHM_VERSION
    extraction_job_id: int
    input_snapshot_sha256: str
    actual_pdf_sha256: str
    source_bytes_match: bool
    decisions: tuple[FactAutoValidationDecision, ...]
    issuer_identity_verified: Literal[False] = False
    audit_assurance_verified: Literal[False] = False
    financial_truth_verified: Literal[False] = False
    free_text_semantics_verified: Literal[False] = False
    actual_consolidation_population_verified: Literal[False] = False
    extractor_confidence_verified: Literal[False] = False
    boundaries: tuple[str, ...] = _BOUNDARIES


@dataclass(frozen=True)
class _Glyph:
    start: int
    end: int
    text: str
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True)
class _Line:
    glyphs: tuple[_Glyph, ...]
    bbox: tuple[float, float, float, float]

    @property
    def text(self):
        return "".join(item.text for item in self.glyphs)


@dataclass
class _TableContext:
    table_name: str
    statement: str
    title_page: int
    title_spans: tuple[SourceSpan, ...]
    last_page: int
    page_numbers: list[int]


def _hash(value: bytes | str) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def _snapshot_hash(snapshot: FactAutoValidationInput) -> str:
    return _hash(json.dumps(snapshot.model_dump(mode="json"), sort_keys=True,
                            ensure_ascii=False, separators=(",", ":"), allow_nan=False))


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value).replace("（", "(").replace("）", ")")


def _label(value: str) -> str:
    # Deliberately do not remove 其中/加/减/非 or guess parent-child semantics.
    return re.sub(r"^[一二三四五六七八九十百\d]+[、.．]", "", _compact(value)).rstrip(":：")


def _number(value: str) -> Decimal | None:
    token = _compact(value).replace("，", ",").replace("−", "-")
    if len(token) > 256 or _NUMBER.fullmatch(token) is None:
        return None
    negative = token.startswith("(")
    result = Decimal((token[1:-1] if negative else token).replace(",", ""))
    return result.copy_negate() if negative else result


def _scaled(value: Decimal, scale: int) -> Decimal:
    sign, digits, exponent = value.as_tuple()
    return Decimal((sign, digits, exponent + len(str(scale)) - 1))


def _bbox(glyphs) -> tuple[float, float, float, float]:
    return (min(item.bbox[0] for item in glyphs), min(item.bbox[1] for item in glyphs),
            max(item.bbox[2] for item in glyphs), max(item.bbox[3] for item in glyphs))


def _box(value) -> tuple[float, float, float, float] | None:
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(isinstance(item, bool) or not isinstance(item, (float, int))
                   or not math.isfinite(item) for item in value)):
        return None
    box = tuple(float(item) for item in value)
    return box if box[0] < box[2] and box[1] < box[3] else None


def _same_box(left, right) -> bool:
    return left is not None and right is not None and all(
        abs(a - b) <= 0.025 for a, b in zip(left, right, strict=True)
    )


def _native_page(page) -> tuple[str, tuple[_Line, ...]]:
    text = page.get_text("text")
    parts, glyphs, offset = [], [], 0
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                for char in span.get("chars", []):
                    token = char["c"]
                    box = _box(char["bbox"])
                    if box is None:
                        raise ValueError("AUTO_FACT_NATIVE_GEOMETRY_INVALID")
                    if not token.isspace():
                        glyphs.append(_Glyph(offset, offset + len(token), token, box))
                    parts.append(token)
                    offset += len(token)
            parts.append("\n")
            offset += 1
    if "".join(parts) != text:
        raise ValueError("AUTO_FACT_NATIVE_GLYPH_STREAM_MISMATCH")
    groups: list[list[_Glyph]] = []
    for item in sorted(glyphs, key=lambda g: ((g.bbox[1] + g.bbox[3]) / 2, g.bbox[0])):
        center = (item.bbox[1] + item.bbox[3]) / 2
        if groups and abs(center - (groups[-1][0].bbox[1] + groups[-1][0].bbox[3]) / 2) <= 2:
            groups[-1].append(item)
        else:
            groups.append([item])
    return text, tuple(_Line(tuple(sorted(group, key=lambda g: g.bbox[0])), _bbox(group))
                       for group in groups)


def _spans(page_no: int, text: str, glyphs) -> tuple[SourceSpan, ...]:
    # Preserve actual full-page offsets even when layout joins separate text blocks.
    groups: list[list[_Glyph]] = []
    for glyph in glyphs:
        if groups and (text[groups[-1][-1].end:glyph.start].isspace()
                       or groups[-1][-1].end == glyph.start):
            groups[-1].append(glyph)
        else:
            groups.append([glyph])
    return tuple(SourceSpan(page_no=page_no, start=items[0].start, end=items[-1].end,
                            quote=text[items[0].start:items[-1].end], bbox=_bbox(items))
                 for items in groups)


def _headers(line: _Line):
    # Whitespace is absent from native glyph groups, but inter-column gaps remain geometry.
    groups: list[list[_Glyph]] = []
    for glyph in line.glyphs:
        if groups and glyph.bbox[0] - groups[-1][-1].bbox[2] < 10:
            groups[-1].append(glyph)
        else:
            groups.append([glyph])
    return [(items, _HEADER.fullmatch("".join(g.text for g in items))) for items in groups]


def _observations(document) -> tuple[list[NativeFactProof], dict[int, str], dict[int, str]]:
    observations, page_hashes, page_texts, page_lines = [], {}, {}, {}
    for page_index, page in enumerate(document):
        page_no = page_index + 1
        text, lines = _native_page(page)
        page_hashes[page_no] = _hash(text)
        page_texts[page_no] = text
        page_lines[page_no] = lines

    report_defaults, report_headers = [], {}
    for page_no, lines in page_lines.items():
        for line in lines:
            compact_line = _compact(line.text)
            if "年度报告" in compact_line and re.search(r"20\d{2}", compact_line):
                report_headers[page_no] = _spans(page_no, page_texts[page_no], line.glyphs)
                break
        for line in lines:
            compact_line = _compact(line.text)
            scale_match = _REPORT_DEFAULT_UNIT.search(compact_line)
            if (_REPORT_DEFAULT_SCOPE in compact_line and "除有特别说明外" in compact_line
                    and scale_match):
                report_defaults.append((page_no, line, _SCALES[scale_match.group(1)]))
    report_default = report_defaults[0] if len(report_defaults) == 1 else None

    context: _TableContext | None = None
    for page_no, lines in page_lines.items():
        text = page_texts[page_no]
        all_titles = [(index, _ANY_TITLE.fullmatch(_compact(line.text)))
                      for index, line in enumerate(lines)]
        all_titles = [(index, match.group(0)) for index, match in all_titles if match]
        target_titles = [(index, _TITLE.fullmatch(_compact(line.text)))
                         for index, line in enumerate(lines)]
        target_titles = [(index, match.group(1)) for index, match in target_titles if match]
        if len(all_titles) > 1 or len(target_titles) > 1:
            context = None
            continue
        if all_titles:
            if not target_titles:
                context = None
                continue
            title_index, table_name = target_titles[0]
            statement = table_name.removeprefix("合并")
            context = _TableContext(
                table_name=table_name,
                statement=statement,
                title_page=page_no,
                title_spans=_spans(page_no, text, lines[title_index].glyphs),
                last_page=page_no - 1,
                page_numbers=[],
            )
        elif context is None or context.last_page != page_no - 1:
            context = None
            continue

        boundary_pattern = (r"法定代表人|主管会计工作负责人|会计机构负责人|后附财务报表附注|"
                            r"财务报表附注|合并财务报表项目注释|重要会计政策及会计估计|"
                            r"财务报表的编制基础")
        boundary = [index for index, line in enumerate(lines)
                    if re.search(boundary_pattern, line.text)]
        end = min(boundary, default=len(lines))
        start = next((index + 1 for index, _ in target_titles), 0)
        section = lines[start:end]
        header_candidates = []
        for index, line in enumerate(section):
            headers = [(glyphs, match) for glyphs, match in _headers(line) if match]
            if len(headers) >= 2:
                header_candidates.append((index, headers))
        if len(header_candidates) != 1:
            context = None
            continue
        header_index, headers = header_candidates[0]
        years = [int(match.group(1)) for _, match in headers]
        if len(set(years)) != len(years):
            context = None
            continue

        anchors = section[:header_index]
        local_units = [(line, _SCALES[match.group(1)]) for line in anchors
                       for match in _UNITS.finditer(_compact(line.text))]
        local_currencies = [line for line in anchors if _CURRENCY.search(_compact(line.text))]
        if (len(local_units) > 1 or len(local_currencies) > 1
                or any(re.search(r"美元|港币|港元|欧元|JPY|USD|HKD|EUR", line.text)
                       for line in anchors)):
            context = None
            continue

        default_page = report_default[0] if report_default else None
        default_line = report_default[1] if report_default else None
        default_scale = report_default[2] if report_default else None
        if local_units:
            unit_line, unit_scale = local_units[0]
            unit_page, unit_basis = page_no, "same_page_explicit"
            if default_scale is not None and unit_scale != default_scale and not local_currencies:
                context = None
                continue
        elif default_line is not None:
            unit_line, unit_scale = default_line, default_scale
            unit_page, unit_basis = default_page, "report_level_default"
        else:
            context = None
            continue
        if local_currencies:
            currency_line = local_currencies[0]
            currency_page, currency_basis = page_no, "same_page_explicit"
        elif default_line is not None:
            currency_line = default_line
            currency_page, currency_basis = default_page, "report_level_default"
        else:
            context = None
            continue

        statement_header = report_headers.get(page_no)
        default_header = report_headers.get(default_page) if default_page is not None else None
        if default_page is not None and (
                not statement_header or not default_header
                or _compact(_quotes(statement_header)) != _compact(_quotes(default_header))):
            context = None
            continue
        context.last_page = page_no
        context.page_numbers.append(page_no)
        report_header_spans = []
        for header_page in (*context.page_numbers, unit_page, currency_page):
            spans = report_headers.get(header_page)
            if spans and spans not in report_header_spans:
                report_header_spans.extend(spans)
        if default_page is not None and (not report_header_spans or any(
                _compact(_quotes((item,))) != _compact(_quotes((report_header_spans[0],)))
                for item in report_header_spans)):
            context = None
            continue

        centers = [sum(_bbox(glyphs)[::2]) / 2 for glyphs, _ in headers]
        separation = min(b - a for a, b in pairwise(centers))
        if separation <= 0:
            context = None
            continue
        note_headers = [glyphs for glyphs, _ in _headers(section[header_index])
                        if "附注" in _compact("".join(glyph.text for glyph in glyphs))]
        if len(note_headers) > 1:
            context = None
            continue
        label_right = (_bbox(note_headers[0])[0] if note_headers
                       else centers[0] - separation / 2)
        row_end = min((index for index in range(header_index + 1, len(section))
                       if re.search(boundary_pattern, section[index].text)), default=len(section))
        for row in section[header_index + 1:row_end]:
            labels = tuple(g for g in row.glyphs if g.bbox[0] < label_right)
            if not labels:
                continue
            label = _label("".join(g.text for g in labels))
            matches = {meta for alias, meta in _SOURCE_LABELS.items()
                       if _label(alias) == label and meta[1] == context.statement}
            if len(matches) != 1:
                continue
            code, statement, kind = next(iter(matches))
            if not label or (kind == "stock" and statement == "资产负债表"
                             and any(match.group(2) is None for _, match in headers)):
                continue
            for column, (header_glyphs, header_match) in enumerate(headers):
                left = ((centers[column - 1] + centers[column]) / 2 if column
                        else centers[0] - separation / 2)
                right = ((centers[column] + centers[column + 1]) / 2
                         if column + 1 < len(centers) else centers[-1] + separation / 2)
                amount = tuple(g for g in row.glyphs
                               if left <= (g.bbox[0] + g.bbox[2]) / 2 < right)
                value = _number("".join(g.text for g in amount))
                if value is None:
                    continue
                year = int(header_match.group(1))
                observations.append(NativeFactProof(
                    page_no=page_no, page_text_sha256=page_hashes[page_no],
                    table_title=context.title_spans,
                    period_header=_spans(page_no, text, header_glyphs),
                    unit_anchor=_spans(unit_page, page_texts[unit_page], unit_line.glyphs),
                    currency_anchor=_spans(currency_page, page_texts[currency_page],
                                            currency_line.glyphs),
                    label=_spans(page_no, text, labels), amount=_spans(page_no, text, amount),
                    row_bbox=_bbox(labels + amount), value_bbox=_bbox(amount),
                    concept_code=code, statement_type=statement,
                    period_start=date(year, 1, 1), period_end=date(year, 12, 31),
                    source_unit_scale=unit_scale, normalized_value=format(
                        _scaled(value, unit_scale), "f"), unit_binding_basis=unit_basis,
                    currency_binding_basis=currency_basis, report_header=tuple(report_header_spans),
                ))
    return observations, page_hashes, page_texts


def _quotes(spans) -> str:
    return "".join(span.quote for span in spans)


def _decision(fact: FactSnapshot, snapshot: FactAutoValidationInput,
              observations: list[NativeFactProof], page_hashes,
              page_texts) -> FactAutoValidationDecision:
    issues = []
    filing = snapshot.filing
    statement = STATEMENT_ALIASES.get(fact.statement_type)
    expected = CONCEPT_METADATA.get(fact.concept_code)
    if fact.filing_version_id != filing.id:
        issues.append("FACT_FILING_ID_MISMATCH")
    if expected is None or expected != (statement, fact.flow_or_stock):
        issues.append("FACT_CONCEPT_METADATA_MISMATCH")
    year = filing.period_end.year - int(fact.comparative_period)
    if fact.period_start != date(year, 1, 1) or fact.period_end != date(year, 12, 31):
        issues.append("FACT_PERIOD_UNSUPPORTED")
    if fact.is_restatement:
        return FactAutoValidationDecision(
            fact_id=fact.id, status="needs_review",
            issue_codes=("FACT_PERIOD_OR_RESTATEMENT_UNSUPPORTED",),
        )
    dimensions = sorted((item.dimension_name, item.dimension_value) for item in fact.dimensions)
    expected_dimensions = [("amount_basis", "normalized_currency"), ("scope", "consolidated")]
    if fact.comparative_period:
        expected_dimensions.append(("comparability_basis", "same_report_consolidated_annual_columns"))
        expected_dimensions.sort()
    if dimensions != expected_dimensions:
        issues.append("FACT_SCOPE_DIMENSION_INVALID")
    value = _number(fact.value)
    if (value is None or not value.is_finite() or fact.currency != "CNY"
            or fact.unit_scale not in _SCALES.values()):
        issues.append("FACT_VALUE_OR_UNIT_INVALID")
    if issues:
        return FactAutoValidationDecision(fact_id=fact.id, status="rejected", issue_codes=tuple(issues))
    matches = [proof for proof in observations if proof.concept_code == fact.concept_code
               and proof.statement_type == statement and proof.period_end.year == year]
    if len(matches) != 1:
        return FactAutoValidationDecision(fact_id=fact.id, status="needs_review", issue_codes=(
            "ORIGINAL_PRIMARY_FACT_NOT_UNIQUE" if matches else "ORIGINAL_PRIMARY_FACT_NOT_RECONSTRUCTED",
        ))
    proof = matches[0]
    for span in (*proof.unit_anchor, *proof.currency_anchor, *proof.report_header):
        source = page_texts.get(span.page_no)
        if (source is None or span.start < 0 or span.end > len(source)
                or source[span.start:span.end] != span.quote):
            issues.append("ORIGINAL_PROOF_SPAN_INVALID")
    if proof.report_header and len({_compact(_quotes((span,))) for span in proof.report_header}) != 1:
        issues.append("ORIGINAL_REPORT_HEADER_MISMATCH")
    if (Decimal(proof.normalized_value) != value or proof.source_unit_scale != fact.unit_scale
            or _compact(fact.raw_value) != _compact(_quotes(proof.amount))
            or (fact.concept_code, statement, fact.flow_or_stock) not in {
                meta for alias, meta in CONCEPT_LABELS.items()
                if _label(alias) == _label(fact.concept_name)}):
        issues.append("FACT_ORIGINAL_VALUE_LABEL_OR_UNIT_MISMATCH")
    if len(fact.evidence) != 1:
        issues.append("FACT_EVIDENCE_NOT_UNIQUE")
    for evidence in fact.evidence:
        row_box = _box(evidence.bounding_box.get("row"))
        value_box = _box(evidence.bounding_box.get("value"))
        unit_page_no = evidence.bounding_box.get("unit_page_no")
        unit_text = evidence.bounding_box.get("unit_text")
        unit_source = page_texts.get(unit_page_no) if isinstance(unit_page_no, int) else None
        unit_matches = list(_UNITS.finditer(_compact(unit_text))) if isinstance(unit_text, str) else []
        if (evidence.page_no != proof.page_no or evidence.page_sha256 != page_hashes.get(proof.page_no)
                or evidence.table_name != "合并" + proof.statement_type
                or _label(evidence.raw_label) != _label(_quotes(proof.label))
                or _compact(evidence.raw_value) != _compact(_quotes(proof.amount))
                or _compact(evidence.row_locator) != _compact(evidence.raw_label)
                or _compact(evidence.column_locator) != _compact(_quotes(proof.period_header))
                or not _same_box(row_box, proof.row_bbox)
                or not _same_box(value_box, proof.value_bbox)
                or evidence.bounding_box.get("source") != "text-layer"
                or evidence.bounding_box.get("extraction_job_id") != snapshot.extraction_job_id
                or evidence.bounding_box.get("scope") != proof.declared_scope
                or evidence.bounding_box.get("unit_scale") != proof.source_unit_scale
                or unit_source is None
                or not _compact(unit_text)
                or _compact(unit_text) not in _compact(unit_source)
                or len(unit_matches) != 1
                or _SCALES[unit_matches[0].group(1)] != proof.source_unit_scale
                or evidence.bounding_box.get("value_basis") != "normalized_currency"):
            issues.append("EVIDENCE_ORIGINAL_BINDING_MISMATCH")
        expected_extractor = ("native-comparative-v2" if fact.comparative_period
                              else snapshot.parser_version)
        if evidence.extractor != expected_extractor:
            issues.append("EVIDENCE_EXTRACTOR_VERSION_MISMATCH")
    return FactAutoValidationDecision(
        fact_id=fact.id, status="needs_review" if issues else "auto_passed",
        issue_codes=tuple(dict.fromkeys(issues)), source_proof=proof,
    )


def validate_native_fact_snapshot(
    pdf_bytes: bytes, snapshot: FactAutoValidationInput,
) -> FactAutoValidationReceipt:
    """Return source-binding decisions; no ORM, reviews, OCR, LLM or network."""
    # Pydantic instances can be forged through model_copy/model_construct. Rebuild
    # every nested field rather than trusting that an instance was validated once.
    snapshot = FactAutoValidationInput.model_validate(
        snapshot.model_dump(mode="python", warnings="error")
    )
    digest = _hash(pdf_bytes)
    snapshot_digest = _snapshot_hash(snapshot)
    filing = snapshot.filing
    reason, status = None, "candidate"
    if digest != filing.file_sha256:
        reason, status = "AUTO_FACT_SOURCE_BYTES_MISMATCH", "rejected"
    elif snapshot.parser_version != "pymupdf-statements-v5":
        reason, status = "AUTO_FACT_NATIVE_PARSER_VERSION_UNSUPPORTED", "rejected"
    elif snapshot.extraction_method != "pdf_text":
        reason = "AUTO_FACT_NATIVE_POLICY_EXCLUDES_OCR"
    elif snapshot.page_numbers is not None:
        reason = "AUTO_FACT_FULL_NATIVE_EXTRACTION_REQUIRED"
    elif (filing.report_type != "annual" or filing.period_basis != "annual"
          or filing.scope != "consolidated" or filing.accounting_standard != "PRC_ASBE"
          or filing.period_start != date(filing.period_end.year, 1, 1)
          or filing.period_end != date(filing.period_end.year, 12, 31)):
        reason = "AUTO_FACT_FILING_POLICY_UNSUPPORTED"
    if reason:
        decisions = tuple(FactAutoValidationDecision(fact_id=fact.id, status=status,
                          issue_codes=(reason,)) for fact in snapshot.facts)
    else:
        try:
            with pymupdf.open(stream=pdf_bytes, filetype="pdf") as document:
                if document.is_encrypted:
                    raise ValueError("AUTO_FACT_ENCRYPTED_PDF_UNSUPPORTED")
                observations, page_hashes, page_texts = _observations(document)
            decisions = tuple(_decision(fact, snapshot, observations, page_hashes, page_texts)
                              for fact in snapshot.facts)
        except (ValueError, RuntimeError) as exc:
            code = str(exc) if str(exc).startswith("AUTO_FACT_") else "AUTO_FACT_NATIVE_RECONSTRUCTION_FAILED"
            decisions = tuple(FactAutoValidationDecision(fact_id=fact.id, status="needs_review",
                              issue_codes=(code,)) for fact in snapshot.facts)
    # A duplicate active concept+period is ambiguous even if its original value is unique.
    keys = [(fact.concept_code, fact.period_start, fact.period_end) for fact in snapshot.facts]
    decisions = tuple(decision.model_copy(update={"status": "needs_review", "issue_codes": (
        "AUTO_FACT_ACTIVE_CONCEPT_PERIOD_AMBIGUOUS",), "source_proof": None})
        if keys.count(keys[index]) > 1 and decision.status == "auto_passed" else decision
        for index, decision in enumerate(decisions))
    return FactAutoValidationReceipt(
        extraction_job_id=snapshot.extraction_job_id, input_snapshot_sha256=snapshot_digest,
        actual_pdf_sha256=digest, source_bytes_match=digest == filing.file_sha256,
        decisions=decisions,
    )


def verify_native_fact_receipt(
    pdf_bytes: bytes, snapshot: FactAutoValidationInput, receipt: FactAutoValidationReceipt,
) -> FactAutoValidationReceipt:
    """Reconstruct every decision/proof from original bytes and compare all fields."""
    receipt = FactAutoValidationReceipt.model_validate(
        receipt.model_dump(mode="python", warnings="error")
    )
    reconstructed = validate_native_fact_snapshot(pdf_bytes, snapshot)
    if reconstructed.model_dump(mode="json") != receipt.model_dump(mode="json"):
        raise ValueError("AUTO_FACT_RECEIPT_RECONSTRUCTION_MISMATCH")
    return reconstructed

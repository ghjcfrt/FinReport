"""Finite, source-reconstructed value/change/cause observations for gap checks.

These observations prove that a particular cell or company attribution occurs
in a supplied source. They do not prove a financial interpretation, complete
report coverage, an independently established cause, or a reconciliation.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from finance_adapters.persistence.audit import stable_hash
from finance_adapters.providers.disclosed_relations import (
    DisclosureSource,
    DisclosureSpan,
    extract_disclosed_relations,
)

VERSION = "disclosure_facets_v1"
_NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_VALUE = re.compile(_NUMBER)
_CHANGE = re.compile(rf"{_NUMBER}%")
_DASHES = {"-", "—", "－"}
_UNIT = re.compile(r"单位\s*[:：]\s*(?:人民币)?(?:万|千|百)?元")
_LABEL_UNIT = re.compile(r"[（(](?:人民币)?(?:万|千|百)?元(?:[/／]股)?[）)]$")
_PERIOD = re.compile(r"\d{4}\s*年(?:\s*\d{1,2}(?:\s*[-至]\s*\d{1,2})?\s*月(?:\s*\d{1,2}\s*日)?)?")
_HEADING = re.compile(r"(?:[一二三四五六七八九十]+[、．]|\d+[、．]|[（(][一二三四五六七八九十\d]+[）)])")
ColumnRole = Literal["current_column", "comparative_column", "reporting_period", "year_to_date"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class FacetAnchor(_Strict):
    role: Literal["value", "change", "cause", "unit", "row", "disclosure"]
    span: DisclosureSpan


class FacetPeriodHeader(_Strict):
    role: Literal["current_column", "comparative_column", "change_column", "reporting_period",
                  "reporting_period_change", "year_to_date", "year_to_date_change"]
    span: DisclosureSpan


class DisclosureFacetObservation(_Strict):
    version: Literal["disclosure_facets_v1"] = VERSION
    id: str = Field(pattern=r"^facet:[a-f0-9]{24}$")
    source_id: str
    document_sha256: str
    text_sha256: str
    page_no: int | None
    subject: DisclosureSpan
    subject_key: str
    target_id: str
    kind: Literal["value", "change", "cause", "reconciliation"]
    column_role: ColumnRole
    period_headers: tuple[FacetPeriodHeader, ...] = Field(min_length=1)
    anchors: tuple[FacetAnchor, ...] = Field(min_length=1)
    extraction_rule: Literal["program_disclosed_relation", "two_period_numeric_table", "reporting_and_ytd_numeric_table"]
    related_disclosed_relation_id: str | None = None
    scope_status: Literal["verbatim_headers_not_inferred_interval_or_consolidation"] = (
        "verbatim_headers_not_inferred_interval_or_consolidation"
    )
    subject_matching: Literal["whitespace_and_unit_suffix_only_no_semantic_aliases"] = (
        "whitespace_and_unit_suffix_only_no_semantic_aliases"
    )
    numeric_computation_performed: Literal[False] = False
    business_cause_independently_verified: Literal[False] = False
    complete_report_coverage_asserted: Literal[False] = False
    formal_admitted: Literal[False] = False


class DisclosureFacetUnknown(_Strict):
    code: str
    span: DisclosureSpan
    message: str


class DisclosureFacetExtraction(_Strict):
    version: Literal["disclosure_facets_v1"] = VERSION
    parent_source: DisclosureSource
    observations: tuple[DisclosureFacetObservation, ...]
    unknowns: tuple[DisclosureFacetUnknown, ...]
    boundary: str = (
        "仅证明给定来源受支持表格/归因中存在相应单元格；缺少观察不等于公司未披露。"
        "科目、列角色、逐字期间表头须分别匹配；累计值不能替代本报告期值。"
        "金额、同比与公司归因均不能替代净利润与经营现金流调节表；不独立认证业务因果。"
    )


def _source(value: Mapping[str, Any] | DisclosureSource) -> DisclosureSource:
    if isinstance(value, DisclosureSource):
        return DisclosureSource.model_validate(value.model_dump(mode="python"))
    left, right = value.get("document_sha256"), value.get("source_sha256")
    if left is not None and right is not None and left != right:
        raise ValueError("DISCLOSURE_FACET_DOCUMENT_HASH_CONFLICT")
    source_id = value.get("source_id", value.get("id"))
    if value.get("id", source_id) != source_id:
        raise ValueError("DISCLOSURE_FACET_SOURCE_ID_CONFLICT")
    return DisclosureSource(id=source_id, document_sha256=left if left is not None else right,
                            text=value["text"], text_sha256=value["text_sha256"], page_no=value.get("page_no"))


def _span(text: str, start: int, end: int) -> DisclosureSpan:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return DisclosureSpan(start=start, end=end, quote=text[start:end])


def _lines(text: str) -> list[tuple[int, int, str]]:
    result, start = [], 0
    for raw in text.splitlines(keepends=True):
        if raw.strip():
            result.append((start, start + len(raw.rstrip("\r\n")), raw.strip()))
        start += len(raw)
    return result


def _key(subject: str) -> str:
    return _LABEL_UNIT.sub("", re.sub(r"\s+", "", subject))


def _observation(parent, subject, kind, column_role, headers, anchors, rule, relation_id=None):
    values = {"source_id": parent.id, "document_sha256": parent.document_sha256,
              "text_sha256": parent.text_sha256, "page_no": parent.page_no, "subject": subject,
              "subject_key": _key(subject.quote), "target_id": "verbatim:" + _key(subject.quote), "kind": kind,
              "column_role": column_role, "period_headers": tuple(headers), "anchors": tuple(anchors),
              "extraction_rule": rule, "related_disclosed_relation_id": relation_id}
    identity = {key: value.model_dump(mode="json") if isinstance(value, BaseModel) else
                [item.model_dump(mode="json") for item in value] if isinstance(value, tuple) else value
                for key, value in values.items()}
    return DisclosureFacetObservation(id="facet:" + stable_hash(identity)[:24], **values)


def _relation_observations(parent):
    observations = []
    for relation in extract_disclosed_relations(parent).relations:
        headers = [FacetPeriodHeader(role=item.role, span=item.span) for item in relation.period_anchors]
        common = [FacetAnchor(role="unit", span=relation.unit_anchor), FacetAnchor(role="row", span=relation.row_span)]
        for role, value in (("current_column", relation.current_amount), ("comparative_column", relation.comparative_amount)):
            observations.append(_observation(parent, relation.subject, "value", role, headers,
                [FacetAnchor(role="value", span=value), *common], "program_disclosed_relation", relation.id))
        if _CHANGE.fullmatch(relation.relative_change.quote):
            observations.append(_observation(parent, relation.subject, "change", "current_column", headers,
                [FacetAnchor(role="change", span=relation.relative_change), *common], "program_disclosed_relation", relation.id))
        observations.append(_observation(parent, relation.subject, "cause", "current_column", headers,
            [FacetAnchor(role="cause", span=relation.object), FacetAnchor(role="disclosure", span=relation.disclosure_span), *common],
            "program_disclosed_relation", relation.id))
    return observations


def _read_row(parent, lines, cursor, count, *, inline_unit):
    start = cursor
    while cursor < len(lines) and not (_VALUE.fullmatch(lines[cursor][2]) or lines[cursor][2] in _DASHES):
        if cursor - start >= 3 or _HEADING.match(lines[cursor][2]) or _UNIT.fullmatch(lines[cursor][2]):
            return None
        cursor += 1
    if cursor == start or cursor + count > len(lines):
        return None
    subject = _span(parent.text, lines[start][0], lines[cursor - 1][1])
    if len(subject.quote) > 160 or any(word in subject.quote for word in ("主要是", "说明", "注：", "是否")):
        return None
    unit_match = _LABEL_UNIT.search(subject.quote)
    if inline_unit and unit_match is None:
        return None
    cells = []
    for index in range(count):
        line = lines[cursor + index]
        matcher = _CHANGE if index % 2 and count == 4 or index == 2 and count == 3 else _VALUE
        if not matcher.fullmatch(line[2]) and line[2] not in _DASHES:
            return None
        cells.append(_span(parent.text, line[0], line[1]))
    unit = (_span(parent.text, subject.start + unit_match.start(), subject.start + unit_match.end())
            if unit_match is not None else None)
    row = _span(parent.text, lines[start][0], lines[cursor + count - 1][1])
    return subject, cells, unit, row, cursor + count


def _two_period_tables(parent, lines):
    observations = []
    for index, (_, _, label) in enumerate(lines):
        if label not in {"项目", "科目名称"} or index == 0 or index + 4 >= len(lines):
            continue
        if not _UNIT.fullmatch(lines[index - 1][2]):
            continue
        first, second, change = lines[index + 1:index + 4]
        if not (_PERIOD.fullmatch(first[2]) and _PERIOD.fullmatch(second[2]) and change[2] in {"同比增减", "变动率"}):
            continue
        if lines[index + 4][2] == "变动原因":
            continue  # The separately validated disclosed-relation grammar owns this layout.
        unit = _span(parent.text, lines[index - 1][0], lines[index - 1][1])
        headers = [FacetPeriodHeader(role=role, span=_span(parent.text, item[0], item[1])) for role, item in
                   (("current_column", first), ("comparative_column", second), ("change_column", change))]
        cursor = index + 4
        while cursor < len(lines):
            row = _read_row(parent, lines, cursor, 3, inline_unit=False)
            if row is None:
                break
            subject, cells, _, row_span, cursor = row
            common = [FacetAnchor(role="unit", span=unit), FacetAnchor(role="row", span=row_span)]
            for offset, kind, role in ((0, "value", "current_column"), (1, "value", "comparative_column"),
                                       (2, "change", "current_column")):
                if cells[offset].quote not in _DASHES:
                    observations.append(_observation(parent, subject, kind, role, headers,
                        [FacetAnchor(role=kind, span=cells[offset]), *common], "two_period_numeric_table"))
    return observations


def _header_phrase(parent, lines, cursor, phrase):
    start, content = cursor, ""
    while cursor < min(start + 3, len(lines)):
        content += re.sub(r"\s+", "", lines[cursor][2])
        cursor += 1
        if content == phrase:
            return _span(parent.text, lines[start][0], lines[cursor - 1][1]), cursor
        if not phrase.startswith(content):
            return None
    return None


def _reporting_and_ytd_tables(parent, lines):
    observations = []
    phrases = (("reporting_period", "本报告期"), ("reporting_period_change", "本报告期比上年同期增减"),
               ("year_to_date", "年初至报告期末"), ("year_to_date_change", "年初至报告期末比上年同期增减"))
    for start, (_, _, label) in enumerate(lines):
        if label != "本报告期":
            continue
        cursor, headers = start, []
        for role, phrase in phrases:
            header = _header_phrase(parent, lines, cursor, phrase)
            if header is None:
                break
            span, cursor = header
            headers.append(FacetPeriodHeader(role=role, span=span))
        if len(headers) != 4:
            continue
        while cursor < len(lines):
            row = _read_row(parent, lines, cursor, 4, inline_unit=True)
            if row is None:
                break
            subject, cells, unit, row_span, cursor = row
            common = [FacetAnchor(role="unit", span=unit), FacetAnchor(role="row", span=row_span)]
            for offset, kind, role in ((0, "value", "reporting_period"), (1, "change", "reporting_period"),
                                       (2, "value", "year_to_date"), (3, "change", "year_to_date")):
                if cells[offset].quote not in _DASHES:
                    observations.append(_observation(parent, subject, kind, role, headers,
                        [FacetAnchor(role=kind, span=cells[offset]), *common], "reporting_and_ytd_numeric_table"))
    return observations


def extract_disclosure_facets(source: Mapping[str, Any] | DisclosureSource) -> DisclosureFacetExtraction:
    """Observe only finite, exact source layouts; absence never means non-disclosure."""
    parent = _source(source)
    lines = _lines(parent.text)
    observed = [*_relation_observations(parent), *_two_period_tables(parent, lines),
                *_reporting_and_ytd_tables(parent, lines)]
    unique = {}
    for item in observed:
        # A row can be recognized by both the attribution and numeric grammar.
        # Keep one observation for that exact subject/cell/column, preferring
        # the already reconstructed attribution with its relation reference.
        anchor = item.anchors[0].span
        key = (item.source_id, item.subject.start, item.subject.end, item.kind, item.column_role,
               anchor.start, anchor.end)
        unique.setdefault(key, item)
    observations = tuple(sorted(unique.values(), key=lambda item: (item.subject.start, item.kind, item.column_role, item.id)))
    unknowns = [DisclosureFacetUnknown(code="RECONCILIATION_NOT_INFERRED", span=_span(parent.text, 0, len(parent.text)),
        message="净额、同比或变动归因不证明存在净利润与经营现金流调节；本版本不抽取调节表。")]
    if not observations:
        unknowns.append(DisclosureFacetUnknown(code="NO_SUPPORTED_FACET_LAYOUT", span=_span(parent.text, 0, len(parent.text)),
            message="本来源未匹配受支持的逐字单元格或公司归因语法；不表示全文缺少相应事实。"))
    return DisclosureFacetExtraction(parent_source=parent, observations=observations, unknowns=tuple(unknowns))


def validate_disclosure_facet(observation: Mapping[str, Any] | DisclosureFacetObservation,
                              source: Mapping[str, Any] | DisclosureSource) -> DisclosureFacetObservation:
    """Do not trust model-created facets, even if their JSON or IDs look valid."""
    candidate = observation.model_dump(mode="json") if isinstance(observation, BaseModel) else dict(observation)
    for expected in extract_disclosure_facets(source).observations:
        if candidate == expected.model_dump(mode="json"):
            return expected
    raise ValueError("DISCLOSURE_FACET_NOT_PROGRAM_RECONSTRUCTED")

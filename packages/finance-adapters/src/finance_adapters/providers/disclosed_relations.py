"""Conservative, source-only extraction of a company's disclosed change causes.

The supported grammars are two-column change/reason tables and change-cause
sentences that exactly join a row in the preceding two-column table.  These
records say what the company disclosed; they do not establish business causes.
Unsupported layouts remain unknown. No model text can be admitted by this API.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

VERSION = "disclosed_relations_v1"
_NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_AMOUNTS = re.compile(
    rf"(?P<first>{_NUMBER})\s+(?P<second>{_NUMBER})\s+"
    rf"(?P<change>{_NUMBER}%|[-—－])(?P<gap>\s*)"
)
_FIRST_NUMBER = re.compile(_NUMBER)
_NUMBER_LINE = re.compile(rf"(?:{_NUMBER}%?|[-—－])(?:\s+(?:{_NUMBER}%?|[-—－]))*\s*")
_DATE = re.compile(
    r"\d{4}(?:\s*[/.-]\s*\d{1,2}\s*[/.-]\s*\d{1,2}|"
    r"\s*年(?:\s*\d{1,2}(?:\s*[-至]\s*\d{1,2})?\s*月(?:\s*\d{1,2}\s*日)?)?)"
)
_UNIT = re.compile(r"单位\s*[:：]\s*(?:人民币)?(?:万|千|百)?元(?:\s*[/／]\s*股)?")
_CAUSE_START = re.compile(r"主要(?:是|系|由于)")
_CAUSE_END = re.compile(r"所致[。；;]?[ \t]*$")
_HEADING = re.compile(r"(?:[一二三四五六七八九十]+[、．]|[（(][一二三四五六七八九十\d]+[）)]|\d+[、．])")
_TABLE_CAPTIONS = {"资产负债表", "损益表", "现金流量表"}
# Numbering is not proof of a section transition: a numbered condition can
# still qualify the table immediately above it. Only complete, registered
# financial-report section labels close this deliberately finite grammar.
_SECTION_LABELS = {
    "股东信息", "募集资金使用情况", "非主营业务分析", "现金流",
    "主要会计数据和财务指标发生变动的情况及原因",
}
_FOLLOWING_DISCLOSURE_HEADING = re.compile(
    r"报告期内公司经营(?:活动产生的现金净流量与本年度净利润存在|现金流)重大差异(?:的)?原因说明"
)


def _verified_section(content: str) -> bool:
    prefix = _HEADING.match(content)
    return prefix is not None and content[prefix.end():].strip() in _SECTION_LABELS


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class DisclosureSpan(_Strict):
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1)

    @model_validator(mode="after")
    def valid_range(self):
        if self.end <= self.start or self.end - self.start != len(self.quote):
            raise ValueError("DISCLOSURE_SPAN_INVALID")
        return self


class DisclosureSource(_Strict):
    id: str = Field(min_length=1)
    document_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    text: str = Field(min_length=1, max_length=500000)
    text_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    page_no: int | None = Field(default=None, gt=0)
    completeness: Literal["full_parent_as_supplied_not_full_report"] = (
        "full_parent_as_supplied_not_full_report"
    )

    @model_validator(mode="after")
    def valid_hash(self):
        if _sha(self.text) != self.text_sha256:
            raise ValueError("DISCLOSURE_SOURCE_HASH_MISMATCH")
        return self


class DisclosurePeriodAnchor(_Strict):
    role: Literal["current_column", "comparative_column"]
    span: DisclosureSpan
    meaning: Literal["verbatim_table_header_not_inferred_interval"] = (
        "verbatim_table_header_not_inferred_interval"
    )


class DisclosedRelation(_Strict):
    version: Literal["disclosed_relations_v1"] = VERSION
    id: str = Field(pattern=r"^disclosed:[a-f0-9]{24}$")
    source_id: str
    document_sha256: str
    text_sha256: str
    subject_type: Literal["financial_statement_line_item"] = "financial_statement_line_item"
    subject: DisclosureSpan
    predicate: Literal["disclosed_cause"] = "disclosed_cause"
    object: DisclosureSpan
    row_span: DisclosureSpan
    disclosure_span: DisclosureSpan
    current_amount: DisclosureSpan
    comparative_amount: DisclosureSpan
    relative_change: DisclosureSpan
    period_anchors: tuple[DisclosurePeriodAnchor, DisclosurePeriodAnchor]
    unit_anchor: DisclosureSpan
    extraction_rule: Literal["two_column_change_reason_table", "table_linked_change_cause_sentence"]
    semantic_status: Literal["company_disclosure_only"] = "company_disclosure_only"
    business_cause_independently_verified: Literal[False] = False
    statement: str


class DisclosureUnknown(_Strict):
    code: str
    span: DisclosureSpan
    message: str


class DisclosedExtraction(_Strict):
    version: Literal["disclosed_relations_v1"] = VERSION
    parent_source: DisclosureSource
    relations: tuple[DisclosedRelation, ...]
    unknowns: tuple[DisclosureUnknown, ...]
    boundary: str = (
        "仅重述本来源明确披露的科目与原因，不独立证明业务因果；"
        "不支持的结构保留为 unknown，选页或父片段不等于整份报告。"
    )


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _source(value: Mapping[str, Any] | DisclosureSource) -> DisclosureSource:
    if isinstance(value, DisclosureSource):
        return value
    # Period/scope metadata is deliberately not promoted to claim-level proof.
    return DisclosureSource(
        id=value["id"], text=value["text"], text_sha256=value["text_sha256"],
        document_sha256=value.get("document_sha256", value.get("source_sha256")),
        page_no=value.get("page_no"),
    )


def _span(text: str, start: int, end: int) -> DisclosureSpan:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return DisclosureSpan(start=start, end=end, quote=text[start:end])


def _lines(text: str) -> list[tuple[int, int, str]]:
    result = []
    start = 0
    for raw in text.splitlines(keepends=True):
        if raw.strip():
            result.append((start, start + len(raw.rstrip("\r\n")), raw.strip()))
        start += len(raw)
    return result


def _unit(text: str, lines, header: int) -> DisclosureSpan | None:
    for start, end, content in reversed(lines[max(0, header - 6):header]):
        match = _UNIT.fullmatch(content)
        if match:
            return _span(text, start, end)
        if content in {"变动原因", "形成原因说明", "是否具有可持续性"}:
            break
    return None


def _anchors(text: str, lines, header: int):
    if header + 3 >= len(lines):
        return None
    first, second, change = lines[header + 1:header + 4]
    if not (_DATE.fullmatch(first[2]) and _DATE.fullmatch(second[2])):
        return None
    if change[2] not in {"变动率", "同比增减"}:
        return None
    for _, _, context in reversed(lines[:header]):
        compact = context.replace(" ", "")
        if re.fullmatch(r"[□☐☑√]适用[□☐☑√]不适用", compact):
            if not re.fullmatch(r"[☑√]适用[□☐]不适用", compact):
                return None
            break
        if _verified_section(context):
            break
        if _HEADING.match(context):
            return None
    unit = _unit(text, lines, header)
    if unit is None:
        return None
    anchors = (
        DisclosurePeriodAnchor(role="current_column", span=_span(text, first[0], first[1])),
        DisclosurePeriodAnchor(role="comparative_column", span=_span(text, second[0], second[1])),
    )
    return anchors, unit


def _subject_number(text: str, lines, cursor: int, stop: int):
    """Only a bounded label immediately followed by two numeric cells qualifies."""
    start = lines[cursor][0]
    for i in range(cursor, min(cursor + 3, stop)):
        line_start, line_end, content = lines[i]
        if _HEADING.match(content) or _UNIT.fullmatch(content):
            return None
        match = _FIRST_NUMBER.search(text, line_start, line_end)
        if match:
            subject = _span(text, start, match.start()) if match.start() > start else None
            if subject is None or len(subject.quote) > 120:
                return None
            label_lines = subject.quote.splitlines()
            if len(label_lines) > 1:
                opening = sum(label_lines[0].count(c) for c in "（(")
                closing = sum(label_lines[0].count(c) for c in "）)")
                if opening <= closing:
                    # An arbitrary preceding condition/footnote is not another
                    # label line. Only a visible parenthetical wrap is supported.
                    return None
            if any(term in subject.quote for term in ("主要是", "变动原因", "同比变动", "%")):
                return None
            numbers = _AMOUNTS.match(text, match.start())
            if numbers is None or numbers.end("change") > lines[min(i + 3, stop - 1)][1]:
                return None
            return subject, numbers
    return None


def _relation(source, subject, numbers, row, disclosure, cause, anchors, unit, rule):
    values = {
        "source_id": source.id, "document_sha256": source.document_sha256,
        "text_sha256": source.text_sha256, "subject": subject,
        "object": cause, "row_span": row, "disclosure_span": disclosure,
        "current_amount": _span(source.text, numbers.start("first"), numbers.end("first")),
        "comparative_amount": _span(source.text, numbers.start("second"), numbers.end("second")),
        "relative_change": _span(source.text, numbers.start("change"), numbers.end("change")),
        "period_anchors": anchors, "unit_anchor": unit, "extraction_rule": rule,
        "statement": f"公司披露：{subject.quote}；{cause.quote}",
    }
    digestable = {
        key: ([item.model_dump(mode="json") for item in value]
              if isinstance(value, tuple) else
              value.model_dump(mode="json") if isinstance(value, BaseModel) else value)
        for key, value in values.items()
    }
    return DisclosedRelation(id="disclosed:" + _sha(_canonical(digestable))[:24], **values)


def _unknown(text, start, end, code, message):
    return DisclosureUnknown(code=code, span=_span(text, start, end), message=message)


def _reason_table(source, lines, header, stop, anchors, unit):
    text = source.text
    relations, unknowns = [], []
    cursor = header + 5
    while cursor < stop:
        parsed = _subject_number(text, lines, cursor, stop)
        if parsed is None:
            unknowns.append(_unknown(text, lines[cursor][0], lines[stop - 1][1],
                                     "UNSUPPORTED_CHANGE_ROW", "科目与连续数值单元格不匹配，停止本表。"))
            break
        subject, numbers = parsed
        cause_start = numbers.end()
        cause_match = _CAUSE_START.match(text, cause_start)
        if cause_match is None:
            unknowns.append(_unknown(text, lines[cursor][0], lines[stop - 1][1],
                                     "CAUSE_MARKER_MISSING", "数值后缺少支持的归因起始词，停止本表。"))
            break
        cause_line = next(i for i in range(cursor, stop) if lines[i][0] <= cause_start < lines[i][1])
        end_line = None
        for i in range(cause_line, min(cause_line + 3, stop)):
            content = lines[i][2]
            if _CAUSE_END.search(content):
                # A line immediately followed by numeric cells can be another
                # row's label, even if it happens to contain the suffix 所致.
                if i > cause_line and i + 1 < stop and _NUMBER_LINE.fullmatch(lines[i + 1][2]):
                    break
                end_line = i
                break
            if i > cause_line and (
                _NUMBER_LINE.fullmatch(content) or _HEADING.match(content)
                or _subject_number(text, lines, i, stop) is not None
            ):
                break
        if end_line is None:
            unknowns.append(_unknown(text, lines[cursor][0], lines[stop - 1][1],
                                     "CAUSE_CONTINUATION_UNRESOLVED", "原因未在支持的连续行内闭合，不跨下一科目拼接。"))
            break
        next_cursor = end_line + 1
        if next_cursor < stop and _subject_number(text, lines, next_cursor, stop) is None:
            unknowns.append(_unknown(text, lines[cursor][0], lines[stop - 1][1],
                                     "UNRESOLVED_ROW_TRAILER", "原因后存在未解析的附加文字，不丢弃限定语后生成关系。"))
            relations.clear()
            break
        cause = _span(text, cause_start, lines[end_line][1])
        row = _span(text, subject.start, cause.end)
        relations.append(_relation(source, subject, numbers, row, row, cause, anchors, unit,
                                   "two_column_change_reason_table"))
        cursor = end_line + 1
    return relations, unknowns


def _decimal_percent(value: str) -> Decimal | None:
    try:
        return Decimal(value.rstrip("%").replace(",", ""))
    except InvalidOperation:
        return None


def _sentence_causes(source, lines, header, stop, anchors, unit):
    """Join prose only to an exact row name and equal disclosed rate, never proximity."""
    text = source.text
    marker = next((i for i in range(header + 4, stop)
                   if lines[i][2] == "相关数据同比发生重大变动的主要影响因素说明"), None)
    if marker is None:
        return [], []
    rows = {}
    cursor = header + 4
    while cursor < marker:
        parsed = _subject_number(text, lines, cursor, marker)
        if parsed is None:
            return [], [_unknown(text, lines[cursor][0], lines[marker][1],
                                 "UNSUPPORTED_COMPARISON_TABLE", "比较表行结构不明，拒绝邻近关联原因。")]
        subject, numbers = parsed
        if numbers.group("change") in {"-", "—", "－"}:
            return [], [_unknown(text, subject.start, numbers.end(),
                                 "CHANGE_RATE_MISSING", "连续披露句要求表内有明确变动率。")]
        if subject.quote in rows:
            return [], [_unknown(text, subject.start, numbers.end(),
                                 "DUPLICATE_SUBJECT", "同表科目重复，归因关联不唯一。")]
        rows[subject.quote] = (subject, numbers)
        last_cell_end = numbers.end("change")
        cursor = next((i for i in range(cursor, marker) if lines[i][1] >= last_cell_end), marker) + 1
    # Applicability is a source condition. Do not discard a negative/ambiguous marker.
    if marker + 1 >= stop or "适用" not in lines[marker + 1][2]:
        return [], [_unknown(text, lines[marker][0], lines[marker][1],
                             "APPLICABILITY_MISSING", "缺少明确适用标记。")]
    applicable = lines[marker + 1][2].replace(" ", "")
    if not re.fullmatch(r"[☑√]适用[□☐]不适用", applicable):
        return [], [_unknown(text, lines[marker][0], lines[marker + 1][1],
                             "APPLICABILITY_NOT_AFFIRMED", "适用标记未明确肯定，不生成归因。")]
    cursor = lines[marker + 2][0] if marker + 2 < stop else len(text)
    end = lines[stop][0] if stop < len(lines) else len(text)
    relations, unknowns = [], []
    pattern = re.compile(
        rf"(?P<subject>[^\n；;。]+?)同比变动(?P<change>{_NUMBER}%)\s*[,，]\s*"
        r"(?P<cause>主要(?:是|系|由于)[^；;。]+?所致)[；;。]"
    )
    while cursor < end:
        while cursor < end and text[cursor].isspace():
            cursor += 1
        if cursor >= end:
            break
        match = pattern.match(text, cursor, end)
        if match is None:
            # Only an explicit following disclosure heading closes this grammar.
            # An arbitrary trailer could qualify the preceding cause.
            line = next(item for item in lines if item[0] <= cursor <= item[1])
            remainder = text[cursor:line[1]].strip()
            if not (cursor == line[0] and _FOLLOWING_DISCLOSURE_HEADING.fullmatch(remainder)):
                relations.clear()
                unknowns.append(_unknown(text, cursor, line[1],
                                         "UNRESOLVED_SENTENCE_TRAILER", "披露句后有未解析文字，不忽略外部限定语。"))
            break
        row = rows.get(match.group("subject"))
        if row is None or _decimal_percent(row[1].group("change")) != _decimal_percent(match.group("change")):
            unknowns.append(_unknown(text, match.start(), match.end(),
                                     "SENTENCE_ROW_NOT_MATCHED", "披露句主语/变动率未唯一匹配前表。"))
        else:
            subject, numbers = row
            cause = _span(text, match.start("cause"), match.end("cause"))
            if len(cause.quote.splitlines()) > 2 or re.search(
                rf"\n[^\n；;。]*同比变动{_NUMBER}%", cause.quote
            ):
                unknowns.append(_unknown(text, match.start(), match.end(),
                                         "SENTENCE_BOUNDARY_UNRESOLVED", "原因可能跨越下一披露句，拒绝关联。"))
                cursor = match.end()
                continue
            disclosure = _span(text, match.start(), match.end())
            row_span = _span(text, subject.start, numbers.end("change"))
            relations.append(_relation(source, subject, numbers, row_span, disclosure,
                                       cause, anchors, unit, "table_linked_change_cause_sentence"))
        cursor = match.end()
    return relations, unknowns


def extract_disclosed_relations(
    source: Mapping[str, Any] | DisclosureSource,
) -> DisclosedExtraction:
    """Extract source-only observations; caller metadata cannot supply missing anchors."""
    parent = _source(source)
    lines = _lines(parent.text)
    relations, unknowns = [], []
    for header, (_, _, content) in enumerate(lines):
        if content not in {"科目名称", "项目"}:
            continue
        anchored = _anchors(parent.text, lines, header)
        if anchored is None:
            continue
        anchors, unit = anchored
        has_cause = header + 4 < len(lines) and lines[header + 4][2] == "变动原因"
        if has_cause:
            stop = next((i for i in range(header + 5, len(lines))
                         if _verified_section(lines[i][2]) or lines[i][2] in {
                             *_TABLE_CAPTIONS, "科目名称", "项目"
                         } or _UNIT.fullmatch(lines[i][2])), len(lines))
            found, unresolved = _reason_table(parent, lines, header, stop, anchors, unit)
        else:
            stop = next((i for i in range(header + 4, len(lines))
                         if _verified_section(lines[i][2]) or _UNIT.fullmatch(lines[i][2])), len(lines))
            found, unresolved = _sentence_causes(parent, lines, header, stop, anchors, unit)
        relations.extend(found)
        unknowns.extend(unresolved)
    if not relations and not unknowns:
        unknowns.append(_unknown(parent.text, 0, len(parent.text),
                                 "NO_SUPPORTED_DISCLOSURE_LAYOUT", "没有受支持且期间、单位完整的归因结构。"))
    return DisclosedExtraction(parent_source=parent, relations=tuple(relations), unknowns=tuple(unknowns))


def validate_disclosed_relation(
    relation: Mapping[str, Any] | DisclosedRelation,
    source: Mapping[str, Any] | DisclosureSource,
) -> DisclosedRelation:
    """Rebuild from the supplied source, rejecting every caller-controlled field mutation."""
    candidate = (relation.model_dump(mode="json") if isinstance(relation, DisclosedRelation)
                 else dict(relation))
    for expected in extract_disclosed_relations(source).relations:
        if candidate == expected.model_dump(mode="json"):
            return expected
    raise ValueError("DISCLOSURE_RELATION_NOT_PROGRAM_RECONSTRUCTED")


def render_disclosed_relation(
    relation: Mapping[str, Any] | DisclosedRelation,
    source: Mapping[str, Any] | DisclosureSource,
) -> str:
    """Render only a source-reconstructed record, never an unverified model paraphrase."""
    return validate_disclosed_relation(relation, source).statement

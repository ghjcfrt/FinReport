from __future__ import annotations

import csv
import html
from datetime import UTC, datetime
from pathlib import Path

import fitz
from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_adapters.config import settings
from finance_adapters.persistence.models import (
    AnalysisRun,
    AnomalyObservation,
    FilingVersion,
    MetricResult,
)


def generate_export(session: Session, filing_id: int, output_format: str) -> Path:
    filing = session.get(FilingVersion, filing_id)
    metrics = list(
        session.scalars(select(MetricResult).where(MetricResult.filing_version_id == filing_id))
    )
    anomalies = list(session.scalars(select(AnomalyObservation).where(AnomalyObservation.filing_version_id == filing_id)))
    analysis = session.scalar(
        select(AnalysisRun)
        .where(AnalysisRun.filing_version_id == filing_id, AnalysisRun.status == "completed")
        .order_by(AnalysisRun.id.desc())
    )
    folder = settings.storage_dir / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    base = folder / f"filing-{filing_id}-{timestamp}"
    if output_format == "csv":
        target = base.with_suffix(".csv")
        with target.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["metric_code", "value", "unit", "status", "formula_version", "input_snapshot_hash"]
            )
            writer.writerows(
                [
                    [
                        m.metric_code,
                        m.value,
                        m.unit,
                        m.applicability_status,
                        m.formula_version,
                        m.input_snapshot_hash,
                    ]
                    for m in metrics
                ]
            )
        return target
    rows = "".join(
        f"<tr><td>{html.escape(m.metric_code)}</td><td>{html.escape(str(m.value or '不可用'))}</td><td>{html.escape(m.unit)}</td><td>{html.escape(m.applicability_status)}</td><td>{html.escape(m.formula_version)}</td></tr>"
        for m in metrics
    )
    claims = "".join(
        f"<li>{html.escape(item.get('content', ''))}</li>"
        for item in ((analysis.output or {}).get("claims", []) if analysis else [])
    )
    evidence_ids = sorted({evidence_id for item in ((analysis.output or {}).get("claims", []) if analysis else []) for evidence_id in item.get("evidence_fragment_ids", [])})
    evidence_rows = "".join(f"<li>证据片段 {item}</li>" for item in evidence_ids) or "<li>无</li>"
    anomaly_rows = "".join(f"<li>{html.escape(item.rule_code)}：{html.escape(item.status)}；{html.escape(item.explanation)}</li>" for item in anomalies) or "<li>无</li>"
    trace = html.escape(str((analysis.tool_trace or {}).get("tools", []))) if analysis else "无"
    page = f"""<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\"><title>FinReport 导出</title><style>body{{font-family:Arial,'Microsoft YaHei',sans-serif;margin:32px}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #bbb;padding:6px;text-align:left}}footer{{margin-top:30px;color:#666}}</style><body><h1>财报分析报告</h1><p>披露版本：{filing.id}；生成时间：{datetime.now(UTC).isoformat()}；规则版本：general-non-financial-v1；模型：{html.escape(analysis.model if analysis else '无')}；模型版本：{html.escape(analysis.model_version if analysis else '无')}；Prompt：{html.escape(analysis.prompt_version if analysis else '无')}；编排：{html.escape(analysis.orchestration_version if analysis else '无')}；验证器：{html.escape(analysis.validator_version if analysis else '无')}</p><h2>结构化指标</h2><table><tr><th>指标</th><th>值</th><th>单位</th><th>状态</th><th>公式版本</th></tr>{rows}</table><h2>异常观察</h2><ul>{anomaly_rows}</ul><h2>已验证分析结论</h2><ul>{claims or "<li>无已完成分析</li>"}</ul><h2>证据索引</h2><ul>{evidence_rows}</ul><h2>工具调用摘要</h2><pre>{trace}</pre><footer>仅供研究与教学，不构成投资建议。导出仅包含当前冻结输入版本。</footer></body></html>"""
    html_path = base.with_suffix(".html")
    html_path.write_text(page, encoding="utf-8")
    if output_format == "html":
        return html_path
    target = base.with_suffix(".pdf")
    pdf = fitz.open()
    pdf_page = pdf.new_page()
    pdf_page.insert_textbox(
        fitz.Rect(36, 36, 560, 800),
        "FinReport export\n\n"
        + "\n".join(
            f"{m.metric_code}: {m.value} {m.unit} ({m.applicability_status})" for m in metrics
        )
        + "\n\n仅供研究与教学，不构成投资建议。",
        fontsize=10,
    )
    pdf.save(target)
    return target

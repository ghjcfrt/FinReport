"""Fail-closed peer statistics over frozen, comparable issuer inputs.

There is no authenticated peer approval registry in the current application.
The public API therefore creates candidates only and supplies no trust verifier.
Caller-provided names or ``verified`` flags are never evidence of approval.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext

from finance_domain.metrics.definitions import METRIC_DEFINITIONS, is_financial_industry
from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import stable_hash
from .metric_gateway import (
    FORMULA_VERSION,
    _snapshot,
    current_verified_facts,
    require_source_assurance,
)
from .models import (
    Company,
    ComparisonRun,
    EvidenceFragment,
    FactReview,
    FilingVersion,
    MetricResult,
    PeerUniverse,
)
from .review_gateway import _located

GATE_VERSION = "peer-gate-v1"
MINIMUM_COVERAGE = Decimal("0.80")
HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
TrustVerifier = Callable[[str, dict], bool]


def _jsonable(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


class PeerGateError(ValueError):
    def __init__(self, code: str, detail: dict | None = None):
        super().__init__(code)
        self.code, self.detail = code, detail or {}


def _decimal_coverage(value: object) -> Decimal:
    try:
        coverage = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PeerGateError("INVALID_PEER_COVERAGE") from exc
    if not coverage.is_finite() or not Decimal(0) <= coverage <= Decimal(1):
        raise PeerGateError("INVALID_PEER_COVERAGE")
    return coverage


def _ids(value: object) -> list[int]:
    if not isinstance(value, list) or any(type(item) is not int or item <= 0 for item in value):
        raise PeerGateError("INVALID_PEER_IDENTIFIERS")
    if len(set(value)) != len(value):
        raise PeerGateError("DUPLICATE_PEER_IDENTIFIERS")
    return sorted(value)


def _date(value: object, code: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise PeerGateError(code) from exc


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    # Avoid both binary float conversion and process-global Decimal precision.
    with localcontext() as context:
        context.prec = max(64, max(len(item.as_tuple().digits) for item in ordered) + 4)
        return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


def _population(
    session: Session, metadata: dict, industry: str, taxonomy: str, cutoff: date,
) -> dict:
    if not isinstance(metadata, dict) or "company_ids" not in metadata:
        raise PeerGateError("PEER_POPULATION_PROVENANCE_REQUIRED")
    company_ids = _ids(metadata.get("company_ids"))
    if not company_ids:
        raise PeerGateError("PEER_POPULATION_DENOMINATOR_REQUIRED")
    if not metadata.get("source_uri") or not HASH_PATTERN.fullmatch(
        str(metadata.get("source_sha256", ""))
    ):
        raise PeerGateError("PEER_POPULATION_PROVENANCE_REQUIRED")
    if _date(metadata.get("as_of"), "INVALID_POPULATION_AS_OF") > cutoff:
        raise PeerGateError("POPULATION_AFTER_CUTOFF")
    companies = list(session.scalars(select(Company).where(Company.id.in_(company_ids))))
    if len(companies) != len(company_ids):
        raise PeerGateError("PEER_POPULATION_COMPANY_NOT_FOUND")
    if any(item.industry_code != industry or item.industry_taxonomy_version != taxonomy
           for item in companies):
        raise PeerGateError("PEER_POPULATION_CLASSIFICATION_MISMATCH")
    value = {
        "source_uri": str(metadata["source_uri"]),
        "source_sha256": metadata["source_sha256"], "as_of": str(metadata["as_of"]),
        "companies": [{"company_id": item.id, "stock_code": item.stock_code,
                       "industry_code": item.industry_code,
                       "taxonomy_version": item.industry_taxonomy_version}
                      for item in sorted(companies, key=lambda item: item.id)],
    }
    value["population_hash"] = stable_hash(value)
    return value


def _qualified_member(
    session: Session, filing: FilingVersion, metric_code: str, *, data_cutoff: date,
) -> dict:
    if filing.status not in {"facts_verified", "metrics_ready", "analysis_ready"}:
        raise PeerGateError("PEER_STRICT_FACTS_REQUIRED", {"filing_id": filing.id})
    replacements = session.scalars(select(FilingVersion).where(
        FilingVersion.supersedes_filing_version_id == filing.id,
        FilingVersion.company_id == filing.company_id,
        FilingVersion.period_start == filing.period_start,
        FilingVersion.period_end == filing.period_end,
        FilingVersion.report_type == filing.report_type,
        FilingVersion.scope == filing.scope,
        FilingVersion.accounting_standard == filing.accounting_standard,
        FilingVersion.period_basis == filing.period_basis,
        FilingVersion.published_at <= data_cutoff,
        FilingVersion.status.in_({"facts_verified", "metrics_ready", "analysis_ready"}),
    ))
    for replacement in replacements:
        if not replacement.source_uri or not HASH_PATTERN.fullmatch(replacement.file_sha256):
            continue
        try:
            require_source_assurance(session, replacement)
        except ValueError:
            continue
        raise PeerGateError("SUPERSEDED_PEER_FILING", {"filing_id": filing.id,
                                                     "superseding_filing_id": replacement.id})
    try:
        require_source_assurance(session, filing)
        facts = current_verified_facts(session, filing)
    except ValueError as exc:
        raise PeerGateError(str(exc), {"filing_id": filing.id}) from exc
    definition = METRIC_DEFINITIONS.get(metric_code)
    if definition is None:
        raise PeerGateError("UNKNOWN_PEER_METRIC")
    company = session.get(Company, filing.company_id)
    if company is None:
        raise PeerGateError("PEER_COMPANY_NOT_FOUND")
    if (is_financial_industry(company.industry_code)
            and metric_code in {"gross_margin", "net_margin", "operating_cashflow_to_net_income",
                                "free_cash_flow"}):
        raise PeerGateError("PEER_METRIC_NOT_APPLICABLE")
    items = list(session.scalars(select(MetricResult).where(
        MetricResult.filing_version_id == filing.id, MetricResult.metric_code == metric_code,
        MetricResult.applicability_status == "calculated",
    )))
    if len(items) != 1:
        raise PeerGateError("AMBIGUOUS_OR_MISSING_PEER_METRIC", {"filing_id": filing.id})
    item = items[0]
    if (item.computation_mode != "strict_audited" or item.quality_status != "strict_audited"
            or item.formula_version != FORMULA_VERSION or item.scope != filing.scope
            or item.period_basis != filing.period_basis or item.unit != definition.unit):
        raise PeerGateError("PEER_METRIC_DIMENSIONS_MISMATCH", {"metric_id": item.id})
    if not facts or item.input_snapshot_hash != _snapshot(facts):
        raise PeerGateError("STALE_PEER_METRIC_SNAPSHOT", {"metric_id": item.id})
    if any(code not in facts for code in definition.required_concepts):
        raise PeerGateError("PEER_METRIC_INPUTS_MISSING")
    inputs = [facts[code] for code in definition.required_concepts]
    if set(item.input_fact_ids) != {fact.id for fact in inputs} or len(item.input_fact_ids) != len(inputs):
        raise PeerGateError("PEER_METRIC_INPUT_IDS_MISMATCH")
    evidence_snapshot = []
    for fact in inputs:
        evidence = list(session.scalars(select(EvidenceFragment).where(
            EvidenceFragment.fact_id == fact.id,
        ).order_by(EvidenceFragment.id)))
        reviews = list(session.scalars(select(FactReview).where(FactReview.fact_id == fact.id)))
        confirmed = {entry.reviewer_id.strip() for entry in reviews
                     if entry.decision in {"confirmed", "corrected"} and entry.reviewer_id.strip()}
        if (not evidence or any(not _located(entry) for entry in evidence)
                or any(entry.decision == "rejected" for entry in reviews)
                or len(confirmed) < 2
                or fact.unit_scale not in {1, 1000, 10000, 1000000, 100000000}):
            raise PeerGateError("PEER_VERIFIED_LOCATED_INPUTS_REQUIRED", {"fact_id": fact.id})
        evidence_snapshot.append({
            "fact_id": fact.id, "concept_code": fact.concept_code, "value": str(fact.value),
            "currency": fact.currency, "unit_scale": fact.unit_scale, "revision": fact.revision_no,
            "evidence": [{"id": entry.id, "page": entry.page_no, "page_sha256": entry.page_sha256,
                          "row": entry.row_locator, "column": entry.column_locator,
                          "raw_value": entry.raw_value, "bbox": entry.bounding_box}
                         for entry in evidence],
        })
    try:
        expected = definition.evaluate({code: fact.value for code, fact in facts.items()})
        value = Decimal(item.value) if item.value is not None else Decimal("NaN")
        if not value.is_finite() or value != expected.quantize(Decimal("0.00000001")):
            raise PeerGateError("PEER_METRIC_VALUE_MISMATCH", {"metric_id": item.id})
    except (InvalidOperation, ValueError, ZeroDivisionError) as exc:
        if isinstance(exc, PeerGateError):
            raise
        raise PeerGateError("PEER_METRIC_REFERENCE_FAILED", {"metric_id": item.id}) from exc
    return {
        "filing_id": filing.id, "company_id": company.id, "stock_code": company.stock_code,
        "industry_code": company.industry_code, "taxonomy_version": company.industry_taxonomy_version,
        "file_sha256": filing.file_sha256, "source_uri": filing.source_uri,
        "published_at": str(filing.published_at), "metric_id": item.id, "value": str(value),
        "formula_version": item.formula_version, "unit": item.unit,
        "fact_snapshot_hash": item.input_snapshot_hash, "input_facts": evidence_snapshot,
        "dimensions": {"period_start": str(filing.period_start), "period_end": str(filing.period_end),
                       "report_type": filing.report_type, "period_basis": filing.period_basis,
                       "scope": filing.scope, "currency": "CNY",
                       "accounting_standard": filing.accounting_standard},
    }


def build_peer_snapshot(
    session: Session, *, version: str, filing_ids: list[int], metric_code: str,
    industry_code: str, taxonomy_version: str, data_cutoff: date, population: dict,
) -> dict:
    """Freeze data, without approving its provenance or any person's identity."""
    ids = _ids(filing_ids)
    population_snapshot = _population(
        session, population, industry_code, taxonomy_version, data_cutoff,
    )
    filings = list(session.scalars(select(FilingVersion).where(FilingVersion.id.in_(ids))))
    if len(filings) != len(ids):
        raise PeerGateError("PEER_FILING_NOT_FOUND")
    issuer_ids = {filing.company_id for filing in filings}
    if len(issuer_ids) != len(ids):
        raise PeerGateError("DUPLICATE_PEER_ISSUER")
    if len(issuer_ids) < 10:
        raise PeerGateError("INSUFFICIENT_DISTINCT_PEER_ISSUERS")
    population_ids = {item["company_id"] for item in population_snapshot["companies"]}
    if not issuer_ids.issubset(population_ids):
        raise PeerGateError("PEER_NOT_IN_FROZEN_POPULATION")
    with localcontext() as context:
        context.prec = 64
        coverage = Decimal(len(issuer_ids)) / Decimal(len(population_ids))
    if coverage < MINIMUM_COVERAGE:
        raise PeerGateError("INSUFFICIENT_PROVEN_PEER_COVERAGE", {"coverage": str(coverage)})
    members = []
    for filing in sorted(filings, key=lambda item: item.id):
        if (not filing.published_at or filing.published_at > data_cutoff
                or not filing.source_uri or not HASH_PATTERN.fullmatch(filing.file_sha256)):
            raise PeerGateError("PEER_SOURCE_OR_CUTOFF_INVALID", {"filing_id": filing.id})
        member = _qualified_member(session, filing, metric_code, data_cutoff=data_cutoff)
        if member["industry_code"] != industry_code or member["taxonomy_version"] != taxonomy_version:
            raise PeerGateError("PEER_CLASSIFICATION_MISMATCH", {"filing_id": filing.id})
        if members and member["dimensions"] != members[0]["dimensions"]:
            raise PeerGateError("PEER_FILING_DIMENSIONS_MISMATCH", {"filing_id": filing.id})
        members.append(member)
    return {"gate_version": GATE_VERSION, "version": version, "metric_code": metric_code,
            "industry_code": industry_code, "taxonomy_version": taxonomy_version,
            "data_cutoff": str(data_cutoff), "population": population_snapshot,
            "coverage": str(coverage), "distinct_issuer_count": len(issuer_ids), "members": members}


def save_peer_candidate(session: Session, payload: dict) -> dict:
    required = {"version", "industry_code", "taxonomy_version", "data_cutoff", "filing_ids", "coverage"}
    if not required.issubset(payload):
        raise PeerGateError("PEER_UNIVERSE_FIELDS_REQUIRED")
    coverage_claim = _decimal_coverage(payload["coverage"])
    ids = _ids(payload["filing_ids"])
    cutoff = _date(payload["data_cutoff"], "INVALID_PEER_CUTOFF")
    for field in ("version", "industry_code", "taxonomy_version"):
        if not isinstance(payload[field], str) or not payload[field].strip() or len(payload[field]) > 64:
            raise PeerGateError("INVALID_PEER_UNIVERSE_METADATA")
    if session.scalar(select(PeerUniverse.id).where(PeerUniverse.version == payload["version"])):
        raise PeerGateError("PEER_UNIVERSE_VERSION_ALREADY_EXISTS")
    snapshot, reasons = None, []
    try:
        snapshot = build_peer_snapshot(
            session, version=payload["version"], filing_ids=ids,
            metric_code=payload.get("metric_code", ""), industry_code=payload["industry_code"],
            taxonomy_version=payload["taxonomy_version"], data_cutoff=cutoff,
            population=payload.get("population"),
        )
    except PeerGateError as exc:
        reasons.append({"code": exc.code, "detail": exc.detail})
    reasons.append({"code": "AUTHENTICATED_PEER_APPROVALS_REQUIRED", "detail": {
        "explanation": "No authenticated approval/provenance registry is configured; supplied names are claims.",
    }})
    actual_coverage = Decimal(snapshot["coverage"]) if snapshot else Decimal(0)
    assessment = {"kind": "server_assessment", "gate_version": GATE_VERSION,
                  "submitted_coverage_claim": str(coverage_claim), "rejection_reasons": reasons,
                  "snapshot": snapshot, "snapshot_hash": stable_hash(snapshot) if snapshot else None,
                  "population_claim": _jsonable(payload.get("population")),
                  "submitted_approval_claims": _jsonable(payload.get("approval_trace", []))}
    universe = PeerUniverse(
        version=payload["version"], industry_code=payload["industry_code"],
        taxonomy_version=payload["taxonomy_version"], data_cutoff=cutoff, filing_ids=ids,
        coverage=actual_coverage, status="candidate", approval_trace=[assessment],
    )
    session.add(universe)
    session.commit()
    return {"version": universe.version, "status": "candidate", "sample_count": len(ids),
            "distinct_issuer_count": snapshot["distinct_issuer_count"] if snapshot else None,
            "coverage": str(actual_coverage), "coverage_proven": False,
            "coverage_recomputed_from_population": snapshot is not None,
            "submitted_coverage_claim": str(coverage_claim), "rejection_reasons": reasons}


def _verified_approvals(records: list, snapshot_hash: str, verifier: TrustVerifier | None) -> None:
    if verifier is None:
        raise PeerGateError("AUTHENTICATED_PEER_APPROVALS_REQUIRED")
    people, names, roles = set(), set(), set()
    for record in records:
        if not isinstance(record, dict) or record.get("kind") != "approval":
            continue
        name = record.get("real_name", "")
        identity = record.get("reviewer_id", "")
        if (not isinstance(name, str) or not name.strip() or not isinstance(identity, str)
                or not identity.strip() or record.get("snapshot_hash") != snapshot_hash
                or record.get("decision") != "approved" or record.get("role") not in
                {"maintainer", "independent_reviewer"}):
            raise PeerGateError("INVALID_PEER_APPROVAL")
        try:
            datetime.fromisoformat(record["approved_at"])
        except (TypeError, ValueError, KeyError) as exc:
            raise PeerGateError("INVALID_PEER_APPROVAL_TIME") from exc
        if not verifier("approval", record):
            raise PeerGateError("UNTRUSTED_PEER_APPROVAL")
        people.add(identity.strip())
        names.add(name.strip().casefold())
        roles.add(record["role"])
    if len(people) < 2 or len(names) < 2 or roles != {"maintainer", "independent_reviewer"}:
        raise PeerGateError("TWO_DISTINCT_PEER_APPROVERS_REQUIRED")


def run_peer_comparison(
    session: Session, payload: dict, *, trust_verifier: TrustVerifier | None = None,
) -> dict:
    """Always persist refusal, and freeze actual inputs before any statistics."""
    frozen_input = {"gate_version": GATE_VERSION, "request": _jsonable(dict(payload))}
    version = str(payload.get("peer_universe_version", ""))
    metric_code = str(payload.get("metric_code", ""))
    try:
        _decimal_coverage(payload.get("coverage"))  # Compatibility claim; never the denominator.
        ids = _ids(payload.get("peer_filing_ids"))
        universe = session.scalar(select(PeerUniverse).where(PeerUniverse.version == version))
        if universe is None or universe.status != "active":
            raise PeerGateError("INSUFFICIENT_FROZEN_PEERS")
        if ids != sorted(universe.filing_ids):
            raise PeerGateError("FROZEN_PEER_MEMBERSHIP_MISMATCH")
        records = universe.approval_trace
        if not isinstance(records, list):
            raise PeerGateError("FROZEN_PEER_SNAPSHOT_REQUIRED")
        snapshots = [item for item in records if isinstance(item, dict) and item.get("kind") == "freeze"]
        if len(snapshots) != 1:
            raise PeerGateError("FROZEN_PEER_SNAPSHOT_REQUIRED")
        freeze = snapshots[0]
        original = freeze.get("snapshot")
        if not isinstance(original, dict) or stable_hash(original) != freeze.get("snapshot_hash"):
            raise PeerGateError("FROZEN_PEER_SNAPSHOT_HASH_MISMATCH")
        population = freeze.get("population_provenance")
        snapshot = build_peer_snapshot(
            session, version=version, filing_ids=ids, metric_code=metric_code,
            industry_code=universe.industry_code, taxonomy_version=universe.taxonomy_version,
            data_cutoff=universe.data_cutoff, population=population,
        )
        frozen_input["snapshot"] = snapshot
        frozen_input["snapshot_hash"] = stable_hash(snapshot)
        if snapshot != original:
            raise PeerGateError("FROZEN_PEER_INPUTS_CHANGED")
        # Numeric DB column is a display summary, not authoritative rounded coverage.
        if abs(_decimal_coverage(universe.coverage) - Decimal(snapshot["coverage"])) > Decimal("0.00005"):
            raise PeerGateError("FROZEN_PEER_COVERAGE_MISMATCH")
        target = session.get(Company, payload.get("company_id"))
        if (target is None or target.industry_code != universe.industry_code
                or target.industry_taxonomy_version != universe.taxonomy_version):
            raise PeerGateError("TARGET_COMPANY_NOT_COMPARABLE")
        _verified_approvals(records, stable_hash(snapshot), trust_verifier)
        if not trust_verifier("population", population):
            raise PeerGateError("UNTRUSTED_PEER_POPULATION_PROVENANCE")
        values = [Decimal(item["value"]) for item in snapshot["members"]]
        median = _median(values)
        with localcontext() as context:
            context.prec = 64
            mad = _median([abs(value - median) for value in values])
        output = {"sample_count": len(values), "coverage": snapshot["coverage"],
                  "population_issuer_count": len(snapshot["population"]["companies"]),
                  "median": str(median), "mad": str(mad), "frozen_input": frozen_input}
        status, failure, detail = "completed", None, {}
    except PeerGateError as exc:
        status, failure, detail = "not_comparable", exc.code, exc.detail
        output = {"frozen_input": frozen_input, "failure_detail": detail}
    run = ComparisonRun(peer_universe_version=version, metric_code=metric_code,
                        input_snapshot_hash=stable_hash(frozen_input), status=status,
                        output=output, failure_reason=failure)
    session.add(run)
    session.commit()
    response = {"status": status, "comparison_run_id": run.id, "peer_universe_version": version,
                "input_snapshot_hash": run.input_snapshot_hash}
    if failure:
        response.update(explanation_code=failure, failure_detail=detail)
    else:
        response.update({key: value for key, value in output.items() if key != "frozen_input"})
    return response

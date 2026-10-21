"""Source-bound v3 evaluation of frozen predictions, without model calls.

The v2 comparison remains unchanged.  V3 additionally binds every direct and
rule evidence link to an authorized, frozen source.  This proves citation
identity and integrity, not that an authentic quotation entails a claim.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_adapters.providers.analysis_evaluation import (
    AnalysisEvaluationCaseV2,
    AnalysisEvaluationDatasetV2,
    FrozenObligationPredictionV2,
    FrozenPredictionSetV2,
    GroundedSemanticClaim,
    Identifier,
    ObligationLabelV2,
    SemanticEvidenceLink,
    SemanticRuleApplication,
    Sha256,
    evaluate_grounded_analysis,
)


class FrozenEvaluationSource(BaseModel):
    """Exact source text and its declared physical-page/document identity."""

    model_config = ConfigDict(extra="forbid")
    source_id: Identifier
    document_sha256: Sha256
    text_sha256: Sha256
    text: str = Field(min_length=1)
    physical_page_no: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def valid_text_hash(self):
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.text_sha256:
            raise ValueError("EVALUATION_FROZEN_SOURCE_HASH_MISMATCH")
        return self


class SourceBoundEvidenceLink(SemanticEvidenceLink):
    start: int = Field(ge=0, strict=True)
    end: int = Field(gt=0, strict=True)


class SourceBoundRuleApplication(SemanticRuleApplication):
    evidence_links: list[SourceBoundEvidenceLink] = Field(default_factory=list)


class SourceBoundSemanticClaim(GroundedSemanticClaim):
    evidence_links: list[SourceBoundEvidenceLink] = Field(default_factory=list)
    rule_applications: list[SourceBoundRuleApplication] = Field(default_factory=list)


class SourceBoundObligationLabel(ObligationLabelV2):
    semantic_claims: list[SourceBoundSemanticClaim] = Field(min_length=1)


class SourceBoundObligationPrediction(FrozenObligationPredictionV2):
    semantic_claims: list[SourceBoundSemanticClaim] = Field(min_length=1)


def _claim_links(claim: GroundedSemanticClaim):
    for index, link in enumerate(claim.evidence_links):
        yield f"evidence_links/{index}", link
    for rule_index, rule in enumerate(claim.rule_applications):
        for index, link in enumerate(rule.evidence_links):
            yield f"rule_applications/{rule_index}/evidence_links/{index}", link


def _link_errors(
    link: SemanticEvidenceLink,
    sources: dict[str, FrozenEvaluationSource],
    authorized: set[str],
    declared: set[str],
) -> list[str]:
    errors = []
    source = sources.get(link.source_id)
    if link.source_id not in authorized:
        errors.append("EVALUATION_SOURCE_NOT_AUTHORIZED")
    if link.source_id not in declared:
        errors.append("EVALUATION_SOURCE_NOT_DECLARED")
    if source is None:
        return [*errors, "EVALUATION_SOURCE_UNKNOWN"]
    if link.text_sha256 != source.text_sha256:
        errors.append("EVALUATION_EVIDENCE_HASH_MISMATCH")
    if not 0 <= link.start < link.end <= len(source.text):
        errors.append("EVALUATION_EVIDENCE_SPAN_OUT_OF_BOUNDS")
    elif source.text[link.start:link.end] != link.quote:
        errors.append("EVALUATION_EVIDENCE_QUOTE_MISMATCH")
    return errors


def _binding_errors(
    claims: list[GroundedSemanticClaim],
    source_ids: list[str],
    sources: dict[str, FrozenEvaluationSource],
    authorized: set[str],
) -> list[dict]:
    errors = []
    declared = set(source_ids)
    for source_id in sorted(declared):
        for code, invalid in (
            ("EVALUATION_SOURCE_UNKNOWN", source_id not in sources),
            ("EVALUATION_SOURCE_NOT_AUTHORIZED", source_id not in authorized),
        ):
            if invalid:
                errors.append({"location": "source_ids", "source_id": source_id, "code": code})
    for claim in claims:
        for location, link in _claim_links(claim):
            for code in _link_errors(link, sources, authorized, declared):
                errors.append({
                    "claim_id": claim.claim_id, "location": location,
                    "source_id": link.source_id, "code": code,
                })
    return errors


class SourceBoundEvaluationCase(AnalysisEvaluationCaseV2):
    """One document per case; authorization is distinct from source presence."""

    authorized_source_ids: list[Identifier]
    frozen_sources: list[FrozenEvaluationSource]
    expected: SourceBoundObligationLabel

    @model_validator(mode="after")
    def valid_source_binding(self):
        sources = {source.source_id: source for source in self.frozen_sources}
        if len(sources) != len(self.frozen_sources):
            raise ValueError("EVALUATION_DUPLICATE_FROZEN_SOURCE")
        authorized = set(self.authorized_source_ids)
        if len(authorized) != len(self.authorized_source_ids):
            raise ValueError("EVALUATION_DUPLICATE_AUTHORIZED_SOURCE")
        if not authorized.issubset(sources):
            raise ValueError("EVALUATION_AUTHORIZED_SOURCE_UNKNOWN")
        if any(source.document_sha256 != self.document_sha256 for source in sources.values()):
            raise ValueError("EVALUATION_SOURCE_DOCUMENT_MISMATCH")
        errors = _binding_errors(
            self.expected.semantic_claims, self.expected.source_ids, sources, authorized,
        )
        if errors:
            raise ValueError("EVALUATION_LABEL_SOURCE_BINDING_INVALID:" + errors[0]["code"])
        return self


class SourceBoundEvaluationDataset(AnalysisEvaluationDatasetV2):
    schema_version: Literal["finreport-analysis-evaluation-v3"]
    cases: list[SourceBoundEvaluationCase] = Field(min_length=1)


class SourceBoundPredictionSet(FrozenPredictionSetV2):
    schema_version: Literal["finreport-frozen-predictions-v3"]
    predictions: list[SourceBoundObligationPrediction]


def _strict_metrics(rows: list[dict], reference_metrics: dict) -> dict:
    count = len(rows)
    admitted = sum(row["predicted_admitted"] for row in rows)
    positives = sum(row["expected_admitted"] for row in rows)
    false_admissions = sum(row["false_admission"] for row in rows)
    checked = sum(row["source_binding_checked"] for row in rows)
    valid = sum(row["source_binding_valid"] for row in rows)
    return {
        **reference_metrics,
        "fully_correct_rate": sum(row["fully_correct"] for row in rows) / count,
        "false_admissions": false_admissions,
        "false_admission_rate": false_admissions / admitted if admitted else None,
        "supported_admission_coverage": sum(
            row["fully_correct"] and row["expected_admitted"] for row in rows
        ) / positives if positives else None,
        "source_binding_checked_predictions": checked,
        "source_binding_valid_predictions": valid,
        "source_binding_valid_rate": valid / checked if checked else None,
        "source_binding_error_count": sum(row["source_binding_error_count"] for row in rows),
        "source_binding_error_predictions": sum(
            bool(row["source_binding_errors"]) for row in rows
        ),
        "source_binding_missing_predictions": count - checked,
    }


def evaluate_source_bound_analysis(
    dataset: SourceBoundEvaluationDataset,
    predictions: SourceBoundPredictionSet,
    *,
    split: Literal["dev", "test"] = "test",
    dataset_file_sha256: str | None = None,
    predictions_file_sha256: str | None = None,
) -> dict:
    """Compare v2 meaning fields and independently verify citation bindings.

    Invalid labels fail schema construction before evaluating predictions.
    Invalid prediction references remain inspectable errors and cannot pass
    the v3 strict result.  Missing predictions are not silently converted to
    insufficiency conclusions.  No source file is opened by this evaluator.
    """

    # Pydantic instances are mutable by default.  Do not trust construction-
    # time checks after a caller may have changed nested source or label data.
    dataset = SourceBoundEvaluationDataset.model_validate(dataset.model_dump())
    predictions = SourceBoundPredictionSet.model_validate(predictions.model_dump())
    projected_cases = [AnalysisEvaluationCaseV2.model_validate(case.model_dump(
        exclude={"authorized_source_ids", "frozen_sources"},
    )) for case in dataset.cases]
    reference = evaluate_grounded_analysis(
        AnalysisEvaluationDatasetV2(
            schema_version="finreport-analysis-evaluation-v2",
            dataset_id=dataset.dataset_id, cases=projected_cases,
        ),
        FrozenPredictionSetV2(
            schema_version="finreport-frozen-predictions-v2",
            predictor_id=predictions.predictor_id, predictions=predictions.predictions,
        ),
        split=split,
    )
    cases = {case.case_id: case for case in dataset.cases}
    prediction_map = {prediction.case_id: prediction for prediction in predictions.predictions}
    rows = []
    for reference_row in reference["cases"]:
        case = cases[reference_row["case_id"]]
        prediction: FrozenObligationPredictionV2 | None = prediction_map.get(case.case_id)
        errors = [] if prediction is None else _binding_errors(
            prediction.semantic_claims, prediction.source_ids,
            {source.source_id: source for source in case.frozen_sources},
            set(case.authorized_source_ids),
        )
        valid = prediction is not None and not errors
        correct = reference_row["fully_correct"] and valid
        rows.append({
            **reference_row,
            "reference_v2_fully_correct": reference_row["fully_correct"],
            "source_binding_checked": prediction is not None,
            "source_binding_valid": valid,
            "source_binding_error_count": len(errors),
            "source_binding_errors": errors,
            "fully_correct": correct,
            "false_admission": bool(reference_row["predicted_admitted"] and not correct),
        })
    result = {
        **reference,
        "schema_version": "finreport-analysis-evaluation-result-v3",
        "mode": "offline_frozen_source_bound_predictions",
        "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "reference_v2_evaluator_sha256": reference["evaluator_sha256"],
        "dspy_reference_score_percent": reference["dspy_strict_score_percent"],
        "strict_score_percent": 100 * sum(row["fully_correct"] for row in rows) / len(rows),
        "inputs": {
            "dataset_canonical_sha256": hashlib.sha256(json.dumps(
                dataset.model_dump(mode="json"), sort_keys=True,
                ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")).hexdigest(),
            "predictions_canonical_sha256": hashlib.sha256(json.dumps(
                predictions.model_dump(mode="json"), sort_keys=True,
                ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")).hexdigest(),
            "dataset_file_sha256": dataset_file_sha256,
            "predictions_file_sha256": predictions_file_sha256,
        },
        "configuration": {
            **reference["configuration"],
            "metric": "v2_reference_agreement_and_verified_frozen_source_binding_v3",
            "source_binding": "authorized_source_document_text_hash_exact_quote_span",
            "strict_score": "v2_fully_correct_and_all_prediction_source_bindings_valid",
            "document_policy": "one_document_sha256_per_case",
        },
        "source_binding": {
            "labels_verified": True,
            "invalid_label_policy": "reject_dataset_before_scoring",
            "prediction_error_policy": "record_each_error_and_fail_case_strict_result",
            "prediction_error_fields": ["claim_id", "location", "source_id", "code"],
            "source_text_in_errors": False,
            "checked": ["document_membership", "authorization", "text_sha256",
                        "span_bounds", "exact_quote", "direct_and_rule_evidence"],
            "not_proven": ["pdf_file_authenticity", "physical_page_authenticity",
                           "quote_entails_claim", "rule_validity", "financial_truth"],
        },
        "metrics": _strict_metrics(rows, reference["metrics"]),
        "by_obligation": {
            obligation: _strict_metrics(
                [row for row in rows if row["obligation_id"] == obligation], metrics,
            ) for obligation, metrics in reference["by_obligation"].items()
        },
        "cases": rows,
        "limitations": [
            *reference["limitations"],
            "V3 verifies frozen source integrity and declared document membership, not PDF bytes.",
            "Physical page numbers are declared metadata; no PDF is opened to authenticate them.",
            "A matching authentic quotation does not establish entailment, causal support or rule validity.",
            "Prose still uses v2 strict reference matching; valid unregistered paraphrases may fail.",
            "Semantic-only metrics retain v2 meaning; v3 strict score additionally requires source binding.",
        ],
    }
    result.pop("dspy_strict_score_percent")
    return result

"""Offline DSPy evaluation of explicitly labelled, frozen obligation predictions.

This module never constructs an LM or trains a program. Labels are supplied
separately from predictions; a regression reference is not an independent gold
standard. Identifiers and hashes, rather than source text, appear in reports.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$")]
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Split = Literal["train", "dev", "test"]
Assessment = Literal[
    "signal_found", "not_identified_in_checked_scope", "insufficient_evidence",
    "not_applicable", "observation",
]
ClaimKind = Literal["fact", "inference", "limitation", "hypothesis"]
ClaimPolarity = Literal["asserted", "negated", "uncertain"]
EvidenceRelation = Literal["supports", "refutes", "context"]
GroundingStatus = Literal["supported", "refuted", "context_only", "unsupported"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NumericObservation(_StrictModel):
    value: Decimal = Field(allow_inf_nan=False)
    unit: Identifier
    period: Identifier
    basis: Identifier


class NumericExpectation(NumericObservation):
    absolute_tolerance: Decimal = Field(default=Decimal(0), ge=0, allow_inf_nan=False)
    relative_tolerance: Decimal = Field(default=Decimal(0), ge=0, allow_inf_nan=False)


class ObligationLabel(_StrictModel):
    assessment: Assessment
    conclusion_code: Identifier
    admitted: bool = Field(strict=True)
    source_ids: list[Identifier]
    numbers: dict[Identifier, NumericExpectation]

    @model_validator(mode="after")
    def unique_sources(self):
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("EVALUATION_DUPLICATE_SOURCE")
        return self


class AnalysisEvaluationCase(_StrictModel):
    case_id: Identifier
    obligation_id: Identifier
    document_sha256: Sha256
    input_sha256: Sha256
    split: Split
    label_basis: Literal["synthetic", "regression_reference", "independent_gold"]
    label_artifact_sha256: Sha256
    expected: ObligationLabel


class AnalysisEvaluationDataset(_StrictModel):
    schema_version: Literal["finreport-analysis-evaluation-v1"]
    dataset_id: Identifier
    cases: list[AnalysisEvaluationCase] = Field(min_length=1)

    @model_validator(mode="after")
    def disjoint_splits(self):
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("EVALUATION_DUPLICATE_CASE")
        documents: dict[str, str] = {}
        inputs: dict[str, str] = {}
        for case in self.cases:
            for identities, identity in (
                (documents, case.document_sha256), (inputs, case.input_sha256),
            ):
                previous = identities.setdefault(identity, case.split)
                if previous != case.split:
                    raise ValueError("EVALUATION_TRAIN_TEST_LEAKAGE")
        return self


class FrozenObligationPrediction(_StrictModel):
    case_id: Identifier
    input_sha256: Sha256
    assessment: Assessment
    conclusion_code: Identifier
    admitted: bool = Field(strict=True)
    source_ids: list[Identifier]
    numbers: dict[Identifier, NumericObservation]

    @model_validator(mode="after")
    def unique_sources(self):
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("EVALUATION_DUPLICATE_SOURCE")
        return self


class FrozenPredictionSet(_StrictModel):
    schema_version: Literal["finreport-frozen-predictions-v1"]
    predictor_id: Identifier
    predictions: list[FrozenObligationPrediction]

    @model_validator(mode="after")
    def unique_cases(self):
        if len({item.case_id for item in self.predictions}) != len(self.predictions):
            raise ValueError("EVALUATION_DUPLICATE_PREDICTION")
        return self


class SemanticRole(_StrictModel):
    """A typed role in a financial claim, kept separate from free text.

    ``type`` is an application-controlled identifier (for example
    ``entity.non_financial`` or ``asset.financial``).  The evaluator compares
    this role structure rather than trusting a model's prose rationale.
    """

    mention: str = Field(min_length=1, max_length=500)
    type: Identifier


class SemanticEvidenceLink(_StrictModel):
    """A source span attached to one claim.

    A source id alone is not a semantic citation: the hash, span and quote are
    retained so a later checker can verify that the cited text supports the
    claim.  Synthetic fixtures may use synthetic hashes, but must still carry
    the same fields.
    """

    source_id: Identifier
    text_sha256: Sha256
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1, max_length=4000)
    relation: EvidenceRelation

    @model_validator(mode="after")
    def valid_span(self):
        if self.end <= self.start:
            raise ValueError("EVALUATION_INVALID_EVIDENCE_SPAN")
        return self


class SemanticRuleApplication(_StrictModel):
    """A deterministic rule/derivation used by an inference claim."""

    rule_id: Identifier
    rule_version: Identifier
    premise_claim_ids: list[Identifier] = Field(default_factory=list)
    evidence_links: list[SemanticEvidenceLink] = Field(default_factory=list)
    result: GroundingStatus

    @model_validator(mode="after")
    def require_grounding(self):
        if self.result in {"supported", "refuted"} and not self.evidence_links and not self.premise_claim_ids:
            raise ValueError("EVALUATION_UNGROUNDED_RULE_APPLICATION")
        return self


class GroundedSemanticClaim(_StrictModel):
    """Versioned structured meaning for one generated financial statement.

    ``claim_text`` and ``basis_summary`` are preserved for inspection but are
    deliberately not used as the sole equality key.  A semantically wrong
    rewrite can quote the right source and use the right conclusion code; the
    typed subject/predicate/object, polarity, scope and evidence relation are
    what make that error observable.
    """

    claim_id: Identifier
    claim_text: str = Field(min_length=1, max_length=8000)
    basis_summary: str = Field(min_length=1, max_length=4000)
    claim_kind: ClaimKind
    subject: SemanticRole
    predicate: Identifier
    object: SemanticRole | None = None
    polarity: ClaimPolarity
    condition: str | None = Field(default=None, max_length=1000)
    period: Identifier | None = None
    scope: Identifier | None = None
    evidence_links: list[SemanticEvidenceLink] = Field(default_factory=list)
    rule_applications: list[SemanticRuleApplication] = Field(default_factory=list)
    grounding: GroundingStatus
    requires_evidence: bool = Field(default=True, strict=True)

    @model_validator(mode="after")
    def validate_grounding(self):
        if self.claim_kind in {"fact", "inference"} and not self.requires_evidence:
            raise ValueError("EVALUATION_FACT_CANNOT_WAIVE_EVIDENCE")
        if self.requires_evidence and self.grounding in {"supported", "refuted"}:
            has_direct_evidence = any(
                link.relation in {"supports", "refutes"} for link in self.evidence_links
            )
            has_rule_evidence = any(
                rule.result in {"supported", "refuted"}
                and (rule.evidence_links or rule.premise_claim_ids)
                for rule in self.rule_applications
            )
            if not (has_direct_evidence or has_rule_evidence):
                raise ValueError("EVALUATION_UNGROUNDED_CLAIM")
        if self.grounding == "unsupported" and (self.evidence_links or self.rule_applications):
            raise ValueError("EVALUATION_UNSUPPORTED_CLAIM_HAS_GROUNDING")
        return self


class ObligationLabelV2(ObligationLabel):
    semantic_claims: list[GroundedSemanticClaim] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_claims(self):
        ids = [claim.claim_id for claim in self.semantic_claims]
        if len(set(ids)) != len(ids):
            raise ValueError("EVALUATION_DUPLICATE_SEMANTIC_CLAIM")
        claim_ids = set(ids)
        for claim in self.semantic_claims:
            for rule in claim.rule_applications:
                if not set(rule.premise_claim_ids).issubset(claim_ids):
                    raise ValueError("EVALUATION_UNKNOWN_PREMISE_CLAIM")
        return self


class AnalysisEvaluationCaseV2(_StrictModel):
    case_id: Identifier
    obligation_id: Identifier
    document_sha256: Sha256
    input_sha256: Sha256
    split: Split
    label_basis: Literal["synthetic", "regression_reference", "independent_gold"]
    label_artifact_sha256: Sha256
    expected: ObligationLabelV2


class AnalysisEvaluationDatasetV2(_StrictModel):
    schema_version: Literal["finreport-analysis-evaluation-v2"]
    dataset_id: Identifier
    cases: list[AnalysisEvaluationCaseV2] = Field(min_length=1)

    @model_validator(mode="after")
    def disjoint_splits(self):
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("EVALUATION_DUPLICATE_CASE")
        identities: dict[str, str] = {}
        for case in self.cases:
            for identity in (case.document_sha256, case.input_sha256):
                previous = identities.setdefault(identity, case.split)
                if previous != case.split:
                    raise ValueError("EVALUATION_TRAIN_TEST_LEAKAGE")
        return self


class FrozenObligationPredictionV2(_StrictModel):
    case_id: Identifier
    input_sha256: Sha256
    assessment: Assessment
    conclusion_code: Identifier
    admitted: bool = Field(strict=True)
    source_ids: list[Identifier]
    numbers: dict[Identifier, NumericObservation]
    semantic_claims: list[GroundedSemanticClaim] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_claims(self):
        ids = [claim.claim_id for claim in self.semantic_claims]
        if len(set(ids)) != len(ids):
            raise ValueError("EVALUATION_DUPLICATE_SEMANTIC_CLAIM")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("EVALUATION_DUPLICATE_SOURCE")
        claim_ids = set(ids)
        for claim in self.semantic_claims:
            for rule in claim.rule_applications:
                if not set(rule.premise_claim_ids).issubset(claim_ids):
                    raise ValueError("EVALUATION_UNKNOWN_PREMISE_CLAIM")
        return self


class FrozenPredictionSetV2(_StrictModel):
    schema_version: Literal["finreport-frozen-predictions-v2"]
    predictor_id: Identifier
    predictions: list[FrozenObligationPredictionV2]

    @model_validator(mode="after")
    def unique_cases(self):
        if len({item.case_id for item in self.predictions}) != len(self.predictions):
            raise ValueError("EVALUATION_DUPLICATE_PREDICTION")
        return self


def _hash_json(value: dict) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _compare(case: AnalysisEvaluationCase, prediction: FrozenObligationPrediction | None) -> dict:
    expected = case.expected
    numbers: dict[str, bool] = {}
    if prediction is not None:
        for name, label in expected.numbers.items():
            value = prediction.numbers.get(name)
            tolerance = max(
                label.absolute_tolerance, abs(label.value) * label.relative_tolerance,
            )
            numbers[name] = bool(
                value is not None
                and (value.unit, value.period, value.basis) == (label.unit, label.period, label.basis)
                and abs(value.value - label.value) <= tolerance
            )
    else:
        numbers = {name: False for name in expected.numbers}
    conclusion_matches = bool(
        prediction is not None and prediction.assessment == expected.assessment
        and prediction.conclusion_code == expected.conclusion_code
    )
    number_matches = bool(
        prediction is not None and set(prediction.numbers) == set(expected.numbers)
        and all(numbers.values())
    )
    expected_sources = set(expected.source_ids)
    actual_sources = set(prediction.source_ids) if prediction is not None else set()
    sources_match = prediction is not None and actual_sources == expected_sources
    admission_matches = prediction is not None and prediction.admitted == expected.admitted
    fully_correct = conclusion_matches and number_matches and sources_match and admission_matches
    predicted_admitted = prediction is not None and prediction.admitted
    return {
        "case_id": case.case_id,
        "obligation_id": case.obligation_id,
        "label_basis": case.label_basis,
        "prediction_present": prediction is not None,
        "expected_admitted": expected.admitted,
        "predicted_admitted": predicted_admitted,
        "conclusion_matches": conclusion_matches,
        "numbers_match": number_matches,
        "numeric_checks": numbers,
        "sources_match": sources_match,
        "source_intersection": len(expected_sources & actual_sources),
        "expected_source_count": len(expected_sources),
        "predicted_source_count": len(actual_sources),
        "admission_matches": admission_matches,
        "false_admission": predicted_admitted and not fully_correct,
        "false_positive_admission": predicted_admitted and not expected.admitted,
        "false_rejection": expected.admitted and not predicted_admitted,
        "fully_correct": fully_correct,
    }


def _aggregate(rows: list[dict]) -> dict:
    count = len(rows)
    present = sum(row["prediction_present"] for row in rows)
    admitted = sum(row["predicted_admitted"] for row in rows)
    positive = sum(row["expected_admitted"] for row in rows)
    source_intersection = sum(row["source_intersection"] for row in rows)
    expected_sources = sum(row["expected_source_count"] for row in rows)
    predicted_sources = sum(row["predicted_source_count"] for row in rows)
    false_admissions = sum(row["false_admission"] for row in rows)
    false_positives = sum(row["false_positive_admission"] for row in rows)
    numeric_count = sum(len(row["numeric_checks"]) for row in rows)
    numeric_correct = sum(sum(row["numeric_checks"].values()) for row in rows)
    return {
        "case_count": count,
        "predictions_present": present,
        "disposition_coverage": _ratio(present, count),
        "fully_correct_rate": _ratio(sum(row["fully_correct"] for row in rows), count),
        "conclusion_match_rate": _ratio(sum(row["conclusion_matches"] for row in rows), count),
        "numeric_count": numeric_count,
        "numeric_match_rate": _ratio(numeric_correct, numeric_count),
        "source_precision": _ratio(source_intersection, predicted_sources),
        "source_recall": _ratio(source_intersection, expected_sources),
        "predicted_admissions": admitted,
        "expected_admissions": positive,
        "false_admissions": false_admissions,
        "false_admission_rate": _ratio(false_admissions, admitted),
        "false_positive_admissions": false_positives,
        "false_positive_rate": _ratio(false_positives, count - positive),
        "false_rejections": sum(row["false_rejection"] for row in rows),
        "supported_admission_coverage": _ratio(sum(
            row["fully_correct"] and row["expected_admitted"] for row in rows
        ), positive),
    }


def _text_key(value: str | None) -> str | None:
    """Normalize only layout whitespace; do not perform synonym guessing."""

    if value is None:
        return None
    return " ".join(value.split())


def _semantic_key(claim: GroundedSemanticClaim) -> tuple:
    """Return the meaning-bearing fields used for claim comparison.

    Free-form ``claim_text`` and ``basis_summary`` are intentionally excluded:
    matching prose is not evidence of matching meaning, and differing prose is
    not by itself a semantic error.  Canonical role types, predicate, polarity,
    condition, period and scope must agree.
    """

    return (
        claim.claim_kind,
        (claim.subject.type, _text_key(claim.subject.mention)),
        claim.predicate,
        None if claim.object is None else (claim.object.type, _text_key(claim.object.mention)),
        claim.polarity,
        _text_key(claim.condition),
        claim.period,
        claim.scope,
        claim.requires_evidence,
    )


def _evidence_key(link: SemanticEvidenceLink) -> tuple:
    """Use immutable source/span identity, while retaining quote for review."""

    return (
        link.source_id, link.text_sha256, link.start, link.end,
        hashlib.sha256(link.quote.encode("utf-8")).hexdigest(), link.relation,
    )


def _rule_key(rule: SemanticRuleApplication) -> tuple:
    return (
        rule.rule_id,
        rule.rule_version,
        tuple(sorted(rule.premise_claim_ids)),
        tuple(sorted(_evidence_key(link) for link in rule.evidence_links)),
        rule.result,
    )


def _claim_is_unsupported(claim: GroundedSemanticClaim) -> bool:
    if not claim.requires_evidence:
        return False
    if claim.grounding == "unsupported":
        return True
    direct = any(link.relation in {"supports", "refutes"} for link in claim.evidence_links)
    derived = any(
        rule.result in {"supported", "refuted"}
        and (rule.evidence_links or rule.premise_claim_ids)
        for rule in claim.rule_applications
    )
    return not (direct or derived)


def _compare_grounded(
    case: AnalysisEvaluationCaseV2,
    prediction: FrozenObligationPredictionV2 | None,
) -> dict:
    """Compare v1 fields plus structured claim/evidence semantics."""

    base = _compare(case, prediction)
    expected_claims = {claim.claim_id: claim for claim in case.expected.semantic_claims}
    actual_claims = {
        claim.claim_id: claim for claim in prediction.semantic_claims
    } if prediction is not None else {}
    claim_rows: list[dict] = []
    expected_evidence: set[tuple] = set()
    actual_evidence: set[tuple] = set()
    expected_rules: set[tuple] = set()
    actual_rules: set[tuple] = set()
    for claim in expected_claims.values():
        expected_evidence.update(_evidence_key(link) for link in claim.evidence_links)
        expected_rules.update(_rule_key(rule) for rule in claim.rule_applications)
    for claim in actual_claims.values():
        actual_evidence.update(_evidence_key(link) for link in claim.evidence_links)
        actual_rules.update(_rule_key(rule) for rule in claim.rule_applications)
    for claim_id, expected_claim in expected_claims.items():
        actual_claim = actual_claims.get(claim_id)
        structure_match = bool(actual_claim is not None and _semantic_key(actual_claim) == _semantic_key(expected_claim))
        expected_links = {_evidence_key(link) for link in expected_claim.evidence_links}
        actual_links = {_evidence_key(link) for link in actual_claim.evidence_links} if actual_claim else set()
        expected_claim_rules = {_rule_key(rule) for rule in expected_claim.rule_applications}
        actual_claim_rules = {_rule_key(rule) for rule in actual_claim.rule_applications} if actual_claim else set()
        claim_rows.append({
            "claim_id": claim_id,
            "prediction_present": actual_claim is not None,
            "structure_matches": structure_match,
            "claim_text_matches": bool(actual_claim is not None
                                       and _text_key(actual_claim.claim_text) == _text_key(expected_claim.claim_text)),
            "basis_summary_matches": bool(actual_claim is not None
                                          and _text_key(actual_claim.basis_summary) == _text_key(expected_claim.basis_summary)),
            "grounding_matches": bool(actual_claim is not None and actual_claim.grounding == expected_claim.grounding),
            "evidence_span_matches": actual_links == expected_links,
            "rule_applications_match": actual_claim_rules == expected_claim_rules,
            "expected_grounding": expected_claim.grounding,
            "predicted_grounding": actual_claim.grounding if actual_claim else None,
            "predicted_unsupported": bool(actual_claim is not None and _claim_is_unsupported(actual_claim)),
        })
    expected_count = len(expected_claims)
    matched_claim_count = sum(row["structure_matches"] for row in claim_rows)
    claim_presence_count = sum(row["prediction_present"] for row in claim_rows)
    structure_ok = expected_count > 0 and matched_claim_count == expected_count and len(actual_claims) == expected_count
    evidence_precision = _ratio(len(expected_evidence & actual_evidence), len(actual_evidence))
    evidence_recall = _ratio(len(expected_evidence & actual_evidence), len(expected_evidence))
    rule_precision = _ratio(len(expected_rules & actual_rules), len(actual_rules))
    rule_recall = _ratio(len(expected_rules & actual_rules), len(expected_rules))
    unsupported_count = sum(_claim_is_unsupported(claim) for claim in actual_claims.values())
    semantic_match = bool(
        structure_ok
        and all(row["grounding_matches"] and row["evidence_span_matches"] and row["rule_applications_match"]
                and row["claim_text_matches"] and row["basis_summary_matches"]
                for row in claim_rows)
        and unsupported_count == 0
    )
    base["base_fully_correct"] = base["fully_correct"]
    base["semantic_claims"] = claim_rows
    base["semantic_fully_correct"] = semantic_match
    base["fully_correct"] = bool(base["fully_correct"] and semantic_match)
    base["semantic_claim_count"] = expected_count
    base["predicted_semantic_claim_count"] = len(actual_claims)
    base["semantic_claim_presence"] = _ratio(claim_presence_count, expected_count)
    base["semantic_structure_match"] = _ratio(matched_claim_count, expected_count)
    base["semantic_grounding_match"] = _ratio(
        sum(row["grounding_matches"] for row in claim_rows), expected_count,
    )
    base["semantic_claim_text_match"] = _ratio(sum(row["claim_text_matches"] for row in claim_rows), expected_count)
    base["semantic_basis_summary_match"] = _ratio(sum(row["basis_summary_matches"] for row in claim_rows), expected_count)
    base["evidence_span_precision"] = evidence_precision
    base["evidence_span_recall"] = evidence_recall
    base["evidence_match_count"] = len(expected_evidence & actual_evidence)
    base["expected_evidence_count"] = len(expected_evidence)
    base["predicted_evidence_count"] = len(actual_evidence)
    base["rule_application_precision"] = rule_precision
    base["rule_application_recall"] = rule_recall
    base["rule_match_count"] = len(expected_rules & actual_rules)
    base["expected_rule_count"] = len(expected_rules)
    base["predicted_rule_count"] = len(actual_rules)
    base["unsupported_claim_count"] = unsupported_count
    base["unsupported_claim_rate"] = _ratio(unsupported_count, len(actual_claims))
    base["semantic_false_admission"] = bool(base["predicted_admitted"] and not semantic_match)
    base["false_admission"] = bool(base["predicted_admitted"] and not base["fully_correct"])
    return base


def _aggregate_grounded(rows: list[dict]) -> dict:
    count = len(rows)
    def mean(name: str) -> float | None:
        values = [row[name] for row in rows if row[name] is not None]
        return sum(values) / len(values) if values else None

    return {
        **_aggregate(rows),
        "base_fully_correct_rate": _ratio(sum(row["base_fully_correct"] for row in rows), count),
        "semantic_fully_correct_rate": _ratio(sum(row["semantic_fully_correct"] for row in rows), count),
        "semantic_claim_presence": mean("semantic_claim_presence"),
        "semantic_structure_match": mean("semantic_structure_match"),
        "semantic_grounding_match": mean("semantic_grounding_match"),
        "semantic_claim_text_match": mean("semantic_claim_text_match"),
        "semantic_basis_summary_match": mean("semantic_basis_summary_match"),
        "evidence_span_precision": _ratio(
            sum(row["evidence_match_count"] for row in rows),
            sum(row["predicted_evidence_count"] for row in rows),
        ),
        "evidence_span_recall": _ratio(
            sum(row["evidence_match_count"] for row in rows),
            sum(row["expected_evidence_count"] for row in rows),
        ),
        "rule_application_precision": _ratio(
            sum(row["rule_match_count"] for row in rows),
            sum(row["predicted_rule_count"] for row in rows),
        ),
        "rule_application_recall": _ratio(
            sum(row["rule_match_count"] for row in rows),
            sum(row["expected_rule_count"] for row in rows),
        ),
        "unsupported_claims": sum(row["unsupported_claim_count"] for row in rows),
        "unsupported_claim_rate": _ratio(
            sum(row["unsupported_claim_count"] for row in rows),
            sum(row["predicted_semantic_claim_count"] for row in rows),
        ),
        "semantic_false_admissions": sum(row["semantic_false_admission"] for row in rows),
        "semantic_false_admission_rate": _ratio(
            sum(row["semantic_false_admission"] for row in rows),
            sum(row["predicted_admitted"] for row in rows),
        ),
    }


def evaluate_frozen_analysis(
    dataset: AnalysisEvaluationDataset,
    predictions: FrozenPredictionSet,
    *,
    split: Literal["dev", "test"] = "test",
    dataset_file_sha256: str | None = None,
    predictions_file_sha256: str | None = None,
) -> dict:
    """Score stored outputs with real DSPy, without generation or optimization.

    ``conclusion_code`` is an explicit labelled category. Its equality does not
    establish that arbitrary natural language conclusions are equivalent.
    Numeric comparisons require the same unit, period and accounting basis.
    A missing prediction reduces coverage; it never becomes an insufficiency
    conclusion. DSPy's score measures strict per-case agreement in percent.
    """
    if split not in {"dev", "test"}:
        raise ValueError("EVALUATION_TRAINING_DISABLED")
    all_cases = {case.case_id: case for case in dataset.cases}
    prediction_map = {prediction.case_id: prediction for prediction in predictions.predictions}
    for key, prediction in prediction_map.items():
        if key not in all_cases:
            raise ValueError("EVALUATION_UNKNOWN_PREDICTION_CASE")
        if prediction.input_sha256 != all_cases[key].input_sha256:
            raise ValueError("EVALUATION_INPUT_HASH_MISMATCH")
    cases = [case for case in dataset.cases if case.split == split]
    if not cases:
        raise ValueError("EVALUATION_EMPTY_SPLIT")

    # Lazy import keeps an evaluation-only dependency out of runtime startup.
    try:
        import dspy
    except ImportError as exc:
        raise RuntimeError("EVALUATION_DSPY_NOT_INSTALLED") from exc

    class FrozenProgram(dspy.Module):
        def forward(self, case_id: str):
            return dspy.Prediction(frozen_prediction=prediction_map.get(case_id))

    examples = [dspy.Example(case_id=case.case_id, labelled_case=case).with_inputs("case_id")
                for case in cases]

    def strict_metric(example, prediction, trace=None):
        return _compare(example.labelled_case, prediction.frozen_prediction)["fully_correct"]

    evaluator = dspy.Evaluate(
        devset=examples, metric=strict_metric, num_threads=1,
        display_progress=False, display_table=False, max_errors=1,
        provide_traceback=False,
    )
    evaluation = evaluator(FrozenProgram())
    rows = [_compare(example.labelled_case, prediction.frozen_prediction)
            for example, prediction, _ in evaluation.results]
    basis_counts = {basis: sum(case.label_basis == basis for case in cases)
                    for basis in sorted({case.label_basis for case in cases})}
    return {
        "schema_version": "finreport-analysis-evaluation-result-v1",
        "status": "evaluated",
        "mode": "offline_frozen_predictions",
        "model_called": False,
        "training_performed": False,
        "dataset_id": dataset.dataset_id,
        "predictor_id": predictions.predictor_id,
        "split": split,
        "sdk": {"name": "dspy", "version": importlib.metadata.version("dspy")},
        "python_version": platform.python_version(),
        "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "inputs": {
            "dataset_canonical_sha256": _hash_json(dataset.model_dump(mode="json")),
            "predictions_canonical_sha256": _hash_json(predictions.model_dump(mode="json")),
            "dataset_file_sha256": dataset_file_sha256,
            "predictions_file_sha256": predictions_file_sha256,
        },
        "configuration": {
            "num_threads": 1,
            "metric": "exact_category_numeric_tolerance_source_and_admission_v1",
            "source_policy": "exact_set",
            "numeric_policy": "max_absolute_or_relative_with_exact_unit_period_basis",
            "document_split_policy": "disjoint_by_document_and_input_sha256",
        },
        "label_basis_counts": basis_counts,
        "independent_gold_available": basis_counts.get("independent_gold", 0) > 0,
        "dspy_strict_score_percent": evaluation.score,
        "metrics": _aggregate(rows),
        "by_obligation": {
            obligation: _aggregate([row for row in rows if row["obligation_id"] == obligation])
            for obligation in sorted({row["obligation_id"] for row in rows})
        },
        "cases": rows,
        "limitations": [
            "Stored predictions only; this does not measure a fresh model rerun.",
            "Synthetic labels test software; regression references test saved agreement.",
            "Financial accuracy requires independent gold and representative held-out filings.",
            "Conclusion codes are labelled categories, not free-text semantic equivalence.",
            "Coverage counts explicit dispositions; insufficient evidence is not a positive fact.",
            "False-admission rate divides incorrect admissions by predicted admissions; null is undefined.",
        ],
    }


def evaluate_grounded_analysis(
    dataset: AnalysisEvaluationDatasetV2,
    predictions: FrozenPredictionSetV2,
    *,
    split: Literal["dev", "test"] = "test",
    dataset_file_sha256: str | None = None,
    predictions_file_sha256: str | None = None,
) -> dict:
    """Evaluate v2 predictions with structured claim and span checks.

    This is deliberately a separate entry point.  Existing v1 artifacts do
    not contain claim structure and are never upgraded or re-labelled as
    semantically evaluated.  DSPy is used only as the deterministic evaluator;
    no LM, optimizer or network call is created here.
    """

    if split not in {"dev", "test"}:
        raise ValueError("EVALUATION_TRAINING_DISABLED")
    all_cases = {case.case_id: case for case in dataset.cases}
    prediction_map = {prediction.case_id: prediction for prediction in predictions.predictions}
    for key, prediction in prediction_map.items():
        if key not in all_cases:
            raise ValueError("EVALUATION_UNKNOWN_PREDICTION_CASE")
        if prediction.input_sha256 != all_cases[key].input_sha256:
            raise ValueError("EVALUATION_INPUT_HASH_MISMATCH")
    cases = [case for case in dataset.cases if case.split == split]
    if not cases:
        raise ValueError("EVALUATION_EMPTY_SPLIT")
    try:
        import dspy
    except ImportError as exc:
        raise RuntimeError("EVALUATION_DSPY_NOT_INSTALLED") from exc

    class FrozenGroundedProgram(dspy.Module):
        def forward(self, case_id: str):
            return dspy.Prediction(frozen_prediction=prediction_map.get(case_id))

    examples = [dspy.Example(case_id=case.case_id, labelled_case=case).with_inputs("case_id")
                for case in cases]

    def semantic_metric(example, prediction, trace=None):
        return _compare_grounded(example.labelled_case, prediction.frozen_prediction)["fully_correct"]

    evaluator = dspy.Evaluate(
        devset=examples, metric=semantic_metric, num_threads=1,
        display_progress=False, display_table=False, max_errors=1,
        provide_traceback=False,
    )
    evaluation = evaluator(FrozenGroundedProgram())
    rows = [_compare_grounded(example.labelled_case, prediction.frozen_prediction)
            for example, prediction, _ in evaluation.results]
    basis_counts = {basis: sum(case.label_basis == basis for case in cases)
                    for basis in sorted({case.label_basis for case in cases})}
    return {
        "schema_version": "finreport-analysis-evaluation-result-v2",
        "status": "evaluated",
        "mode": "offline_frozen_grounded_predictions",
        "model_called": False,
        "training_performed": False,
        "dataset_id": dataset.dataset_id,
        "predictor_id": predictions.predictor_id,
        "split": split,
        "sdk": {"name": "dspy", "version": importlib.metadata.version("dspy")},
        "python_version": platform.python_version(),
        "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "inputs": {
            "dataset_canonical_sha256": _hash_json(dataset.model_dump(mode="json")),
            "predictions_canonical_sha256": _hash_json(predictions.model_dump(mode="json")),
            "dataset_file_sha256": dataset_file_sha256,
            "predictions_file_sha256": predictions_file_sha256,
        },
        "configuration": {
            "num_threads": 1,
            "metric": "category_numeric_source_and_structured_claim_span_v2",
            "source_policy": "exact_set",
            "semantic_policy": "exact_structured_roles_predicate_polarity_scope_and_grounding",
            "evidence_policy": "exact_source_hash_span_and_relation",
            "prose_policy": "strict_whitespace_normalized_claim_text_and_basis_summary_against_label",
            "document_split_policy": "disjoint_by_document_and_input_sha256",
        },
        "label_basis_counts": basis_counts,
        "independent_gold_available": basis_counts.get("independent_gold", 0) > 0,
        "dspy_strict_score_percent": evaluation.score,
        "metrics": _aggregate_grounded(rows),
        "by_obligation": {
            obligation: _aggregate_grounded([row for row in rows if row["obligation_id"] == obligation])
            for obligation in sorted({row["obligation_id"] for row in rows})
        },
        "cases": rows,
        "limitations": [
            "Stored predictions only; this does not measure a fresh model rerun.",
            "Structured role and evidence agreement is not a general natural-language semantic proof.",
            "Prose uses strict reference agreement; alternative valid paraphrases may be false rejections.",
            "Matching prose is not by itself a semantic proof; structured meaning and grounding must also agree.",
            "Independent gold and representative held-out filings are required for financial accuracy claims.",
            "A source/span citation must still be checked against the source text by the production gate.",
            "Regression references test saved agreement and do not establish independent correctness.",
        ],
    }


def build_grounded_analysis_signature():
    """Build the real DSPy field contract without constructing/calling an LM.

    Generation remains the caller's responsibility and is intentionally not
    started by the offline evaluation CLI.  The returned Signature makes
    evidence and rule applications explicit, but its declaration does not
    establish semantic correctness; production validation remains necessary.
    """

    try:
        import dspy
    except ImportError as exc:
        raise RuntimeError("EVALUATION_DSPY_NOT_INSTALLED") from exc

    class GroundedFinancialAnalysis(dspy.Signature):
        """Answer only from frozen sources and program facts.

        Preserve subject/object roles, negation, conditions, period and scope.
        Every factual claim needs source/hash/span grounding; every inference
        needs a registered rule and grounded premises.  Rule identifiers alone
        are not evidence.  Unknown support must remain unresolved rather than
        be rewritten as a supported financial conclusion.  basis_summary is a
        short inspectable explanation and must not expose private reasoning.
        """

        obligation: str = dspy.InputField(desc="The financial question and checked scope.")
        frozen_source_units: list[dict] = dspy.InputField(
            desc="Authorized source units, exact text/hash/span and available disclosures.",
        )
        program_facts: list[dict] = dspy.InputField(desc="Deterministic calculations with unit, period and basis.")
        rule_catalog: list[dict] = dspy.InputField(desc="Allowed rule ids, versions, preconditions and results.")
        claims: list[GroundedSemanticClaim] = dspy.OutputField(
            desc="Atomic claims with roles, polarity, scope, evidence links and short basis_summary.",
        )
        unresolved_requirements: list[str] = dspy.OutputField(
            desc="Missing source documents, unresolved support or required accounting assumptions.",
        )

    return GroundedFinancialAnalysis

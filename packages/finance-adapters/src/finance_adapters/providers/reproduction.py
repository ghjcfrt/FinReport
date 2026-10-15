from __future__ import annotations

import hashlib
import importlib.metadata
import platform
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from finance_adapters.persistence.audit import stable_hash
from finance_adapters.persistence.models import AnalysisRun

BUNDLE_VERSION = "finreport-reproduction-v1"
SOURCE_FILES = (
    "packages/finance-adapters/src/finance_adapters/providers/llm_analysis.py",
    "packages/finance-adapters/src/finance_adapters/providers/model_clients.py",
    "packages/finance-adapters/src/finance_adapters/providers/document_context.py",
    "packages/finance-adapters/src/finance_adapters/providers/reproduction.py",
    "packages/finance-adapters/src/finance_adapters/persistence/metric_gateway.py",
    "packages/finance-adapters/src/finance_adapters/persistence/audit.py",
    "packages/finance-domain/src/finance_domain/analysis/contracts.py",
    "packages/finance-domain/src/finance_domain/metrics/definitions.py",
    "packages/finance-domain/src/finance_domain/metrics/formulas.py",
    "tools/replay-analysis.py", "pyproject.toml", "uv.lock",
)
STAGES = ("structure", "reference", "numeric", "semantic")
REFERENCE_FORMULA_VERSION = "general-non-financial-v2"
REFERENCE_FORMULAS = {
    "revenue": ("revenue", ("revenue",), "currency"),
    "operating_profit": ("operating_profit", ("operating_profit",), "currency"),
    "net_income_attributable": ("net_income_attributable", ("net_income_attributable",), "currency"),
    "cashflow_operations": ("cashflow_operations", ("cashflow_operations",), "currency"),
    "free_cash_flow": ("cashflow_operations - capex_cash", ("cashflow_operations", "capex_cash"), "currency"),
    "gross_margin": ("(revenue - operating_cost) / revenue", ("revenue", "operating_cost"), "ratio"),
    "net_margin": ("net_income_attributable / revenue", ("net_income_attributable", "revenue"), "ratio"),
    "operating_cashflow_to_net_income": ("cashflow_operations / net_income", ("cashflow_operations", "net_income"), "ratio"),
}


def runtime_manifest() -> dict:
    root = Path(__file__).resolve().parents[5]
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
            text=True, check=True, timeout=5,
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"], cwd=root,
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        revision, dirty = None, None
    dependencies = {}
    for name in ("pydantic", "SQLAlchemy", "httpx", "PyMuPDF", "fastapi"):
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            dependencies[name] = None
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "python": platform.python_version(), "platform": platform.platform(),
        "git_revision": revision, "tracked_worktree_dirty": dirty,
        "dependencies": dependencies,
        "source_files_sha256": {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in SOURCE_FILES if (root / name).is_file()
        },
    }


def build_reproduction_bundle(run: AnalysisRun) -> dict:
    if run.status not in {"completed", "failed"} or run.status == "completed" and run.invalidated_at is not None:
        raise ValueError("REPRODUCTION_CURRENT_TERMINAL_ANALYSIS_REQUIRED")
    trace = run.tool_trace or {}
    if not trace.get("evidence_package") or not trace.get("attempts"):
        raise ValueError("REPRODUCTION_TRACE_INCOMPLETE")
    payload = {
        "analysis_run_id": run.id, "filing_version_id": run.filing_version_id,
        "analysis_status": run.status, "failure_reason": run.failure_reason,
        "invalidated_at": run.invalidated_at.isoformat() if run.invalidated_at else None,
        "historical": run.invalidated_at is not None,
        "input_snapshot_hash": run.input_snapshot_hash,
        "tool_trace_hash": run.tool_trace_hash, "model": run.model,
        "model_version": run.model_version, "prompt_version": run.prompt_version,
        "validator_version": run.validator_version,
        "orchestration_version": run.orchestration_version,
        "output": run.output, "tool_trace": trace,
        "export_runtime": runtime_manifest(),
        "captured_at": datetime.now(UTC).isoformat(),
        "boundaries": [
            ("Offline replay checks saved input, model response, validators and numerical JSON rendering; "
             "it does not call a model or promise identical output from a new model invocation."),
            ("Hashes detect changes relative to this bundle; they are not an independent signature "
             "and do not prevent an administrator rewriting both data and hashes."),
            ("export_runtime records export-time local files; execution_runtime, when present, "
             "records the code and dependencies at analysis start."),
            "A passed replay of a failed analysis confirms the saved failure trace, not a formal financial report.",
            "Reference formulas recompute saved normalized inputs; PDF byte identity does not prove correct extraction.",
            "Unreviewed document excerpts and supplier-exposed reasoning are not verified financial facts.",
        ],
    }
    return {"schema_version": BUNDLE_VERSION, "payload": payload, "payload_sha256": stable_hash(payload)}


def _reference_metric(metric: dict) -> Decimal:
    if metric.get("formula_version") != REFERENCE_FORMULA_VERSION:
        raise ValueError("REPLAY_REFERENCE_FORMULA_VERSION_MISMATCH")
    code = metric["code"]
    if code not in REFERENCE_FORMULAS:
        raise ValueError("REPLAY_REFERENCE_FORMULA_UNSUPPORTED")
    formula, concepts, unit = REFERENCE_FORMULAS[code]
    if metric["formula"] != formula or metric["unit"] != unit:
        raise ValueError(f"REPLAY_REFERENCE_FORMULA_CONTRACT_MISMATCH:{code}")
    if tuple(item["concept_code"] for item in metric["inputs"]) != concepts:
        raise ValueError(f"REPLAY_REFERENCE_INPUT_CONCEPTS_MISMATCH:{code}")
    values = {item["concept_code"]: Decimal(item["value"]) for item in metric["inputs"]}
    if any(not item.is_finite() for item in values.values()):
        raise ValueError("REPLAY_NON_FINITE_METRIC_INPUT")
    if code in {"revenue", "operating_profit", "net_income_attributable", "cashflow_operations"}:
        return values[code]
    if code == "free_cash_flow":
        if values["capex_cash"] < 0:
            raise ValueError("REPLAY_NEGATIVE_CAPEX_CASH")
        return values["cashflow_operations"] - values["capex_cash"]
    if code in {"gross_margin", "net_margin"}:
        denominator = values["revenue"]
        numerator = (denominator - values["operating_cost"] if code == "gross_margin"
                     else values["net_income_attributable"])
    elif code == "operating_cashflow_to_net_income":
        numerator, denominator = values["cashflow_operations"], values["net_income"]
    else:
        raise ValueError("REPLAY_REFERENCE_FORMULA_UNSUPPORTED")
    if denominator <= 0:
        raise ValueError("REPLAY_NON_POSITIVE_DENOMINATOR")
    return numerator / denominator


def _reference_results(package: dict) -> list[dict]:
    metrics, evidence = package["metrics"], {item["id"]: item for item in package["evidence"]}
    if not metrics or len({item["id"] for item in metrics}) != len(metrics):
        raise ValueError("REPLAY_METRIC_IDENTIFIERS_INVALID")
    if len({item["code"] for item in metrics}) != len(metrics):
        raise ValueError("REPLAY_METRIC_CODES_DUPLICATED")
    results = []
    for metric in metrics:
        expected = _reference_metric(metric).quantize(Decimal("0.00000001"))
        if expected != Decimal(metric["value"]):
            raise ValueError(f"REPLAY_REFERENCE_METRIC_MISMATCH:{metric['code']}")
        if metric["input_fact_ids"] != [item["fact_id"] for item in metric["inputs"]]:
            raise ValueError("REPLAY_REFERENCE_FACT_IDS_MISMATCH")
        required = [ref for item in metric["inputs"] for ref in item["evidence_ids"]]
        if metric["required_evidence_ids"] != required:
            raise ValueError("REPLAY_REFERENCE_EVIDENCE_IDS_MISMATCH")
        for item in metric["inputs"]:
            if item.get("standard_unit") != "CNY" or not item["evidence_ids"]:
                raise ValueError("REPLAY_REFERENCE_INPUT_UNIT_OR_EVIDENCE_MISMATCH")
            if any(ref not in evidence or evidence[ref]["fact_id"] != item["fact_id"] for ref in item["evidence_ids"]):
                raise ValueError("REPLAY_REFERENCE_EVIDENCE_FACT_MISMATCH")
            for field in ("scope", "period_start", "period_end"):
                if item[field] != metric[field] or metric[field] != package["analysis_scope"][field]:
                    raise ValueError("REPLAY_REFERENCE_INPUT_SCOPE_OR_PERIOD_MISMATCH")
        results.append({"code": metric["code"], "reference_value": str(expected), "passed": True})
    return results


def _validate_raw(raw_content: str, package: dict) -> tuple[dict | None, dict, str | None, list[dict]]:
    from finance_domain.analysis.contracts import AnalysisOutput
    from pydantic import ValidationError

    from finance_adapters.providers.llm_analysis import AnalysisValidationError, _validate_claims

    output, structured, issues, error = None, True, [], None
    try:
        output = AnalysisOutput.model_validate_json(raw_content)
        _validate_claims(output, package)
    except ValidationError as exc:
        structured, error = False, str(exc)
        issues = [{"stage": "structure", "code": item["type"],
                   "path": "/" + "/".join(str(part) for part in item["loc"]), "message": item["msg"]}
                  for item in exc.errors(include_input=False, include_url=False)]
    except AnalysisValidationError as exc:
        issues, error = exc.issues, str(exc)
    except ValueError as exc:
        error = str(exc)
        issues = [{"stage": "semantic", "code": error, "path": "/"}]
    stages = {name: {"status": "failed" if any(item["stage"] == name for item in issues)
                     else "not_run" if not structured and name != "structure" else "passed",
                     "errors": [item for item in issues if item["stage"] == name]}
              for name in STAGES}
    return output.model_dump(mode="json") if output is not None else None, stages, error, issues


def _check_provider_metadata(attempt: dict) -> dict:
    from finance_adapters.providers.model_clients import ModelClient, ModelConfig, ModelRequestError

    metadata = attempt.get("provider_metadata", {})
    if not isinstance(metadata, dict):
        raise TypeError("REPLAY_PROVIDER_METADATA_INVALID")
    available = "request" in metadata or "response" in metadata
    if not available:
        return {"available": False, "reason": "Transport evidence was not recorded."}
    request = metadata.get("request")
    if not isinstance(request, dict) or stable_hash(request) != metadata.get("request_sha256"):
        raise ValueError("REPLAY_PROVIDER_REQUEST_HASH_MISMATCH")
    response = metadata.get("response")
    if response is not None and stable_hash(response) != metadata.get("response_sha256"):
        raise ValueError("REPLAY_PROVIDER_RESPONSE_HASH_MISMATCH")
    if response is None and metadata.get("response_sha256") is not None:
        raise ValueError("REPLAY_PROVIDER_RESPONSE_HASH_MISMATCH")
    if isinstance(response, dict):
        client = ModelClient(ModelConfig(metadata.get("provider", "openai"), "", "", metadata.get("model", "")))
        try:
            actual_reasoning = client._reasoning(response)
        except Exception as exc:
            if (attempt["status"] != "provider_failed"
                    or metadata.get("error") != {"type": type(exc).__name__, "code": "MODEL_REQUEST_FAILED"}):
                raise ValueError("REPLAY_PROVIDER_RESPONSE_PARSING_MISMATCH") from exc
            actual_reasoning = []
        if metadata.get("reasoning_output", []) != actual_reasoning:
            raise ValueError("REPLAY_PROVIDER_REASONING_METADATA_MISMATCH")
    body, model_request = request.get("body", {}), attempt["request"]
    if request.get("method") != "POST":
        raise ValueError("REPLAY_PROVIDER_REQUEST_METHOD_MISMATCH")
    if metadata.get("provider") == "anthropic":
        same_input = body.get("system") == model_request["system"] and body.get("messages") == [
            {"role": "user", "content": model_request["user"]}]
    else:
        same_input = body.get("messages") == [
            {"role": "system", "content": model_request["system"]},
            {"role": "user", "content": model_request["user"]}]
        expected_format = {"type": "json_object"}
        if "response_schema" in model_request:
            expected_format = {"type": "json_schema", "json_schema": {
                "name": "finreport_analysis", "strict": True,
                "schema": ModelClient._strict_schema(model_request["response_schema"]),
            }}
        if body.get("response_format") != expected_format:
            raise ValueError("REPLAY_PROVIDER_RESPONSE_FORMAT_MISMATCH")
    if not same_input:
        raise ValueError("REPLAY_PROVIDER_INPUT_MISMATCH")
    if attempt["status"] != "provider_failed":
        status_code = metadata.get("status_code")
        if not isinstance(status_code, int) or not 200 <= status_code < 300 or metadata.get("error"):
            raise ValueError("REPLAY_PROVIDER_SUCCESS_CLASSIFICATION_MISMATCH")
        if not isinstance(response, dict):
            raise ValueError("REPLAY_PROVIDER_RESPONSE_REQUIRED")
        if metadata.get("provider") == "anthropic":
            text = "".join(item.get("text", "") for item in response.get("content", [])
                           if item.get("type", "text") == "text")
            finish = response.get("stop_reason")
        else:
            choices = response.get("choices", [])
            text = choices[0].get("message", {}).get("content") if choices else None
            finish = choices[0].get("finish_reason") if choices else None
        if text != attempt["raw_content"]:
            raise ValueError("REPLAY_PROVIDER_RAW_CONTENT_MISMATCH")
        if metadata.get("usage") != response.get("usage") or metadata.get("finish_reason") != finish:
            raise ValueError("REPLAY_PROVIDER_NORMALIZED_METADATA_MISMATCH")
    else:
        error, status_code = metadata.get("error", {}), metadata.get("status_code")
        if error.get("code") != attempt.get("error"):
            raise ValueError("REPLAY_PROVIDER_FAILURE_ERROR_MISMATCH")
        code = error["code"]
        if code == "MODEL_REQUEST_TIMEOUT":
            valid_failure = (response is None and status_code is None
                             and error.get("type") in {"TimeoutException", "ReadTimeout", "WriteTimeout", "ConnectTimeout", "PoolTimeout"})
        elif code == "MODEL_REQUEST_FAILED":
            valid_failure = (error.get("type") == "HTTPStatusError"
                             and isinstance(status_code, int) and not 200 <= status_code < 400
                             or response is None and status_code is None and error.get("type") not in {None, "HTTPStatusError"})
            if not valid_failure and isinstance(response, dict) and isinstance(status_code, int) and 200 <= status_code < 300:
                try:
                    client._response(response)
                except ModelRequestError:
                    pass
                except Exception as exc:  # noqa: BLE001 - the original adapter also classified unexpected parsing errors.
                    valid_failure = error.get("type") == type(exc).__name__
        elif code == "MODEL_INVALID_JSON_RESPONSE":
            valid_failure = isinstance(response, str) and isinstance(status_code, int) and 200 <= status_code < 300
        elif code == "MODEL_INVALID_RESPONSE":
            valid_failure = response is not None and not isinstance(response, dict)
        else:
            valid_failure = False
            if isinstance(response, dict) and isinstance(status_code, int) and 200 <= status_code < 300:
                try:
                    client._response(response)
                except ModelRequestError as exc:
                    valid_failure = str(exc) == code
        if not valid_failure:
            raise ValueError("REPLAY_PROVIDER_FAILURE_RESPONSE_CLASSIFICATION_MISMATCH")
    return {"available": True, "request_hash_matches": True,
            "response_hash_matches": response is not None,
            "supplier_reasoning_fields": [item.get("field") for item in metadata.get("reasoning_output", [])]}


def _replay_attempts(trace: dict, package: dict) -> tuple[list[dict], dict | None]:
    import json

    from finance_domain.analysis.contracts import AnalysisOutput

    from finance_adapters.providers.llm_analysis import (
        SYSTEM_PROMPT,
        _analysis_request,
        _changed_paths,
        finalize_analysis_output,
        validate_analysis_output,
    )

    expected_request, results, accepted_output = _analysis_request(package), [], None
    attempts = trace["attempts"]
    if not 1 <= len(attempts) <= 2 or trace.get("system_prompt") != SYSTEM_PROMPT:
        raise ValueError("REPLAY_ATTEMPT_SEQUENCE_OR_SYSTEM_PROMPT_MISMATCH")
    for index, attempt in enumerate(attempts):
        if attempt.get("attempt") != index + 1 or stable_hash(attempt.get("request")) != attempt.get("request_hash"):
            raise ValueError("REPLAY_ATTEMPT_REQUEST_HASH_MISMATCH")
        request = attempt["request"]
        if request.get("system") != SYSTEM_PROMPT or request.get("response_json") is not True:
            raise ValueError("REPLAY_ATTEMPT_PROMPT_MISMATCH")
        if "response_schema" in request and request["response_schema"] != AnalysisOutput.model_json_schema():
            raise ValueError("REPLAY_ATTEMPT_SCHEMA_MISMATCH")
        try:
            submitted = json.loads(request["user"])
        except (ValueError, TypeError) as exc:
            raise ValueError("REPLAY_ATTEMPT_INPUT_INVALID") from exc
        if submitted != expected_request:
            raise ValueError("REPLAY_ATTEMPT_INPUT_OR_RETRY_FEEDBACK_MISMATCH")
        status = attempt.get("status")
        raw = attempt.get("raw_content")
        if status != "provider_failed" and (not isinstance(raw, str) or stable_hash(raw) != attempt.get("response_hash")):
            raise ValueError("REPLAY_ATTEMPT_RESPONSE_HASH_MISMATCH")
        provider = _check_provider_metadata(attempt)
        if status == "provider_failed":
            expected_stages = {name: {"status": "not_run", "errors": []} for name in STAGES}
            if attempt.get("validation") != expected_stages or any(
                field in attempt for field in ("raw_content", "model_output", "rendered_output")
            ) or not attempt.get("error") or index != len(attempts) - 1:
                raise ValueError("REPLAY_PROVIDER_FAILURE_CLASSIFICATION_MISMATCH")
            results.append({"attempt": index + 1, "status": status, "validation": expected_stages,
                            "provider_failure_reexecuted": False, "provider_metadata": provider})
            continue
        model_output, stages, error, issues = _validate_raw(raw, package)
        if attempt.get("validation") != stages or (model_output is not None and attempt.get("model_output") != model_output):
            raise ValueError("REPLAY_ATTEMPT_VALIDATION_MISMATCH")
        if error is not None:
            if status != "rejected" or attempt.get("validation_error") != error:
                raise ValueError("REPLAY_REJECTION_CLASSIFICATION_MISMATCH")
            expected_request["validation_feedback"] = {
                "rejected_response": raw, "error": error, "issues": issues,
                "instruction": "修正整个 JSON 后重新提交，不要省略限制或伪造数值。",
            }
        else:
            if status not in {"accepted", "render_rejected"} or index != len(attempts) - 1:
                raise ValueError("REPLAY_ACCEPTANCE_CLASSIFICATION_MISMATCH")
            rendered = finalize_analysis_output(AnalysisOutput.model_validate(model_output), package).model_dump(mode="json")
            if attempt.get("rendered_output") != rendered:
                raise ValueError("REPLAY_RENDERED_OUTPUT_MISMATCH")
            transition = attempt.get("provider_to_render", {})
            if (transition.get("model_output_hash") != stable_hash(model_output)
                    or transition.get("rendered_output_hash") != stable_hash(rendered)
                    or transition.get("changed_paths") != sorted(_changed_paths(model_output, rendered))
                    or transition.get("model_reasoning_proven") is not False):
                raise ValueError("REPLAY_RENDER_TRANSITION_MISMATCH")
            render_error = None
            try:
                validate_analysis_output(rendered, package)
            except ValueError as exc:
                render_error = str(exc)
            expected_render = {"status": "passed"} if render_error is None else {"status": "failed", "error": render_error}
            if attempt.get("render_validation") != expected_render or (render_error is None) != (status == "accepted"):
                raise ValueError("REPLAY_RENDER_VALIDATION_CLASSIFICATION_MISMATCH")
            if status == "accepted":
                accepted_output = rendered
                if trace.get("model_output") != model_output or trace.get("rendered_output") != rendered:
                    raise ValueError("REPLAY_TRACE_OUTPUT_MISMATCH")
        results.append({"attempt": index + 1, "status": status, "validation": stages,
                        "response_hash_matches": True, "provider_metadata": provider})
    return results, accepted_output


def validate_saved_analysis_run(run: AnalysisRun, package: dict) -> None:
    """Bind a current completed report to its exact saved model and render trace."""
    from finance_adapters.providers.llm_analysis import (
        ORCHESTRATION_VERSION,
        PROMPT_VERSION,
        VALIDATOR_VERSION,
    )

    if run.status != "completed" or run.failure_reason is not None:
        raise ValueError("REPLAY_COMPLETED_ANALYSIS_STATE_MISMATCH")
    versions = (PROMPT_VERSION, VALIDATOR_VERSION, ORCHESTRATION_VERSION)
    if (run.prompt_version, run.validator_version, run.orchestration_version) != versions:
        raise ValueError("REPLAY_IMPLEMENTATION_VERSION_MISMATCH")
    trace = run.tool_trace
    if not isinstance(trace, dict) or not trace.get("attempts") or not trace.get("evidence_package"):
        raise ValueError("REPLAY_CURRENT_ANALYSIS_TRACE_REQUIRED")
    if stable_hash(trace) != run.tool_trace_hash:
        raise ValueError("REPLAY_TOOL_TRACE_HASH_MISMATCH")
    if trace.get("failure_reason") is not None:
        raise ValueError("REPLAY_COMPLETED_ANALYSIS_STATE_MISMATCH")
    if (stable_hash(package) != run.input_snapshot_hash or trace["evidence_package"] != package
            or trace.get("input_hash") != run.input_snapshot_hash):
        raise ValueError("REPLAY_INPUT_SNAPSHOT_HASH_MISMATCH")
    if trace.get("versions") != dict(zip(("prompt", "validator", "orchestration"), versions)):
        raise ValueError("REPLAY_TRACE_VERSION_MISMATCH")
    attempts, rendered = _replay_attempts(trace, package)
    if any(not item["provider_metadata"]["available"] for item in attempts):
        raise ValueError("REPLAY_PROVIDER_TRANSPORT_EVIDENCE_REQUIRED")
    if rendered is None or rendered != run.output or trace.get("output_hash") != stable_hash(rendered):
        raise ValueError("REPLAY_RENDERED_OUTPUT_MISMATCH")


def replay_bundle(bundle: dict, source_pdf: Path | None = None) -> dict:
    from finance_adapters.providers.llm_analysis import (
        ORCHESTRATION_VERSION,
        PROMPT_VERSION,
        VALIDATOR_VERSION,
        _hash,
        validate_analysis_output,
    )

    if bundle.get("schema_version") != BUNDLE_VERSION:
        raise ValueError("REPLAY_UNSUPPORTED_BUNDLE_VERSION")
    payload = bundle["payload"]
    if stable_hash(payload) != bundle.get("payload_sha256"):
        raise ValueError("REPLAY_BUNDLE_HASH_MISMATCH")
    expected_versions = (PROMPT_VERSION, VALIDATOR_VERSION, ORCHESTRATION_VERSION)
    if tuple(payload[k] for k in ("prompt_version", "validator_version", "orchestration_version")) != expected_versions:
        raise ValueError("REPLAY_IMPLEMENTATION_VERSION_MISMATCH")
    trace = payload["tool_trace"]
    if _hash(trace) != payload["tool_trace_hash"]:
        raise ValueError("REPLAY_TOOL_TRACE_HASH_MISMATCH")
    package = trace["evidence_package"]
    if _hash(package) != payload["input_snapshot_hash"]:
        raise ValueError("REPLAY_INPUT_SNAPSHOT_HASH_MISMATCH")
    if trace.get("input_hash") != payload["input_snapshot_hash"]:
        raise ValueError("REPLAY_TRACE_INPUT_HASH_MISMATCH")
    if trace.get("versions") != dict(zip(("prompt", "validator", "orchestration"), expected_versions)):
        raise ValueError("REPLAY_TRACE_VERSION_MISMATCH")
    reference_results = _reference_results(package)
    attempt_results, replayed = _replay_attempts(trace, package)
    analysis_status = payload.get("analysis_status", "completed")
    if analysis_status == "completed":
        if payload.get("failure_reason") is not None or trace.get("failure_reason") is not None:
            raise ValueError("REPLAY_COMPLETED_ANALYSIS_STATE_MISMATCH")
        if replayed is None:
            raise ValueError("REPLAY_ACCEPTED_MODEL_RESPONSE_REQUIRED")
        if replayed != payload["output"] or trace.get("output_hash") != stable_hash(replayed):
            raise ValueError("REPLAY_RENDERED_OUTPUT_MISMATCH")
        validate_analysis_output(payload["output"], package)
    elif analysis_status == "failed":
        if payload["output"] is not None or not payload.get("failure_reason") or payload["failure_reason"] != trace.get("failure_reason"):
            raise ValueError("REPLAY_FAILED_ANALYSIS_STATE_MISMATCH")
    else:
        raise ValueError("REPLAY_ANALYSIS_NOT_TERMINAL")
    pdf_verified = None
    if source_pdf is not None:
        expected_pdf = package.get("document_context", {}).get("source_sha256")
        pdf_verified = bool(expected_pdf and hashlib.sha256(source_pdf.read_bytes()).hexdigest() == expected_pdf)
        if not pdf_verified:
            raise ValueError("REPLAY_SOURCE_PDF_HASH_MISMATCH")
    current = runtime_manifest()
    execution_runtime = trace.get("execution_runtime")
    recorded = execution_runtime or payload.get("export_runtime", {})
    code_match = recorded.get("source_files_sha256") == current["source_files_sha256"]
    if not code_match:
        raise ValueError("REPLAY_SOURCE_CODE_HASH_MISMATCH")
    dependencies_match = recorded.get("dependencies") == current["dependencies"]
    python_match = recorded.get("python") == current["python"]
    if not dependencies_match or not python_match:
        raise ValueError("REPLAY_RUNTIME_DEPENDENCY_MISMATCH")
    return {
        "status": "passed", "model_called": False, "database_required": False,
        "analysis_status": analysis_status, "failure_reason": payload.get("failure_reason"),
        "payload_sha256": bundle["payload_sha256"], "input_snapshot_hash": payload["input_snapshot_hash"],
        "reference_metrics": reference_results, "reference_metric_count": len(reference_results),
        "all_eight_supported_metrics_present": {item["code"] for item in reference_results} == set(REFERENCE_FORMULAS),
        "attempts": attempt_results, "rendered_output_matches": True if analysis_status == "completed" else None,
        "source_code_matches": code_match, "source_pdf_matches": pdf_verified,
        "runtime_provenance": {
            "execution_runtime_recorded": execution_runtime is not None,
            "comparison_basis": "execution_runtime" if execution_runtime else "export_runtime_only",
            "dependencies_match": dependencies_match, "python_matches": python_match,
            "platform_matches": recorded.get("platform") == current["platform"],
            "export_runtime_matches_execution": (payload.get("export_runtime", {}).get("source_files_sha256")
                                                 == execution_runtime.get("source_files_sha256")) if execution_runtime else None,
            "replay_runtime": current,
        },
        "reasoning_boundary": "Only provider-exposed fields recorded; no inference about hidden internals.",
        "boundaries": payload["boundaries"],
    }

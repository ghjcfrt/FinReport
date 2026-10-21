"""Optional local validation and remote structured-output schema adapters.

Guardrails checks the caller's schema without calling an LLM or its hosted
service.  Pass an action *envelope* schema to retain the workflow's independent
candidate admission.  These checks establish protocol validity, not financial
truth or evidence sufficiency.  Outlines only builds the remote provider's
``response_format``; it does not constrain tokens on the local machine.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from importlib import metadata
from typing import Any


class OutputGuardError(ValueError):
    """A failed optional backend or validation, with safe audit metadata."""

    def __init__(self, code: str, audit_metadata: dict[str, Any]):
        super().__init__(code)
        self.code = code
        self.audit_metadata = audit_metadata


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_json_text(value).encode("utf-8")).hexdigest()


def _version(distribution: str) -> str:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return "unknown"


def _preserve_open_values(schema: Any) -> Any:
    """Prevent Guardrails pruning data allowed by an unconstrained schema.

    ``{}`` allows every JSON value.  Its additionalProperties=True equivalent
    also tells Guardrails' JSON-path pruning pass to preserve opaque mappings,
    including findings which the caller will admit independently.
    """
    if not isinstance(schema, dict):
        return schema
    if not schema:
        return {"additionalProperties": True}
    result = deepcopy(schema)
    for key in ("$defs", "definitions", "properties", "patternProperties", "dependentSchemas"):
        if isinstance(result.get(key), dict):
            result[key] = {name: _preserve_open_values(value)
                           for name, value in result[key].items()}
    for key in ("items", "additionalProperties", "contains", "if", "then", "else", "not",
                "propertyNames", "unevaluatedProperties", "unevaluatedItems"):
        if isinstance(result.get(key), dict):
            result[key] = _preserve_open_values(result[key])
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        if isinstance(result.get(key), list):
            result[key] = [_preserve_open_values(value) for value in result[key]]
    return result


def _null_omission_paths(original: Any, parsed: Any, path: str = "$") -> list[str] | None:
    """Find only omitted null object keys; any other difference is invalid."""
    if type(original) is not type(parsed):
        return None
    paths: list[str] = []
    if isinstance(original, dict):
        if parsed.keys() - original.keys():
            return None
        for key in sorted(original):
            child_path = f"{path}.{key}"
            if key not in parsed:
                if original[key] is not None:
                    return None
                paths.append(child_path)
            else:
                child_paths = _null_omission_paths(original[key], parsed[key], child_path)
                if child_paths is None:
                    return None
                paths.extend(child_paths)
    elif isinstance(original, list):
        if len(original) != len(parsed):
            return None
        for index, (first, second) in enumerate(zip(original, parsed, strict=True)):
            child_paths = _null_omission_paths(first, second, f"{path}[{index}]")
            if child_paths is None:
                return None
            paths.extend(child_paths)
    elif original != parsed:
        return None
    return paths


def guard_agent_action(raw: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Validate immutable JSON against the supplied schema, returning an audit.

    No repair or reask is adopted.  The original schema first checks the raw
    values before Guardrails' higher-level parser can coerce or prune them.
    Its null object-key omissions are recorded; its parsed output is never
    returned for admission.  Other changes fail validation.
    The caller retains responsibility for cross-field financial business rules
    and per-candidate evidence admission.
    """
    audit: dict[str, Any] = {
        "backend": "guardrails-ai", "backend_version": _version("guardrails-ai"),
        "passed": False, "local_only": True, "num_reasks": 0,
    }
    if not isinstance(raw, dict) or not isinstance(schema, dict):
        raise OutputGuardError("OUTPUT_GUARD_INVALID_ARGUMENT", audit)
    try:
        audit.update(input_sha256=_sha256(raw), schema_sha256=_sha256(schema))
    except (TypeError, ValueError):
        raise OutputGuardError("OUTPUT_GUARD_INVALID_JSON", audit) from None
    try:
        from guardrails import Guard
        from guardrails.schema.validator import (
            SchemaValidationError,
            validate_json_schema,
            validate_payload,
        )
        from guardrails.settings import settings as guardrails_settings
    except ImportError:
        raise OutputGuardError("OUTPUT_GUARD_DEPENDENCY_UNAVAILABLE", audit) from None

    try:
        validate_json_schema(schema)
    except SchemaValidationError as exc:
        audit["error_paths"] = sorted(exc.fields)
        raise OutputGuardError("OUTPUT_GUARD_INVALID_SCHEMA", audit) from None
    try:
        # This avoids silently accepting values repaired by Guard.parse.
        validate_payload(deepcopy(raw), deepcopy(schema))
    except SchemaValidationError as exc:
        audit["error_paths"] = sorted(exc.fields)
        raise OutputGuardError("OUTPUT_GUARD_SCHEMA_REJECTED", audit) from None
    except Exception as exc:  # noqa: BLE001 - normalize optional backend failures
        audit["exception_type"] = type(exc).__name__
        raise OutputGuardError("OUTPUT_GUARD_SCHEMA_ERROR", audit) from None

    try:
        # The anonymous-metrics switch does not disable the independent OTel
        # instrumentation.  Disable Guardrails tracing itself before raw text
        # reaches Guard/Runner; preserve the application's own tracer provider.
        guardrails_settings.disable_tracing = True
        guard = Guard(output_schema=_preserve_open_values(schema), use_server=False)
        guard.configure(allow_metrics_collection=False)
        outcome = guard.parse(_json_text(raw), num_reasks=0, llm_api=None, disable_tracer=True)
        audit["tracing_disabled"] = True
        if not outcome.validation_passed:
            raise OutputGuardError("OUTPUT_GUARD_REJECTED", audit)
        audit["validated_output_sha256"] = _sha256(outcome.validated_output)
        audit["output_unchanged"] = audit["validated_output_sha256"] == audit["input_sha256"]
        omissions = _null_omission_paths(raw, outcome.validated_output)
        if omissions is None:
            raise OutputGuardError("OUTPUT_GUARD_MUTATED_INPUT", audit)
        audit["normalization_null_only"] = bool(omissions)
        audit["normalized_null_paths"] = omissions
    except OutputGuardError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize optional backend failures
        audit["exception_type"] = type(exc).__name__
        raise OutputGuardError("OUTPUT_GUARD_BACKEND_ERROR", audit) from None
    audit["passed"] = True
    return audit


def outlines_response_format(schema: dict[str, Any]) -> dict[str, Any]:
    """Build OpenAI-compatible JSON Schema options using Outlines itself.

    The caller sends and audits these options through its existing transport.
    A remote gateway must support the resulting JSON Schema contract; this
    function neither probes that capability nor guarantees token-level decoding.
    """
    audit = {"backend": "outlines", "backend_version": _version("outlines"),
             "local_constrained_decoding": False}
    if not isinstance(schema, dict):
        raise OutputGuardError("OUTLINES_INVALID_SCHEMA", audit)
    try:
        from outlines.models.openai import OpenAITypeAdapter
        from outlines.types import JsonSchema
    except ImportError:
        raise OutputGuardError("OUTLINES_DEPENDENCY_UNAVAILABLE", audit) from None
    try:
        # A plain dict denotes loose JSON mode to Outlines.  JsonSchema is
        # required to request the strict remote JSON Schema response format.
        result = OpenAITypeAdapter().format_output_type(JsonSchema(deepcopy(schema)))
        if result.get("response_format", {}).get("type") != "json_schema":
            raise OutputGuardError("OUTLINES_SCHEMA_FORMAT_INVALID", audit)
        return result
    except OutputGuardError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize optional backend failures
        audit["exception_type"] = type(exc).__name__
        raise OutputGuardError("OUTLINES_SCHEMA_ERROR", audit) from None

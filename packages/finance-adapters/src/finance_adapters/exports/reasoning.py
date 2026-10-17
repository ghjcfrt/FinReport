"""Public, credential-free projection of provider-returned reasoning fields."""
from __future__ import annotations

import json


def provider_reasoning(trace: dict | None) -> dict:
    """Expose recorded text, never the private request/response metadata container."""
    trace = trace if isinstance(trace, dict) else {}
    calls = []
    groups = [("指标分析", trace.get("attempts"))]
    for key in ("agent", "agent_workflow_execution"):
        agent = trace.get(key)
        if isinstance(agent, dict):
            groups.append(("证据分析", agent.get("rounds")))
    for stage, attempts in groups:
        if not isinstance(attempts, list):
            continue
        for index, attempt in enumerate(attempts, 1):
            if not isinstance(attempt, dict):
                continue
            metadata = attempt.get("provider_metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            request = metadata.get("request")
            body = request.get("body", {}) if isinstance(request, dict) else {}
            enabled = body.get("enable_thinking") if isinstance(body, dict) else None
            enabled = enabled if type(enabled) is bool else None
            fields = []
            raw_fields = metadata.get("reasoning_output", [])
            for field in raw_fields if isinstance(raw_fields, list) else []:
                if not isinstance(field, dict):
                    continue
                content = field.get("content")
                if content is None or content == "":
                    continue
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False)
                fields.append({"field": str(field.get("field", "unknown")), "content": content})
            status = "returned" if fields else "disabled" if enabled is False else "not_returned"
            calls.append({"stage": stage, "call_index": index, "status": status,
                          "thinking_enabled": enabled, "fields": fields})
    return {"status": "available" if any(call["fields"] for call in calls)
            else "not_returned" if calls else "not_called", "calls": calls}

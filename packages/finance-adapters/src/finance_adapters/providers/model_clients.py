from __future__ import annotations

import base64
import hashlib
import json
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from finance_adapters.providers.run_journal import (
    JournalPersistenceError,
    journal_stream_event,
    start_model_call,
)


class BaiduUnlimitedOCRAdapter:
    """Build and normalize the two supported Unlimited-OCR wire protocols.

    Local vLLM deployments expose an OpenAI-compatible endpoint.  The
    managed Baidu service exposes an asynchronous form endpoint instead.  The
    adapter is deliberately limited to payload construction and response
    normalization so both protocols remain deterministic and easy to fixture.
    """

    PROVIDERS = frozenset({"baidu-unlimited-ocr", "baidu_unlimited_ocr", "unlimited-ocr", "unlimited_ocr"})

    @classmethod
    def matches(cls, provider: str) -> bool:
        return provider.strip().lower() in cls.PROVIDERS

    @staticmethod
    def is_retired_ocr(provider: str, model: str) -> bool:
        normalized = [value.lower().replace("-", "").replace("_", "") for value in (provider, model)]
        return any("deepseekocr" in value or value == "dsocr" for value in normalized)

    @staticmethod
    def resolve_protocol(base_url: str, protocol: str = "auto") -> str:
        configured = protocol.strip().lower().replace("-", "_")
        if configured in {"baidu_cloud", "cloud"}:
            return "baidu_cloud"
        if configured in {"openai_compatible", "openai", "http", "local"}:
            return "openai_compatible"
        parsed = urlsplit(base_url)
        if (parsed.hostname or "").lower() == "aip.baidubce.com" or "unlimited-ocr-parser" in parsed.path.lower():
            return "baidu_cloud"
        return "openai_compatible"

    @staticmethod
    def openai_payload(*, model: str, prompt: str, image: bytes, media_type: str) -> dict[str, Any]:
        encoded = base64.b64encode(image).decode("ascii")
        return {
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:{media_type};base64,{encoded}",
                }},
            ]}],
            "temperature": 0,
            "max_tokens": 8192,
            "skip_special_tokens": False,
            "vllm_xargs": {"ngram_size": 35, "window_size": 128},
            "stream": False,
        }

    @staticmethod
    def cloud_submission(*, image: bytes, file_name: str = "page.png") -> dict[str, str]:
        return {
            "file_data": base64.b64encode(image).decode("ascii"),
            "file_name": file_name,
            "return_span_boxes": "true",
            "merge_tables": "false",
        }

    @staticmethod
    def result_text(data: Any) -> str:
        """Extract text and table markdown from a cloud parse-result JSON body."""
        if isinstance(data, str):
            return data
        if not isinstance(data, dict):
            return ""
        result = data.get("result", data)
        if isinstance(result, dict):
            for key in ("markdown", "text", "content"):
                value = result.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            pages = result.get("pages")
        else:
            pages = None
        if not isinstance(pages, list):
            return ""
        chunks: list[str] = []
        for page in pages:
            if not isinstance(page, dict):
                continue
            text = page.get("text", "")
            if isinstance(text, str) and text.strip():
                chunks.append(text)
            tables = page.get("tables")
            for table in tables if isinstance(tables, list) else []:
                markdown = table.get("markdown") if isinstance(table, dict) else None
                if isinstance(markdown, str) and markdown.strip() and markdown not in (text or ""):
                    chunks.append(markdown)
        return "\n\n".join(chunks)


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    base_url: str
    api_key: str
    model: str
    timeout: float = 60.0
    enable_thinking: bool | None = None
    reasoning_effort: str | None = None
    protocol: str = "auto"
    secret_key: str = ""
    poll_interval: float = 5.0
    stream: bool = False
    provider_sort: str | None = None
    schema_builder: str = "native"

    def __post_init__(self) -> None:
        if self.schema_builder not in {"native", "outlines"}:
            raise ValueError("MODEL_INVALID_SCHEMA_BUILDER")
        if self.schema_builder == "outlines" and self.provider == "anthropic":
            raise ValueError("MODEL_SCHEMA_BUILDER_UNSUPPORTED_PROVIDER")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("MODEL_INVALID_TIMEOUT")
        if not math.isfinite(self.poll_interval) or self.poll_interval < 0:
            raise ValueError("MODEL_INVALID_POLL_INTERVAL")
        if not isinstance(self.stream, bool):
            raise TypeError("MODEL_INVALID_STREAM")
        if self.provider_sort is not None and (
            not isinstance(self.provider_sort, str) or not self.provider_sort.strip()
        ):
            raise ValueError("MODEL_INVALID_PROVIDER_SORT")


class ModelRequestError(ValueError):
    """A provider failure with the same sanitized evidence as a successful call."""

    def __init__(self, code: str, metadata: dict[str, Any]):
        super().__init__(code)
        self.metadata = metadata
        self.audit_metadata = metadata


class ModelClient:
    """Small protocol adapter for OpenAI and Anthropic APIs.

    Secrets stay in the server process. Callers only receive normalized text and
    response metadata, which keeps OCR and analysis auditable and provider-neutral.
    """

    ACTION_TOOL_NAME = "financial_evidence_action"

    def __init__(self, config: ModelConfig):
        self.config = config
        if config.schema_builder == "outlines":
            from finance_adapters.providers.output_guard import outlines_response_format

            # A missing/broken optional SDK must fail before the evidence loop
            # can make paid native-tool requests and reach final synthesis.
            outlines_response_format({"type": "object", "properties": {},
                                      "additionalProperties": False})
        self._ocr_access_token = ""
        self._ocr_token_expires_at = 0.0
        self._known_secrets = {value for value in (config.api_key, config.secret_key) if value}

    def _sanitize(self, value: Any) -> Any:
        if isinstance(value, float) and not math.isfinite(value):
            return {"non_finite_number": str(value)}
        if isinstance(value, dict):
            sensitive = {"authorization", "xapikey", "apikey", "accesstoken", "refreshtoken",
                         "password", "secret", "secretkey", "clientid", "clientsecret", "cookie", "setcookie"}
            return {
                str(key): "[REDACTED]" if str(key).lower().replace("-", "").replace("_", "") in sensitive
                else self._sanitize(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._sanitize(item) for item in value]
        if isinstance(value, str):
            for secret in sorted(self._known_secrets, key=lambda value: (-len(value), value)):
                if value == secret:
                    return "[REDACTED]"
                if len(secret) >= 8:
                    value = value.replace(secret, "[REDACTED]")
        return value

    @staticmethod
    def _hash(value: Any) -> str:
        return hashlib.sha256(json_text(value).encode("utf-8")).hexdigest()

    @classmethod
    def _strict_schema(cls, schema: Any, *, mapping: bool = False) -> Any:
        if isinstance(schema, list):
            return [cls._strict_schema(item) for item in schema]
        if not isinstance(schema, dict):
            return schema
        if mapping:
            return {name: cls._strict_schema(item) for name, item in schema.items()}
        result = {name: cls._strict_schema(item, mapping=name in {"properties", "$defs", "definitions"})
                  for name, item in schema.items() if name != "default"}
        if "properties" in result:
            result["additionalProperties"] = False
            result["required"] = list(result["properties"])
        return result

    @staticmethod
    def _audit_url(url: str) -> str:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parsed.port:
            host += f":{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))

    def _reasoning(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        fields = []
        if self.config.provider == "anthropic":
            for index, block in enumerate(data.get("content", [])):
                if isinstance(block, dict) and block.get("type") == "thinking":
                    fields.append({"field": f"content[{index}].thinking",
                                   "type": "provider_reported_reasoning", "content": block.get("thinking", "")})
        else:
            for index, choice in enumerate(data.get("choices", [])):
                message = choice.get("message", {})
                for name in ("reasoning_content", "reasoning", "thinking", "thought"):
                    if name in message:
                        fields.append({"field": f"choices[{index}].message.{name}",
                                       "type": "provider_reported_reasoning", "content": message[name]})
        return self._sanitize(fields)

    @classmethod
    def _action_tool(cls, schema: dict[str, Any], *, anthropic: bool = False) -> dict[str, Any]:
        description = "Submit one controlled financial evidence action. No other tools are available."
        if anthropic:
            return {"name": cls.ACTION_TOOL_NAME, "description": description, "input_schema": schema}
        return {"type": "function", "function": {
            "name": cls.ACTION_TOOL_NAME, "description": description,
            "parameters": cls._strict_schema(schema), "strict": True,
        }}

    def _response(self, data: dict[str, Any], *,
                  native_action_schema: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
        if not isinstance(data, dict):
            raise ModelRequestError("MODEL_INVALID_RESPONSE", {})
        if data.get("error"):
            raise ModelRequestError("MODEL_PROVIDER_ERROR", {})
        metadata = self._sanitize({
            "id": data.get("id"), "model": data.get("model", self.config.model),
            "usage": self._sanitize(data.get("usage")), "reasoning_output": self._reasoning(data),
        })
        if self.config.provider == "anthropic":
            finish = data.get("stop_reason")
            content = "".join(item.get("text", "") for item in data.get("content", [])
                              if item.get("type", "text") == "text")
            tool_calls = [item for item in data.get("content", []) if item.get("type") == "tool_use"]
        else:
            choices = data.get("choices", [])
            if not choices:
                raise ModelRequestError("MODEL_EMPTY_RESPONSE", metadata)
            finish = choices[0].get("finish_reason")
            content = choices[0].get("message", {}).get("content")
            tool_calls = choices[0].get("message", {}).get("tool_calls") or []
        metadata["finish_reason"] = finish
        if finish in {"length", "max_tokens"}:
            raise ModelRequestError("MODEL_OUTPUT_TRUNCATED", metadata)
        if finish in {"content_filter", "refusal"}:
            raise ModelRequestError("MODEL_OUTPUT_BLOCKED", metadata)
        if native_action_schema is not None and tool_calls:
            if not isinstance(tool_calls, list) or len(tool_calls) != 1 or not isinstance(tool_calls[0], dict):
                raise ModelRequestError("MODEL_ACTION_TOOL_COUNT_INVALID", metadata)
            call = tool_calls[0]
            if self.config.provider == "anthropic":
                name, arguments = call.get("name"), call.get("input")
            else:
                function = call.get("function")
                if call.get("type") != "function" or not isinstance(function, dict):
                    raise ModelRequestError("MODEL_ACTION_TOOL_INVALID", metadata)
                name, arguments = function.get("name"), function.get("arguments")
            if name != self.ACTION_TOOL_NAME:
                raise ModelRequestError("MODEL_ACTION_TOOL_NOT_REGISTERED", metadata)
            try:
                if not isinstance(arguments, (str, dict)):
                    raise TypeError("Action arguments must be an object")
                # Preserve wire arguments, including invalid JSON, so the
                # strict action validator can reject them and ask the model to
                # correct its action. Never repair or execute provider JSON.
                content = arguments if isinstance(arguments, str) else json_text(arguments)
            except (TypeError, ValueError) as exc:
                raise ModelRequestError("MODEL_ACTION_ARGUMENTS_INVALID", metadata) from exc
            metadata.update(response_channel="native_action_tool", action_tool_name=name,
                            action_tool_id=call.get("id"))
        elif native_action_schema is not None:
            # Some compatible gateways still return the requested action JSON
            # in content. It goes through the same strict action validator.
            metadata["response_channel"] = "content"
        if not isinstance(content, str) or not content.strip():
            raise ModelRequestError("MODEL_EMPTY_RESPONSE", metadata)
        return content, metadata

    def _stream_data(self, response: httpx.Response, metadata: dict[str, Any]) -> dict[str, Any]:
        """Assemble OpenAI SSE into the same response used by live and offline validation.

        Event hashes cover sanitized semantic events, not private raw wire bytes.
        A disconnect without a terminal finish reason is never a successful answer.
        """
        data: dict[str, Any] = {"choices": []}
        events: list[dict[str, Any]] = []
        metadata.update(stream_events=events, stream_complete=False, stream_done=False,
                        stream_transport="sse")
        message: dict[str, Any] = {"role": "assistant", "content": ""}
        choice: dict[str, Any] = {"index": 0, "message": message, "finish_reason": None}
        tools: dict[int, dict[str, Any]] = {}
        event_lines: list[str] = []
        event_name = "message"
        event_id: str | None = None

        def fail(code: str = "MODEL_INVALID_STREAM_RESPONSE") -> None:
            raise ModelRequestError(code, {})

        def append_text(target: dict[str, Any], name: str, value: Any) -> None:
            if value is None:
                return
            if not isinstance(value, str):
                fail()
            target[name] = target.get(name, "") + value

        def consume() -> None:
            nonlocal event_lines, event_name, event_id
            if not event_lines:
                event_name, event_id = "message", None
                return
            wire = "\n".join(event_lines)
            event_lines = []
            if wire == "[DONE]":
                events.append({"event": event_name, "id": event_id, "data": "[DONE]", "data_encoding": "sentinel"})
                journal_stream_event(events[-1])
                metadata["stream_done"] = True
                event_name, event_id = "message", None
                return
            try:
                chunk = json.loads(wire)
            except ValueError as exc:
                events.append({"event": event_name, "id": event_id, "data": self._sanitize(wire),
                               "data_encoding": "raw"})
                journal_stream_event(events[-1])
                raise ModelRequestError("MODEL_INVALID_STREAM_JSON", {}) from exc
            events.append(self._sanitize({"event": event_name, "id": event_id, "data": chunk,
                                          "data_encoding": "json"}))
            journal_stream_event(events[-1])
            is_error = event_name == "error"
            event_name, event_id = "message", None
            if metadata["stream_done"] or not isinstance(chunk, dict):
                fail()
            if is_error or chunk.get("error"):
                fail("MODEL_PROVIDER_ERROR")
            for name in ("id", "model", "created", "system_fingerprint"):
                if chunk.get(name) is not None:
                    data[name] = chunk[name]
            if chunk.get("usage") is not None:
                if not isinstance(chunk["usage"], dict):
                    fail()
                data["usage"] = chunk["usage"]
            chunks = chunk.get("choices", [])
            if chunks is None:
                chunks = []
            if not isinstance(chunks, list):
                fail()
            for part in chunks:
                if not isinstance(part, dict) or part.get("index", 0) != 0:
                    fail()
                delta = part.get("delta") or {}
                if not isinstance(delta, dict):
                    fail()
                if choice["finish_reason"] is not None and delta:
                    fail()
                if not data["choices"]:
                    data["choices"] = [choice]
                for name in ("content", "reasoning_content", "reasoning", "thinking", "thought"):
                    if name in delta:
                        append_text(message, name, delta[name])
                if "refusal" in delta:
                    append_text(message, "refusal", delta["refusal"])
                calls = delta.get("tool_calls") or []
                if not isinstance(calls, list):
                    fail()
                for call in calls:
                    if not isinstance(call, dict):
                        fail()
                    index = call.get("index")
                    if type(index) is not int or index < 0:
                        fail()
                    tool = tools.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    for name in ("id", "type"):
                        if call.get(name) is not None:
                            if not isinstance(call[name], str):
                                fail()
                            if name == "id":
                                append_text(tool, name, call[name])
                            else:
                                tool[name] = call[name]
                    function = call.get("function") or {}
                    if not isinstance(function, dict):
                        fail()
                    for name in ("name", "arguments"):
                        if name in function:
                            append_text(tool["function"], name, function[name])
                    message["tool_calls"] = [tools[key] for key in sorted(tools)]
                finish = part.get("finish_reason")
                if finish is not None:
                    if finish not in {"stop", "tool_calls", "function_call", "length", "content_filter", "refusal"}:
                        fail("MODEL_INVALID_FINISH_REASON")
                    if choice["finish_reason"] is not None and choice["finish_reason"] != finish:
                        fail()
                    choice["finish_reason"] = finish

        try:
            for line in response.iter_lines():
                line = line.removeprefix("\ufeff")
                if not line:
                    consume()
                elif line.startswith(":"):
                    events.append({"comment": self._sanitize(line[1:])})
                    journal_stream_event(events[-1])
                else:
                    field, _, value = line.partition(":")
                    value = value.removeprefix(" ")
                    if field == "data":
                        event_lines.append(value)
                    elif field == "event":
                        event_name = value
                    elif field == "id":
                        event_id = value
            consume()
            if choice["finish_reason"] is None:
                fail("MODEL_STREAM_INCOMPLETE")
            metadata["stream_complete"] = True
            return data
        finally:
            metadata["response"] = self._sanitize(data)
            metadata["response_sha256"] = self._hash(metadata["response"])
            metadata["stream_events_sha256"] = self._hash(events)
            metadata.update({key: value for key, value in self._sanitize({
                "usage": data.get("usage"), "reasoning_output": self._reasoning(data),
                "finish_reason": choice["finish_reason"],
            }).items()})

    def _request(self, url: str, headers: dict[str, str], payload: dict[str, Any], *,
                 native_action_schema: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
        started = time.perf_counter()
        request = self._sanitize({"method": "POST", "url": self._audit_url(url), "headers": headers, "body": payload})
        metadata: dict[str, Any] = {
            "provider": self.config.provider, "model": self.config.model,
            "started_at": datetime.now(UTC).isoformat(), "timeout_seconds": self.config.timeout,
            "request": request, "request_sha256": self._hash(request),
            "response": None, "response_sha256": None, "status_code": None,
            "usage": None, "reasoning_output": [], "hash_basis": "canonical_sanitized_json",
        }
        journal_call = start_model_call(metadata, self._known_secrets)
        journal_error: str | None = None
        try:
            with httpx.Client(timeout=self.config.timeout) as client:
                if payload.get("stream") is True:
                    if journal_call:
                        journal_call.dispatch()
                    with client.stream("POST", url, headers=headers, json=payload) as response:
                        if journal_call:
                            journal_call.response_received(response.status_code, dict(response.headers))
                        metadata["status_code"] = response.status_code
                        metadata["response_headers"] = self._sanitize(dict(response.headers))
                        if response.is_success and "text/event-stream" in response.headers.get("content-type", ""):
                            data = self._stream_data(response, metadata)
                        else:
                            response.read()
                            data = self._json_response(response, metadata)
                            # Some gateways ignore stream=true and send a whole response.
                            # Accept only a visibly terminated completion in that case.
                            choices = data.get("choices", []) if isinstance(data, dict) else []
                            finish = (choices[0].get("finish_reason") if isinstance(choices, list)
                                      and choices and isinstance(choices[0], dict) else None)
                            metadata.update(stream_transport="json_fallback", stream_complete=bool(finish))
                            if finish is not None and finish not in {
                                "stop", "tool_calls", "function_call", "length", "content_filter", "refusal",
                            }:
                                raise ModelRequestError("MODEL_INVALID_FINISH_REASON", {})
                            if response.is_success and not finish and not (isinstance(data, dict) and data.get("error")):
                                raise ModelRequestError("MODEL_STREAM_INCOMPLETE", {})
                else:
                    if journal_call:
                        journal_call.dispatch()
                    response = client.post(url, headers=headers, json=payload)
                    if journal_call:
                        journal_call.response_received(getattr(response, "status_code", None),
                                                       dict(getattr(response, "headers", {})))
                    data = self._json_response(response, metadata)
            if journal_call:
                journal_call.persist_response(metadata.get("response", data),
                                              stream_complete=metadata.get("stream_complete"),
                                              stream_transport=metadata.get("stream_transport"))
            if not isinstance(data, dict):
                raise ModelRequestError("MODEL_INVALID_RESPONSE", metadata)
            content, normalized = self._response(data, native_action_schema=native_action_schema)
            metadata.update(normalized)
            if journal_call:
                content = journal_call.journal.sanitize(content)
            return content, metadata
        except ModelRequestError as exc:
            journal_error = str(exc)
            metadata.update(exc.metadata)
            metadata["error"] = {"type": type(exc).__name__, "code": str(exc)}
            if journal_call:
                raise ModelRequestError(journal_call.journal.sanitize(str(exc)), metadata) from None
            raise ModelRequestError(str(exc), metadata) from exc
        except Exception as exc:
            code = "MODEL_REQUEST_TIMEOUT" if isinstance(exc, httpx.TimeoutException) else "MODEL_REQUEST_FAILED"
            if isinstance(exc, JournalPersistenceError):
                code = str(exc)
            if journal_call:
                metadata["failure_phase"] = (
                    "connection_timeout" if isinstance(exc, httpx.ConnectTimeout) else
                    "stream_idle_timeout" if isinstance(exc, httpx.ReadTimeout) and journal_call.received else
                    "response_header_timeout" if isinstance(exc, httpx.ReadTimeout) else
                    "write_timeout" if isinstance(exc, httpx.WriteTimeout) else
                    "pool_timeout" if isinstance(exc, httpx.PoolTimeout) else
                    "connection_interrupted" if isinstance(exc, httpx.TransportError) else
                    "local_or_protocol_failure")
            journal_error = code
            metadata["error"] = {"type": type(exc).__name__, "code": code}
            if journal_call:
                raise ModelRequestError(code, metadata) from None
            raise ModelRequestError(code, metadata) from exc
        finally:
            metadata["duration_seconds"] = round(time.perf_counter() - started, 6)
            if journal_call:
                # Incomplete streams and protocol errors are retained as
                # fragments, never promoted to a complete response. A complete
                # HTTP error body can still be persisted before being rejected.
                try:
                    safe_metadata = journal_call.journal.sanitize(metadata)
                    metadata.clear()
                    metadata.update(safe_metadata)
                    # Scoped sanitization also removes signed-URL credentials
                    # from provider text. Hash the final exposed representation,
                    # rather than retaining a hash of its less-filtered parent.
                    for field in ("request", "response", "stream_events"):
                        if metadata.get(field) is not None:
                            metadata[field + "_sha256"] = self._hash(metadata[field])
                    if (not journal_call.persisted and metadata.get("response") is not None
                            and not (payload.get("stream") is True
                                     and metadata.get("stream_complete") is not True)):
                        journal_call.persist_response(metadata["response"],
                                                      status_code=metadata.get("status_code"))
                    journal_call.finish(metadata, journal_error)
                finally:
                    journal_call.close()

    def _json_response(self, response: httpx.Response, metadata: dict[str, Any]) -> Any:
        metadata["status_code"] = getattr(response, "status_code", None)
        metadata["response_headers"] = self._sanitize(dict(getattr(response, "headers", {})))
        try:
            data = response.json()
        except ValueError:
            metadata["response"] = self._sanitize(getattr(response, "text", ""))
            metadata["response_sha256"] = self._hash(metadata["response"])
            response.raise_for_status()
            raise ModelRequestError("MODEL_INVALID_JSON_RESPONSE", {}) from None
        metadata["response"] = self._sanitize(data)
        metadata["response_sha256"] = self._hash(metadata["response"])
        response.raise_for_status()
        return data

    def complete(self, *, system: str, user: str, response_json: bool = False,
                 response_schema: dict[str, Any] | None = None,
                 native_action_schema: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
        if self.config.provider == "anthropic":
            payload: dict[str, Any] = {
                "model": self.config.model,
                "max_tokens": 8192,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
            if native_action_schema is not None:
                payload["tools"] = [self._action_tool(native_action_schema, anthropic=True)]
                payload["tool_choice"] = {"type": "tool", "name": self.ACTION_TOOL_NAME,
                                          "disable_parallel_tool_use": True}
            headers = {
                "x-api-key": self.config.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            url = self.config.base_url.rstrip("/") + "/messages"
        else:
            payload = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
            if native_action_schema is not None:
                payload["tools"] = [self._action_tool(native_action_schema)]
                payload["tool_choice"] = {"type": "function", "function": {"name": self.ACTION_TOOL_NAME}}
                payload["parallel_tool_calls"] = False
            elif response_schema is not None:
                if self.config.schema_builder == "outlines":
                    from finance_adapters.providers.output_guard import outlines_response_format

                    payload.update(outlines_response_format(self._strict_schema(response_schema)))
                    # Keep the stable wire name used by historical provider
                    # replay; Outlines' adapter defaults to the name 'default'.
                    payload["response_format"]["json_schema"]["name"] = "finreport_analysis"
                elif self.config.schema_builder == "native":
                    payload["response_format"] = {"type": "json_schema", "json_schema": {
                        "name": "finreport_analysis", "strict": True, "schema": self._strict_schema(response_schema),
                    }}
                else:
                    raise ValueError("MODEL_INVALID_SCHEMA_BUILDER")
            elif response_json:
                payload["response_format"] = {"type": "json_object"}
            if self.config.enable_thinking is not None:
                payload["enable_thinking"] = self.config.enable_thinking
            if self.config.reasoning_effort:
                payload["reasoning_effort"] = self.config.reasoning_effort
            if self.config.stream:
                payload["stream"] = True
                payload["stream_options"] = {"include_usage": True}
            if self.config.provider_sort:
                payload["provider"] = {"sort": self.config.provider_sort}
            headers = {"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {}
            url = self.config.base_url.rstrip("/") + "/chat/completions"
        if native_action_schema is not None:
            return self._request(url, headers, payload, native_action_schema=native_action_schema)
        content, metadata = self._request(url, headers, payload)
        if response_schema is not None and self.config.schema_builder == "outlines" and self.config.provider != "anthropic":
            from importlib.metadata import version

            metadata["structured_output_builder"] = {
                "backend": "outlines", "backend_version": version("outlines"),
                "local_constrained_decoding": False,
            }
        return content, metadata

    def _ocr_protocol(self) -> str:
        return BaiduUnlimitedOCRAdapter.resolve_protocol(self.config.base_url, self.config.protocol)

    def _baidu_cloud_vision(self, *, image: bytes) -> tuple[str, dict[str, Any]]:
        started = time.perf_counter()
        deadline = time.monotonic() + self.config.timeout
        base_url = self.config.base_url.rstrip("/")
        if base_url.endswith("/unlimited-ocr-parser/task/query"):
            submit_url = base_url.removesuffix("/query")
        elif base_url.endswith("/unlimited-ocr-parser/task"):
            submit_url = base_url
        else:
            submit_url = base_url + "/rest/2.0/brain/online/v2/unlimited-ocr-parser/task"
        query_url = submit_url + "/query"
        request_body = BaiduUnlimitedOCRAdapter.cloud_submission(image=image)
        metadata: dict[str, Any] = {
            "provider": self.config.provider, "model": self.config.model,
            "started_at": datetime.now(UTC).isoformat(), "timeout_seconds": self.config.timeout,
            "request": self._sanitize({
                "method": "POST", "url": self._audit_url(submit_url),
                "headers": {"content-type": "application/x-www-form-urlencoded"},
                "body": request_body,
            }),
            "request_sha256": None, "response": None, "response_sha256": None,
            "status_code": None, "usage": None, "reasoning_output": [],
            "hash_basis": "canonical_sanitized_json", "http_exchanges": [],
        }
        metadata["request_sha256"] = self._hash(metadata["request"])
        if not self.config.api_key:
            metadata["error"] = {"type": "ModelRequestError", "code": "OCR_BAIDU_ACCESS_TOKEN_REQUIRED"}
            metadata["duration_seconds"] = round(time.perf_counter() - started, 6)
            raise ModelRequestError("OCR_BAIDU_ACCESS_TOKEN_REQUIRED", metadata)

        responses: list[Any] = []

        def remaining_timeout() -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ModelRequestError("OCR_BAIDU_POLL_TIMEOUT", metadata)
            return remaining

        def exchange(client, method: str, url: str, *, params=None, data=None,
                     expect_json: bool = False, text_only: bool = False):
            request = self._sanitize({
                "method": method, "url": self._audit_url(url), "query": params,
                "body_sha256": self._hash(self._sanitize(data)) if data is not None else None,
            })
            entry: dict[str, Any] = {"request": request, "status_code": None}
            metadata["http_exchanges"].append(entry)
            if method == "POST":
                response = client.post(url, params=params, headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                }, data=data, timeout=remaining_timeout())
            else:
                response = client.get(url, params=params, timeout=remaining_timeout())
            metadata["status_code"] = entry["status_code"] = getattr(response, "status_code", None)
            entry["response_headers"] = self._sanitize(dict(getattr(response, "headers", {})))
            invalid_json = False
            try:
                body = response.text if text_only else response.json()
            except ValueError:
                body = response.text
                invalid_json = True
            responses.append(self._sanitize(body))
            entry["response_index"] = len(responses) - 1
            response.raise_for_status()
            if expect_json and invalid_json:
                raise ModelRequestError("MODEL_INVALID_JSON_RESPONSE", metadata)
            return body

        try:
            with httpx.Client(timeout=self.config.timeout) as client:
                token = self.config.api_key
                if self.config.secret_key:
                    if not self._ocr_access_token or time.monotonic() >= self._ocr_token_expires_at:
                        base = urlsplit(self.config.base_url)
                        token_url = urlunsplit((base.scheme, base.netloc, "/oauth/2.0/token", "", ""))
                        authentication = exchange(client, "GET", token_url, params={
                            "grant_type": "client_credentials", "client_id": self.config.api_key,
                            "client_secret": self.config.secret_key,
                        }, expect_json=True)
                        access_token = authentication.get("access_token") if isinstance(authentication, dict) else None
                        if not isinstance(access_token, str) or not access_token.strip():
                            raise ModelRequestError("OCR_BAIDU_TOKEN_FAILED", metadata)
                        self._ocr_access_token = access_token
                        self._known_secrets.add(access_token)
                        try:
                            expires_in = float(authentication.get("expires_in", 0))
                        except (TypeError, ValueError):
                            expires_in = 0
                        if not math.isfinite(expires_in):
                            expires_in = 0
                        self._ocr_token_expires_at = time.monotonic() + max(expires_in - 30, 0)
                    token = self._ocr_access_token

                submit_data = exchange(client, "POST", submit_url, params={"access_token": token},
                                       data=request_body, expect_json=True)
                if not isinstance(submit_data, dict) or submit_data.get("error_code", 0) not in (0, None):
                    if isinstance(submit_data, dict) and submit_data.get("error_code") in {110, 111}:
                        self._ocr_token_expires_at = 0
                    raise ModelRequestError("OCR_BAIDU_SUBMIT_FAILED", metadata)
                task = submit_data.get("result", {})
                task_id = task.get("task_id") if isinstance(task, dict) else None
                if not isinstance(task_id, str) or not task_id.strip():
                    raise ModelRequestError("OCR_BAIDU_TASK_ID_MISSING", metadata)
                metadata["id"] = task_id

                result_data: dict[str, Any] | None = None
                while True:
                    query_data = exchange(client, "POST", query_url, params={"access_token": token},
                                          data={"task_id": task_id}, expect_json=True)
                    if not isinstance(query_data, dict) or query_data.get("error_code", 0) not in (0, None):
                        if isinstance(query_data, dict) and query_data.get("error_code") in {110, 111}:
                            self._ocr_token_expires_at = 0
                        raise ModelRequestError("OCR_BAIDU_QUERY_FAILED", metadata)
                    result = query_data.get("result", {})
                    status = result.get("status") if isinstance(result, dict) else None
                    if status in {"success", "completed"}:
                        result_data = result
                        break
                    if status == "failed":
                        raise ModelRequestError("OCR_BAIDU_TASK_FAILED", metadata)
                    if status not in {"pending", "running"}:
                        raise ModelRequestError("OCR_BAIDU_INVALID_TASK_STATUS", metadata)
                    time.sleep(min(max(self.config.poll_interval, 0.01), remaining_timeout()))

                text = ""
                if result_data:
                    parse_url = result_data.get("parse_result_url")
                    markdown_url = result_data.get("markdown_url")
                    if parse_url:
                        try:
                            parsed_data = exchange(client, "GET", parse_url, expect_json=True)
                            text = BaiduUnlimitedOCRAdapter.result_text(parsed_data)
                        except (httpx.HTTPError, ModelRequestError):
                            if not markdown_url:
                                raise
                    if not text and markdown_url:
                        text = exchange(client, "GET", markdown_url, text_only=True)
                if not text and result_data:
                    text = BaiduUnlimitedOCRAdapter.result_text(result_data)
            if not text.strip():
                raise ModelRequestError("MODEL_EMPTY_RESPONSE", metadata)
            remaining_timeout()
            metadata["response"] = self._sanitize({"baidu": responses})
            metadata["response_sha256"] = self._hash(metadata["response"])
            metadata["finish_reason"] = "stop"
            metadata["duration_seconds"] = round(time.perf_counter() - started, 6)
            return text, self._sanitize(metadata)
        except ModelRequestError as exc:
            metadata["response"] = self._sanitize({"baidu": responses})
            metadata["response_sha256"] = self._hash(metadata["response"])
            metadata["error"] = {"type": type(exc).__name__, "code": str(exc)}
            metadata["duration_seconds"] = round(time.perf_counter() - started, 6)
            raise ModelRequestError(str(exc), self._sanitize(metadata)) from exc
        except Exception as exc:
            metadata["response"] = self._sanitize({"baidu": responses})
            metadata["response_sha256"] = self._hash(metadata["response"])
            code = "MODEL_REQUEST_TIMEOUT" if isinstance(exc, httpx.TimeoutException) else "MODEL_REQUEST_FAILED"
            metadata["error"] = {"type": type(exc).__name__, "code": code}
            metadata["duration_seconds"] = round(time.perf_counter() - started, 6)
            raise ModelRequestError(code, self._sanitize(metadata)) from exc
        finally:
            metadata["duration_seconds"] = round(time.perf_counter() - started, 6)

    def vision(self, *, system: str, prompt: str, image: bytes, media_type: str = "image/png") -> tuple[str, dict[str, Any]]:
        if BaiduUnlimitedOCRAdapter.is_retired_ocr(self.config.provider, self.config.model):
            code = "OCR_LEGACY_PROVIDER_RETIRED_CONFIGURE_UNLIMITED_OCR"
            raise ModelRequestError(code, self._sanitize({
                "provider": self.config.provider, "model": self.config.model,
                "request": None, "response": None,
                "error": {"type": "ModelRequestError", "code": code},
            }))
        if BaiduUnlimitedOCRAdapter.matches(self.config.provider):
            if self._ocr_protocol() == "baidu_cloud":
                return self._baidu_cloud_vision(image=image)
            payload = BaiduUnlimitedOCRAdapter.openai_payload(
                model=self.config.model, prompt=prompt, image=image, media_type=media_type,
            )
            headers = {"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {}
            url = self.config.base_url.rstrip("/") + "/chat/completions"
            return self._request(url, headers, payload)
        encoded = base64.b64encode(image).decode("ascii")
        if self.config.provider == "anthropic":
            payload: dict[str, Any] = {
                "model": self.config.model,
                "max_tokens": 8192,
                "system": system,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": encoded}},
                        {"type": "text", "text": prompt},
                    ],
                }],
            }
            headers = {"x-api-key": self.config.api_key, "anthropic-version": "2023-06-01"}
            url = self.config.base_url.rstrip("/") + "/messages"
        else:
            image_part = {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{encoded}"},
            }
            payload = {
                "model": self.config.model,
                "messages": [{"role": "system", "content": system}, {
                    "role": "user", "content": [
                        {"type": "text", "text": prompt}, image_part,
                    ],
                }],
            }
            headers = {"Authorization": f"Bearer {self.config.api_key}"}
            url = self.config.base_url.rstrip("/") + "/chat/completions"
        return self._request(url, headers, payload)


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)

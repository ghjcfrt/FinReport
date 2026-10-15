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


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    base_url: str
    api_key: str
    model: str
    timeout: float = 60.0
    enable_thinking: bool | None = None
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("MODEL_INVALID_TIMEOUT")


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

    def __init__(self, config: ModelConfig):
        self.config = config

    def _sanitize(self, value: Any) -> Any:
        if isinstance(value, float) and not math.isfinite(value):
            return {"non_finite_number": str(value)}
        if isinstance(value, dict):
            sensitive = {"authorization", "xapikey", "apikey", "accesstoken", "refreshtoken",
                         "password", "secret", "clientsecret", "cookie", "setcookie"}
            return {
                str(key): "[REDACTED]" if str(key).lower().replace("-", "").replace("_", "") in sensitive
                else self._sanitize(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._sanitize(item) for item in value]
        if isinstance(value, str) and self.config.api_key:
            if value == self.config.api_key:
                return "[REDACTED]"
            if len(self.config.api_key) >= 8:
                return value.replace(self.config.api_key, "[REDACTED]")
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

    def _response(self, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        metadata = self._sanitize({
            "id": data.get("id"), "model": data.get("model", self.config.model),
            "usage": self._sanitize(data.get("usage")), "reasoning_output": self._reasoning(data),
        })
        if self.config.provider == "anthropic":
            finish = data.get("stop_reason")
            content = "".join(item.get("text", "") for item in data.get("content", [])
                              if item.get("type", "text") == "text")
        else:
            choices = data.get("choices", [])
            if not choices:
                raise ModelRequestError("MODEL_EMPTY_RESPONSE", metadata)
            finish = choices[0].get("finish_reason")
            content = choices[0].get("message", {}).get("content")
        metadata["finish_reason"] = finish
        if finish in {"length", "max_tokens"}:
            raise ModelRequestError("MODEL_OUTPUT_TRUNCATED", metadata)
        if finish in {"content_filter", "refusal"}:
            raise ModelRequestError("MODEL_OUTPUT_BLOCKED", metadata)
        if not isinstance(content, str) or not content.strip():
            raise ModelRequestError("MODEL_EMPTY_RESPONSE", metadata)
        return content, metadata

    def _request(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        started = time.perf_counter()
        request = self._sanitize({"method": "POST", "url": self._audit_url(url), "headers": headers, "body": payload})
        metadata: dict[str, Any] = {
            "provider": self.config.provider, "model": self.config.model,
            "started_at": datetime.now(UTC).isoformat(), "timeout_seconds": self.config.timeout,
            "request": request, "request_sha256": self._hash(request),
            "response": None, "response_sha256": None, "status_code": None,
            "usage": None, "reasoning_output": [], "hash_basis": "canonical_sanitized_json",
        }
        try:
            with httpx.Client(timeout=self.config.timeout) as client:
                response = client.post(url, headers=headers, json=payload)
                metadata["status_code"] = getattr(response, "status_code", None)
                response_headers = getattr(response, "headers", {})
                metadata["response_headers"] = self._sanitize(dict(response_headers))
                try:
                    data = response.json()
                except ValueError:
                    metadata["response"] = self._sanitize(getattr(response, "text", ""))
                    metadata["response_sha256"] = self._hash(metadata["response"])
                    response.raise_for_status()
                    raise ModelRequestError("MODEL_INVALID_JSON_RESPONSE", metadata)
                metadata["response"] = self._sanitize(data)
                metadata["response_sha256"] = self._hash(metadata["response"])
                response.raise_for_status()
            if not isinstance(data, dict):
                raise ModelRequestError("MODEL_INVALID_RESPONSE", metadata)
            content, normalized = self._response(data)
            metadata.update(normalized)
            return content, metadata
        except ModelRequestError as exc:
            metadata.update(exc.metadata)
            metadata["error"] = {"type": type(exc).__name__, "code": str(exc)}
            raise ModelRequestError(str(exc), metadata) from exc
        except Exception as exc:
            code = "MODEL_REQUEST_TIMEOUT" if isinstance(exc, httpx.TimeoutException) else "MODEL_REQUEST_FAILED"
            metadata["error"] = {"type": type(exc).__name__, "code": code}
            raise ModelRequestError(code, metadata) from exc
        finally:
            metadata["duration_seconds"] = round(time.perf_counter() - started, 6)

    def complete(self, *, system: str, user: str, response_json: bool = False,
                 response_schema: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
        if self.config.provider == "anthropic":
            payload: dict[str, Any] = {
                "model": self.config.model,
                "max_tokens": 8192,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
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
            if response_schema is not None:
                payload["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": "finreport_analysis", "strict": True, "schema": self._strict_schema(response_schema),
                }}
            elif response_json:
                payload["response_format"] = {"type": "json_object"}
            if self.config.enable_thinking is not None:
                payload["enable_thinking"] = self.config.enable_thinking
            if self.config.reasoning_effort:
                payload["reasoning_effort"] = self.config.reasoning_effort
            headers = {"Authorization": f"Bearer {self.config.api_key}"}
            url = self.config.base_url.rstrip("/") + "/chat/completions"
        return self._request(url, headers, payload)

    def vision(self, *, system: str, prompt: str, image: bytes, media_type: str = "image/png") -> tuple[str, dict[str, Any]]:
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
            if "deepseek-ocr" in self.config.model.lower():
                # DeepSeek-OCR uses its document prompt without a system role,
                # and expects the image before the prompt (official examples).
                payload = {
                    "model": self.config.model,
                    "messages": [{"role": "user", "content": [
                        image_part,
                        {"type": "text", "text": "<image>\n<|grounding|>Convert the document to markdown."},
                    ]}],
                    "temperature": 0, "max_tokens": 4096, "stream": False,
                }
            else:
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

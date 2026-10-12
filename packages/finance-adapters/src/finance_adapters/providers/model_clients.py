from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    base_url: str
    api_key: str
    model: str
    timeout: float = 60.0


class ModelClient:
    """Small protocol adapter for OpenAI and Anthropic APIs.

    Secrets stay in the server process. Callers only receive normalized text and
    response metadata, which keeps OCR and analysis auditable and provider-neutral.
    """

    def __init__(self, config: ModelConfig):
        self.config = config

    def _response(self, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if self.config.provider == "anthropic":
            finish = data.get("stop_reason")
            content = "".join(item.get("text", "") for item in data.get("content", []))
        else:
            choices = data.get("choices", [])
            if not choices:
                raise ValueError("MODEL_EMPTY_RESPONSE")
            finish = choices[0].get("finish_reason")
            content = choices[0].get("message", {}).get("content")
        if finish in {"length", "max_tokens"}:
            raise ValueError("MODEL_OUTPUT_TRUNCATED")
        if finish in {"content_filter", "refusal"}:
            raise ValueError("MODEL_OUTPUT_BLOCKED")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("MODEL_EMPTY_RESPONSE")
        return content, {
            "id": data.get("id"), "model": data.get("model", self.config.model),
            "finish_reason": finish,
        }

    def complete(self, *, system: str, user: str, response_json: bool = False) -> tuple[str, dict[str, Any]]:
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
            if response_json:
                payload["response_format"] = {"type": "json_object"}
            headers = {"Authorization": f"Bearer {self.config.api_key}"}
            url = self.config.base_url.rstrip("/") + "/chat/completions"
        with httpx.Client(timeout=self.config.timeout) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
        data = response.json()
        return self._response(data)

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
        with httpx.Client(timeout=self.config.timeout) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
        data = response.json()
        return self._response(data)


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)

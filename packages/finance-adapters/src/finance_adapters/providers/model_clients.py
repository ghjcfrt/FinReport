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
        if self.config.provider == "anthropic":
            content = data["content"][0]["text"]
        else:
            content = data["choices"][0]["message"]["content"]
        return content, {"id": data.get("id"), "model": data.get("model", self.config.model)}

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
            payload = {
                "model": self.config.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{encoded}"}},
                ]}],
            }
            headers = {"Authorization": f"Bearer {self.config.api_key}"}
            url = self.config.base_url.rstrip("/") + "/chat/completions"
        with httpx.Client(timeout=self.config.timeout) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
        data = response.json()
        content = data["content"][0]["text"] if self.config.provider == "anthropic" else data["choices"][0]["message"]["content"]
        return content, {"id": data.get("id"), "model": data.get("model", self.config.model)}


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)

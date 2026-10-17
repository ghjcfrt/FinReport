"""Local OpenAI-compatible Unlimited-OCR service.

The service intentionally keeps the official Transformers remote-code model
behind a small HTTP boundary so FinReport can use the same OpenAI OCR path.
It does not fabricate OCR: /v1/chat/completions returns 503 until the model
has loaded successfully.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

MODEL_NAME = os.environ.get("UNLIMITED_OCR_MODEL", "baidu/Unlimited-OCR")
MODEL_DIR = os.environ.get("UNLIMITED_OCR_MODEL_DIR", "")
app = FastAPI(title="Unlimited-OCR local", version="0.1.0")
MODEL = None
TOKENIZER = None
LOAD_ERROR = ""
LOAD_SECONDS = None
INFERENCE_LOCK = threading.Lock()
QUANTIZATION = os.environ.get("UNLIMITED_OCR_QUANTIZATION", "nf4")
EXPECTED_REVISION = "8249d20b2822a7d8a4c22c8d8c9069b5ac36bda3"
EXPECTED_MODEL_SHA256 = "2bc48a7a110061ea58fff65d3169367eebe3aee371ca6968dc2219c1b2855fc6"
MODEL_REVISION = None
MODEL_SHA256 = None


class CompletionRequest(BaseModel):
    model: str = "Unlimited-OCR"
    messages: list[dict]
    max_tokens: int = 8192
    temperature: float = 0
    stream: bool = False


def _image_from_messages(messages: list[dict]) -> tuple[str, bytes]:
    images = []
    for message in messages:
        content = message.get("content", [])
        if isinstance(content, str):
            continue
        for item in content:
            if item.get("type") != "image_url":
                continue
            url = item.get("image_url", {}).get("url", "")
            if not url.startswith("data:") or ";base64," not in url:
                raise HTTPException(400, "only data:image/* base64 URLs are supported")
            header, encoded = url.split(",", 1)
            try:
                images.append((header.split(";", 1)[0][5:], base64.b64decode(encoded, validate=True)))
            except ValueError as exc:
                raise HTTPException(400, "invalid base64 image") from exc
    if len(images) > 1:
        raise HTTPException(400, "submit exactly one page image per request")
    if images:
        return images[0]
    raise HTTPException(400, "an image_url content item is required")


def load_model() -> None:
    global MODEL, TOKENIZER, LOAD_ERROR, LOAD_SECONDS, MODEL_REVISION, MODEL_SHA256
    started = time.perf_counter()
    try:
        import torch
        from transformers import AutoModel, AutoTokenizer, BitsAndBytesConfig

        source = MODEL_DIR or MODEL_NAME
        if MODEL_DIR:
            model_path = Path(MODEL_DIR)
            manifest_path = model_path.parent / "evidence" / "artifact-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            weight_path = model_path / "model.safetensors"
            digest = hashlib.sha256()
            with weight_path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                    digest.update(chunk)
            MODEL_REVISION = manifest.get("revision")
            MODEL_SHA256 = digest.hexdigest()
            if manifest.get("model_id") != "baidu/Unlimited-OCR" or MODEL_REVISION != EXPECTED_REVISION or MODEL_SHA256 != EXPECTED_MODEL_SHA256:
                raise ValueError("local checkpoint identity mismatch")
        TOKENIZER = AutoTokenizer.from_pretrained(source, trust_remote_code=True)
        kwargs = {}
        if QUANTIZATION == "nf4":
            kwargs.update(quantization_config=BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=torch.bfloat16), device_map={"": 0})
        candidate = AutoModel.from_pretrained(
            source, trust_remote_code=True, use_safetensors=True,
            torch_dtype=torch.bfloat16, low_cpu_mem_usage=True,
            attn_implementation="eager", **kwargs,
        ).eval()
        if QUANTIZATION != "nf4":
            candidate = candidate.cuda()
        MODEL = candidate
        LOAD_SECONDS = round(time.perf_counter() - started, 3)
    except Exception as exc:  # noqa: BLE001 - health endpoint retains model load failures
        LOAD_ERROR = f"{type(exc).__name__}: {exc}"
        LOAD_SECONDS = round(time.perf_counter() - started, 3)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ready" if MODEL is not None else "error" if LOAD_ERROR else "loading",
        "model": MODEL_NAME,
        "model_dir": MODEL_DIR or None,
        "model_revision": MODEL_REVISION,
        "model_sha256": MODEL_SHA256,
        "backend": f"transformers-cuda-{QUANTIZATION}",
        "load_seconds": LOAD_SECONDS,
        "error": LOAD_ERROR or None,
    }


@app.get("/v1/models")
def models() -> dict:
    return {"object": "list", "data": [{"id": "Unlimited-OCR", "object": "model", "owned_by": "baidu"}]}


@app.post("/v1/chat/completions")
def completion(request: CompletionRequest) -> dict:
    if MODEL is None:
        raise HTTPException(503, detail={"code": "MODEL_NOT_READY", "error": LOAD_ERROR or "model loading"})
    if request.stream:
        raise HTTPException(400, "streaming is not supported by this single-page service")
    if request.model not in {"Unlimited-OCR", "baidu/Unlimited-OCR"}:
        raise HTTPException(400, "unknown model")
    media_type, image = _image_from_messages(request.messages)
    if media_type not in {"image/png", "image/jpeg", "image/jpg", "image/webp"}:
        raise HTTPException(400, "unsupported image type")
    # Keep the model's documented OCR prompt; finance formatting belongs to
    # FinReport's downstream parser and must not change the checkpoint task.
    prompt = "<image>document parsing."
    started = time.perf_counter()
    import torch
    with INFERENCE_LOCK:
        MODEL.generation_config.max_new_tokens = min(max(request.max_tokens, 64), 8192)
        torch.cuda.reset_peak_memory_stats()
        try:
            default_temp_root = Path(__file__).resolve().parents[2] / "data" / "local-services" / "unlimited-ocr" / "tmp"
            temp_root = Path(os.environ.get("UNLIMITED_OCR_TMP", default_temp_root))
            temp_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="request-", dir=temp_root) as request_dir:
                image_path = Path(request_dir) / "page.png"
                image_path.write_bytes(image)
                output = MODEL.infer(TOKENIZER, prompt=prompt, image_file=str(image_path), output_path=request_dir, eval_mode=True, max_length=min(max(request.max_tokens, 64), 12288), no_repeat_ngram_size=35, ngram_window=128)
        except Exception as exc:
            raise HTTPException(500, detail={"code": "INFERENCE_FAILED", "error": f"{type(exc).__name__}: {exc}"}) from exc
        peak_memory = torch.cuda.max_memory_allocated()
    elapsed = round(time.perf_counter() - started, 3)
    completion_tokens = len(TOKENIZER.encode(output))
    finish_reason = "length" if completion_tokens >= min(max(request.max_tokens, 64), 8192) else "stop"
    return {"id": f"unlimited-ocr-{uuid.uuid4().hex}", "object": "chat.completion", "model": request.model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": output}, "finish_reason": finish_reason}],
            "usage": {"completion_tokens": completion_tokens}, "x_local_elapsed_seconds": elapsed,
            "x_local_cuda_peak_memory_bytes": peak_memory, "x_local_quantization": QUANTIZATION}


if __name__ == "__main__":
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    threading.Thread(target=load_model, daemon=True).start()
    uvicorn.run(app, host=args.host, port=args.port)

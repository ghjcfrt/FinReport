"""Download the pinned official checkpoint, verify it, apply one NF4 dtype fix."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import requests

REVISION = "8249d20b2822a7d8a4c22c8d8c9069b5ac36bda3"
MODEL_SHA256 = "2bc48a7a110061ea58fff65d3169367eebe3aee371ca6968dc2219c1b2855fc6"
FILES = ["config.json", "configuration_deepseek_v2.py", "conversation.py", "deepencoder.py", "modeling_deepseekv2.py", "modeling_unlimitedocr.py", "generation_config.json", "model.safetensors", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "LICENSE"]
ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "data/local-services/unlimited-ocr/model"
EVIDENCE = ROOT / "data/local-services/unlimited-ocr/evidence"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    MODEL.mkdir(parents=True, exist_ok=True)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        path = MODEL / name
        if path.exists():
            continue
        response = requests.get(f"https://huggingface.co/baidu/Unlimited-OCR/resolve/{REVISION}/{name}", stream=True, timeout=120)
        response.raise_for_status()
        partial = path.with_suffix(path.suffix + ".part")
        with partial.open("wb") as target:
            for chunk in response.iter_content(8 * 1024 * 1024):
                target.write(chunk)
        partial.replace(path)
        print(f"downloaded {name}", flush=True)
    actual = sha256(MODEL / "model.safetensors")
    if actual != MODEL_SHA256:
        raise ValueError(f"checkpoint SHA256 mismatch: {actual}")
    (EVIDENCE / "model.sha256").write_text(actual + "\n", encoding="utf-8")
    code = MODEL / "modeling_unlimitedocr.py"
    original = code.read_text(encoding="utf-8")
    marker = "                    inputs_embeds[idx].masked_scatter_(images_seq_mask[idx].unsqueeze(-1).cuda(), images_in_this_batch)"
    replacement = "                    images_in_this_batch = images_in_this_batch.to(inputs_embeds.dtype)\n" + marker
    if "images_in_this_batch = images_in_this_batch.to(inputs_embeds.dtype)" not in original:
        if original.count(marker) != 1:
            raise ValueError("pinned remote-code scatter boundary not found exactly once")
        (EVIDENCE / "modeling_unlimitedocr.original.py").write_text(original, encoding="utf-8")
        code.write_text(original.replace(marker, replacement), encoding="utf-8")
    manifest = {"model_id": "baidu/Unlimited-OCR", "revision": REVISION, "quantization": "nf4", "license": "MIT", "source_patch": "cast vision features to input embedding dtype before masked_scatter_", "files": {p.name: {"bytes": p.stat().st_size, "sha256": sha256(p)} for p in sorted(MODEL.iterdir()) if p.is_file()}}
    (EVIDENCE / "artifact-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("checkpoint verified; NF4 dtype patch applied", flush=True)


if __name__ == "__main__":
    main()

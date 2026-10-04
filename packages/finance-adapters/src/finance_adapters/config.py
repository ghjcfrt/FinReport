from __future__ import annotations

import os
from pathlib import Path


class Settings:
    database_url = os.getenv("FINREPORT_DATABASE_URL", "sqlite:///./data/finreport.db")
    storage_dir = Path(os.getenv("FINREPORT_STORAGE_DIR", "./data/storage"))
    max_upload_bytes = int(os.getenv("FINREPORT_MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
    llm_provider = os.getenv("FINREPORT_LLM_PROVIDER", "openai")
    llm_base_url = os.getenv("FINREPORT_LLM_BASE_URL")
    llm_api_key = os.getenv("FINREPORT_LLM_API_KEY")
    llm_model = os.getenv("FINREPORT_LLM_MODEL")
    ocr_provider = os.getenv("FINREPORT_OCR_PROVIDER", "openai")
    ocr_base_url = os.getenv("FINREPORT_OCR_BASE_URL", llm_base_url or "")
    ocr_api_key = os.getenv("FINREPORT_OCR_API_KEY", llm_api_key or "")
    ocr_model = os.getenv("FINREPORT_OCR_MODEL", "")
    ocr_timeout_seconds = float(os.getenv("FINREPORT_OCR_TIMEOUT_SECONDS", "60"))
    ocr_max_retries = int(os.getenv("FINREPORT_OCR_MAX_RETRIES", "2"))
    ocr_max_page_bytes = int(os.getenv("FINREPORT_OCR_MAX_PAGE_BYTES", str(8 * 1024 * 1024)))
    task_backend = os.getenv("FINREPORT_TASK_BACKEND", "background")
    api_key = os.getenv("FINREPORT_API_KEY")
    storage_backend = os.getenv("FINREPORT_STORAGE_BACKEND", "local")
    s3_endpoint_url = os.getenv("FINREPORT_S3_ENDPOINT_URL")
    s3_bucket = os.getenv("FINREPORT_S3_BUCKET")
    s3_access_key = os.getenv("FINREPORT_S3_ACCESS_KEY")
    s3_secret_key = os.getenv("FINREPORT_S3_SECRET_KEY")
    av_scanner_executable = os.getenv("FINREPORT_AV_SCANNER_EXECUTABLE")
    av_scan_timeout_seconds = float(os.getenv("FINREPORT_AV_SCAN_TIMEOUT_SECONDS", "60"))
    av_mode = os.getenv("FINREPORT_AV_MODE", "supervisor")
    av_native_probe_executable = os.getenv("FINREPORT_AV_NATIVE_PROBE_EXECUTABLE")
    av_native_shim = os.getenv("FINREPORT_AV_NATIVE_SHIM")
    av_native_database = os.getenv("FINREPORT_AV_NATIVE_DATABASE")
    av_native_certs = os.getenv("FINREPORT_AV_NATIVE_CERTS")

    def update_model(self, stage: str, *, provider: str, base_url: str, api_key: str, model: str) -> None:
        if stage == "analysis":
            self.llm_provider, self.llm_base_url, self.llm_api_key, self.llm_model = provider, base_url, api_key, model
        elif stage == "ocr":
            self.ocr_provider, self.ocr_base_url, self.ocr_api_key, self.ocr_model = provider, base_url, api_key, model
        else:
            raise ValueError("UNKNOWN_MODEL_STAGE")


settings = Settings()

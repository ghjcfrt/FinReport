from __future__ import annotations

import math
import os
from pathlib import Path

from dotenv import load_dotenv


def _load_environment() -> None:
    # Resolve from the source checkout, so launchers and arbitrary working
    # directories share one configuration. Explicit process values take priority.
    configured = os.getenv("FINREPORT_ENV_FILE")
    if configured:
        load_dotenv(Path(configured), override=False)
        return
    for root in Path(__file__).resolve().parents:
        if (root / "apps").is_dir() and (root / "packages").is_dir():
            load_dotenv(root / ".env", override=False)
            return


_load_environment()


def _env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean environment variable using explicit, human-friendly values."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_optional_bool(name: str) -> bool | None:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return None
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _env_positive_float(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


def _env_structured_output_mode() -> str:
    value = os.getenv("FINREPORT_LLM_STRUCTURED_OUTPUT_MODE", "json_object").strip().lower()
    if value not in {"json_object", "json_schema"}:
        raise ValueError("FINREPORT_LLM_STRUCTURED_OUTPUT_MODE must be json_object or json_schema")
    return value


def _env_ocr_protocol() -> str:
    value = os.getenv("FINREPORT_OCR_PROTOCOL", "auto").strip().lower().replace("-", "_")
    if value not in {"auto", "openai_compatible", "baidu_cloud", "local", "http", "cloud"}:
        raise ValueError(
            "FINREPORT_OCR_PROTOCOL must be auto, openai_compatible, baidu_cloud, local, http or cloud"
        )
    return value


class Settings:
    database_url = os.getenv("FINREPORT_DATABASE_URL", "sqlite:///./data/finreport.db")
    storage_dir = Path(os.getenv("FINREPORT_STORAGE_DIR", "./data/storage"))
    max_upload_bytes = int(os.getenv("FINREPORT_MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
    llm_provider = os.getenv("FINREPORT_LLM_PROVIDER", "openai")
    llm_base_url = os.getenv("FINREPORT_LLM_BASE_URL")
    llm_api_key = os.getenv("FINREPORT_LLM_API_KEY")
    llm_model = os.getenv("FINREPORT_LLM_MODEL")
    llm_timeout_seconds = _env_positive_float("FINREPORT_LLM_TIMEOUT_SECONDS", 120.0)
    llm_enable_thinking = _env_optional_bool("FINREPORT_LLM_ENABLE_THINKING")
    llm_reasoning_effort = os.getenv("FINREPORT_LLM_REASONING_EFFORT", "").strip() or None
    llm_stream = _env_optional_bool("FINREPORT_LLM_STREAM") or False
    llm_provider_sort = os.getenv("FINREPORT_LLM_PROVIDER_SORT", "").strip() or None
    llm_structured_output_mode = _env_structured_output_mode()
    # Bound actions, not tokens. Each accepted round is checkpointed for resume.
    analysis_agent_max_rounds = int(os.getenv("FINREPORT_ANALYSIS_AGENT_MAX_ROUNDS", "16"))
    ocr_provider = os.getenv("FINREPORT_OCR_PROVIDER", "baidu-unlimited-ocr")
    # OCR credentials are independent of the analysis provider.  A missing OCR
    # endpoint must never send financial-report images to the analysis service.
    ocr_base_url = os.getenv("FINREPORT_OCR_BASE_URL", "")
    ocr_api_key = os.getenv("FINREPORT_OCR_API_KEY", "")
    ocr_secret_key = os.getenv("FINREPORT_OCR_SECRET_KEY", "")
    ocr_model = os.getenv("FINREPORT_OCR_MODEL", "baidu/Unlimited-OCR")
    ocr_protocol = _env_ocr_protocol()
    ocr_prompt = os.getenv("FINREPORT_OCR_PROMPT", "<image>document parsing.")
    ocr_timeout_seconds = _env_positive_float("FINREPORT_OCR_TIMEOUT_SECONDS", 120.0)
    ocr_poll_interval_seconds = _env_positive_float("FINREPORT_OCR_POLL_INTERVAL_SECONDS", 5.0)
    ocr_max_retries = int(os.getenv("FINREPORT_OCR_MAX_RETRIES", "2"))
    ocr_max_page_bytes = int(os.getenv("FINREPORT_OCR_MAX_PAGE_BYTES", str(8 * 1024 * 1024)))
    # Evidence retrieval uses Qdrant when explicitly configured.  The
    # application never depends on the service being reachable: the retrieval
    # adapter falls back to deterministic SQLite ranking for local and offline
    # runs.  A fixed hashed-vector dimension keeps indexing reproducible even
    # when no external embedding service is installed.
    qdrant_url = os.getenv("FINREPORT_QDRANT_URL", "").rstrip("/")
    qdrant_api_key = os.getenv("FINREPORT_QDRANT_API_KEY", "")
    qdrant_collection = os.getenv("FINREPORT_QDRANT_COLLECTION", "finreport_document_chunks")
    qdrant_timeout_seconds = _env_positive_float("FINREPORT_QDRANT_TIMEOUT_SECONDS", 5.0)
    qdrant_vector_size = int(os.getenv("FINREPORT_QDRANT_VECTOR_SIZE", "256"))
    # Preferred protocol: the official mcp-server-qdrant streamable HTTP
    # endpoint.  The REST URL above is a separately named lexical-vector
    # adapter, never silently described as MCP or semantic search.
    qdrant_mcp_url = os.getenv("FINREPORT_QDRANT_MCP_URL", "").rstrip("/")
    qdrant_mcp_token = os.getenv("FINREPORT_QDRANT_MCP_TOKEN", "")
    qdrant_enabled = _env_bool("FINREPORT_QDRANT_ENABLED", bool(qdrant_mcp_url or qdrant_url))
    docling_enabled = _env_bool("FINREPORT_DOCLING_ENABLED", False)
    docling_mcp_url = os.getenv("FINREPORT_DOCLING_MCP_URL", "http://127.0.0.1:18769/mcp")
    docling_mcp_timeout_seconds = _env_positive_float("FINREPORT_DOCLING_MCP_TIMEOUT_SECONDS", 300.0)
    docling_artifact_dir = Path(os.getenv("FINREPORT_DOCLING_ARTIFACT_DIR", "data/local-services/mcp/docling-artifacts"))
    docling_mcp_cache_dir = Path(os.getenv("FINREPORT_DOCLING_MCP_CACHE_DIR", "data/local-services/mcp/docling-output"))
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
    # Mock scanning is test-only and must be explicitly enabled.  Keeping this
    # disabled by default prevents a deployment from silently promoting an
    # upload without an antivirus result when AV mode is misconfigured.
    allow_mock_scanner = _env_bool("FINREPORT_ALLOW_MOCK_SCANNER", False)
    av_native_probe_executable = os.getenv("FINREPORT_AV_NATIVE_PROBE_EXECUTABLE")
    av_native_shim = os.getenv("FINREPORT_AV_NATIVE_SHIM")
    av_native_database = os.getenv("FINREPORT_AV_NATIVE_DATABASE")
    av_native_certs = os.getenv("FINREPORT_AV_NATIVE_CERTS")
    # Virus-database updates are opt-in. The updater always stages a new
    # immutable version and requires official signatures plus a native probe
    # before swapping current.json.
    av_update_enabled = _env_bool("FINREPORT_AV_UPDATE_ENABLED", False)
    av_update_script = os.getenv("FINREPORT_AV_UPDATE_SCRIPT")
    av_update_freshclam = os.getenv("FINREPORT_AV_UPDATE_FRESHCLAM")
    av_update_config = os.getenv("FINREPORT_AV_UPDATE_CONFIG")
    av_update_sigtool = os.getenv("FINREPORT_AV_UPDATE_SIGTOOL")
    av_update_signature_certs = os.getenv("FINREPORT_AV_UPDATE_SIGNATURE_CERTS")
    av_update_probe_executable = os.getenv("FINREPORT_AV_UPDATE_PROBE_EXECUTABLE")
    av_update_probe_file = os.getenv("FINREPORT_AV_UPDATE_PROBE_FILE")
    av_update_timeout_seconds = float(os.getenv("FINREPORT_AV_UPDATE_TIMEOUT_SECONDS", "900"))
    # The production Scanner Supervisor listens on a Windows named pipe.  Keep
    # the protocol settings in configuration so the API process never needs to
    # know how the Supervisor is hosted.
    av_supervisor_pipe = os.getenv("FINREPORT_SCANNER_PIPE", r"\\.\pipe\FinReport.Scanner.v1")
    av_supervisor_poll_interval_seconds = float(
        os.getenv("FINREPORT_SCANNER_POLL_INTERVAL_SECONDS", "0.10")
    )
    av_supervisor_connect_timeout_seconds = float(
        os.getenv("FINREPORT_SCANNER_CONNECT_TIMEOUT_SECONDS", "2")
    )

    def update_model(self, stage: str, *, provider: str, base_url: str, api_key: str, model: str) -> None:
        if stage == "analysis":
            self.llm_provider, self.llm_base_url, self.llm_api_key, self.llm_model = provider, base_url, api_key, model
        elif stage == "ocr":
            self.ocr_provider, self.ocr_base_url, self.ocr_api_key, self.ocr_model = provider, base_url, api_key, model
        else:
            raise ValueError("UNKNOWN_MODEL_STAGE")


settings = Settings()

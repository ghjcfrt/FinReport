"""Run the pinned official Qdrant MCP server with a CPU FastEmbed model."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

from mcp_server_qdrant.embeddings.fastembed import FastEmbedProvider
from mcp_server_qdrant.mcp_server import QdrantMCPServer
from mcp_server_qdrant.settings import QdrantSettings, ToolSettings
from qdrant_client import models

ROOT = Path(__file__).resolve().parents[2]
data_dir = ROOT / "data" / "local-services" / "mcp" / "qdrant-data"
data_dir.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("QDRANT_LOCAL_PATH", str(data_dir))
os.environ.setdefault("COLLECTION_NAME", "finreport_document_chunks")
os.environ.setdefault("QDRANT_ALLOW_ARBITRARY_FILTER", "true")
os.environ.setdefault("QDRANT_SEARCH_LIMIT", "30")
os.environ.setdefault("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
os.environ.setdefault("FASTEMBED_CACHE_PATH", str(ROOT / "data/local-services/mcp/models/fastembed"))
os.environ.setdefault("HF_HOME", str(ROOT / "data/local-services/mcp/models/huggingface"))

settings = QdrantSettings()
server = QdrantMCPServer(
    tool_settings=ToolSettings(),
    qdrant_settings=settings,
    embedding_provider=FastEmbedProvider(os.environ["EMBEDDING_MODEL"]),
)

generation_file = data_dir / "finreport-index-generation.json"
if not generation_file.is_file():
    generation_file.write_text(json.dumps({"generation": str(uuid.uuid4())}), encoding="utf-8")


@server.tool(name="finreport-index-status")
def index_status() -> dict:
    """Return deployment identity for audited rebuild and model provenance."""
    return {"generation": json.loads(generation_file.read_text(encoding="utf-8"))["generation"],
            "model": os.environ["EMBEDDING_MODEL"], "provider": "FastEmbedProvider",
            "vector_name": server.embedding_provider.get_vector_name(),
            "vector_size": server.embedding_provider.get_vector_size(),
            "collection": settings.collection_name, "device": "cpu",
            "implementation": "mcp-server-qdrant-0.8.1"}


@server.tool(name="finreport-store")
async def store_evidence(information: str, metadata: dict) -> str:
    """Idempotently store a FinReport SQLite evidence snapshot."""
    required = {"filing_version_id", "chunk_id", "chunk_key", "source_sha256", "text_sha256"}
    if not required.issubset(metadata):
        raise ValueError("FINREPORT_EVIDENCE_METADATA_REQUIRED")
    if hashlib.sha256(information.encode("utf-8")).hexdigest() != metadata["text_sha256"]:
        raise ValueError("FINREPORT_TEXT_HASH_MISMATCH")
    collection = settings.collection_name
    connector = server.qdrant_connector
    await connector._ensure_collection_exists(collection)
    embeddings = await server.embedding_provider.embed_documents([information])
    point_key = json.dumps([collection, metadata["filing_version_id"], metadata["chunk_key"],
                            metadata["source_sha256"], metadata["text_sha256"]], sort_keys=True)
    point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, point_key))
    await connector._client.upsert(collection_name=collection, wait=True, points=[models.PointStruct(
        id=point_id, vector={server.embedding_provider.get_vector_name(): embeddings[0]},
        payload={"document": information, "metadata": metadata},
    )])
    return point_id


@server.tool(name="finreport-index-count")
async def index_count(filing_version_id: int | None = None) -> dict:
    """Count local index points for deployment verification; no document reads."""
    client = server.qdrant_connector._client
    if not await client.collection_exists(settings.collection_name):
        return {"count": 0}
    query_filter = models.Filter(must=[models.FieldCondition(key="metadata.filing_version_id",
                    match=models.MatchValue(value=filing_version_id))]) if filing_version_id else None
    result = await client.count(collection_name=settings.collection_name, count_filter=query_filter, exact=True)
    return {"count": result.count}

server.run(transport="streamable-http", host="127.0.0.1", port=int(os.getenv("MCP_QDRANT_PORT", "18768")))

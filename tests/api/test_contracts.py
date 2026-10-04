import httpx
from fastapi.testclient import TestClient
from finreport_api.main import app


def test_model_settings_exposes_provider_shapes_without_secrets():
    with TestClient(app) as client:
        response = client.get("/api/v1/settings/models")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["supported_providers"] == ["openai", "anthropic"]
    assert "api_key" not in str(data).lower()


def test_health_response_contains_reproducibility_metadata():
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["request_id"]
    assert body["data_as_of"]
    assert body["rule_version"]


def test_model_discovery_uses_openai_models_endpoint(monkeypatch):
    def fake_get(url, *, headers, timeout):
        assert url == "https://api.example.com/v1/models"
        assert headers == {"Authorization": "Bearer test-key"}
        assert timeout == 15.0
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={"data": [{"id": "model-b"}, {"id": "model-a"}]},
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/settings/models/analysis/discover",
            json={"provider": "openai", "base_url": "https://api.example.com/v1", "api_key": "test-key"},
        )
    assert response.status_code == 200
    assert response.json()["data"]["models"] == ["model-a", "model-b"]

from finance_adapters.providers.model_clients import ModelClient, ModelConfig


def test_openai_payload_shape(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"id": "x", "model": "m", "choices": [{"message": {"content": "ok"}}]}

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers, json):
            captured.update(url=url, headers=headers, json=json)
            return Response()

    monkeypatch.setattr("httpx.Client", Client)
    text, _ = ModelClient(ModelConfig("openai", "https://example/v1", "k", "m")).complete(system="s", user="u", response_json=True)
    assert text == "ok"
    assert captured["url"].endswith("/chat/completions")
    assert captured["json"]["response_format"] == {"type": "json_object"}


def test_anthropic_payload_shape(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"id": "x", "model": "m", "content": [{"text": "ok"}]}

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers, json):
            captured.update(url=url, headers=headers, json=json)
            return Response()

    monkeypatch.setattr("httpx.Client", Client)
    text, _ = ModelClient(ModelConfig("anthropic", "https://example/v1", "k", "m")).complete(system="s", user="u")
    assert text == "ok"
    assert captured["url"].endswith("/messages")
    assert captured["headers"]["x-api-key"] == "k"

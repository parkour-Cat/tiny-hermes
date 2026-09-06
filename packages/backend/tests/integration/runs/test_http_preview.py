from fastapi.testclient import TestClient

from .http_tool_support import document


def test_preview_parses_operations_without_registering(client: TestClient, scope: dict[str, str]) -> None:
    before = client.get("/api/v1/http-tools", headers=scope).json()
    response = client.post("/api/v1/http-tools/preview", headers=scope, json={"document": document()})
    assert response.status_code == 200, response.text
    operations = {item["operation_id"]: item for item in response.json()["operations"]}
    assert operations["listOrders"]["read_only"] is True
    assert operations["createOrder"]["read_only"] is False
    assert client.get("/api/v1/http-tools", headers=scope).json() == before
    refused = client.post("/api/v1/http-tools/preview", headers=scope, json={"document": "{}"})
    assert refused.status_code == 422


from uuid import uuid4

from fastapi.testclient import TestClient


def test_a_submitted_run_exposes_the_user_message_over_http(
    client: TestClient, scope: dict[str, str], session_id: str, submitted_run: dict[str, object]
) -> None:
    del submitted_run
    messages = client.get(f"/api/v1/sessions/{session_id}/messages", headers=scope)
    assert messages.status_code == 200
    body = messages.json()
    assert body[0]["role"] == "user"
    assert body[0]["parts"][0]["type"] == "text"
    assert body[0]["parts"][0]["text"] == "do the thing"


def test_unknown_session_messages_are_a_generic_not_found(
    client: TestClient, scope: dict[str, str]
) -> None:
    missing = client.get(f"/api/v1/sessions/{uuid4()}/messages", headers=scope)
    assert missing.status_code == 404
    assert missing.json()["code"] == "session_not_found"

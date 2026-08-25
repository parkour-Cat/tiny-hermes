"""Editing what an endpoint says about itself.

An endpoint could be registered and disabled and nothing else. Somebody
registered a vision endpoint, left the image switch off, and had no way to
say so afterwards — the console offers 测试连接 / 设定价格 / 停用 and no
edit. It is the gap the channel bindings had, with the same cost: a correct
value reachable only by starting over.
"""

import pytest
from fastapi.testclient import TestClient


def test_an_endpoint_can_be_told_it_accepts_images(
    client: TestClient, admin_csrf: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODEL_KEY", "k")
    """An endpoint could be registered and disabled and nothing else.

    A person registered a vision endpoint, left the switch off, and had no
    way to say so afterwards — the console offers 测试连接 / 设定价格 / 停用
    and no edit. The same gap the channel bindings had, and the same cost:
    a correct value that can only be reached by deleting and starting over.

    Only this field. Changing `model` or `base_url` is swapping the endpoint
    for a different one underneath every AgentVersion that named it, and
    that is a new registration, not an edit. Whether it accepts images is a
    statement about the endpoint that was already true or already false.
    """
    created = client.post(
        "/api/v1/model-endpoints",
        headers={"X-CSRF-Token": admin_csrf},
        json={
            "name": "vision-edit",
            "base_url": "https://api.example.com/v1",
            "model": "deepseek-v4-flash-vision-exp",
            "context_window": 128000,
            "max_output_tokens": 4096,
            "usage_quality": "provider",
            "credential_ref": "MODEL_KEY",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["accepts_images"] is False

    updated = client.patch(
        f"/api/v1/model-endpoints/{created.json()['id']}",
        headers={"X-CSRF-Token": admin_csrf},
        json={"accepts_images": True},
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["accepts_images"] is True


def test_the_status_patch_still_works_on_its_own(
    client: TestClient, admin_csrf: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODEL_KEY", "k")
    """The field that was already there. A request naming only `status` must
    not have its image declaration reset to the default underneath it."""
    created = client.post(
        "/api/v1/model-endpoints",
        headers={"X-CSRF-Token": admin_csrf},
        json={
            "name": "vision-status",
            "base_url": "https://api.example.com/v1",
            "model": "deepseek-v4-flash-vision-exp",
            "context_window": 128000,
            "max_output_tokens": 4096,
            "usage_quality": "provider",
            "credential_ref": "MODEL_KEY",
            "accepts_images": True,
        },
    )
    assert created.status_code == 201, created.text
    endpoint_id = created.json()["id"]

    disabled = client.patch(
        f"/api/v1/model-endpoints/{endpoint_id}",
        headers={"X-CSRF-Token": admin_csrf},
        json={"status": "disabled"},
    )

    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["status"] == "disabled"
    assert disabled.json()["accepts_images"] is True

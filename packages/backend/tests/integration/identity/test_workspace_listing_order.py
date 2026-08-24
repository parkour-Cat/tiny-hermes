"""Which end of the workspace list a new one lands on.

The list is a picker, and the workspace somebody just made is the one they
are about to open. Oldest-first put it at the bottom, below every workspace
an e2e run ever created — on a real installation the entry you want is the
one you have to scroll for, and the ones you will never open again are the
ones at eye level.
"""

from fastapi.testclient import TestClient


def _create(client: TestClient, csrf: str, name: str) -> str:
    created = client.post(
        "/api/v1/workspaces", headers={"X-CSRF-Token": csrf}, json={"name": name}
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def test_the_newest_workspace_is_first(client: TestClient, admin_csrf: str) -> None:
    first = _create(client, admin_csrf, "Older")
    second = _create(client, admin_csrf, "Newer")

    listed = client.get("/api/v1/workspaces", headers={"X-CSRF-Token": admin_csrf})

    assert listed.status_code == 200, listed.text
    ids = [item["id"] for item in listed.json()]
    assert ids.index(second) < ids.index(first)


def test_two_created_in_the_same_instant_still_have_one_order(
    client: TestClient, admin_csrf: str
) -> None:
    """`created_at` ties are real — two rows can share a timestamp — and an
    order left to the planner puts the list in a different shape on every
    read, which is worse than either order."""
    names = [_create(client, admin_csrf, f"W{index}") for index in range(5)]

    once = [
        item["id"]
        for item in client.get(
            "/api/v1/workspaces", headers={"X-CSRF-Token": admin_csrf}
        ).json()
    ]
    twice = [
        item["id"]
        for item in client.get(
            "/api/v1/workspaces", headers={"X-CSRF-Token": admin_csrf}
        ).json()
    ]

    assert once == twice
    assert set(names) <= set(once)

"""Creating the row that makes a channel exist.

Everything else in this module — signature verification, decryption, the
exactly-once claim, ingestion into a Run, the blocked notice — has worked
since the Feishu channel landed, and until this route the **only** thing
that ever inserted a `channel_bindings` row was a test. The whole transport
was reachable by writing a row into Postgres by hand and no other way.
§20.1 names Channels as an M3 navigation entry; there was nothing for it to
navigate to.

§4.6's line is `密钥、安全策略与渠道`: an administrator manages the
metadata and never sees plaintext, a developer may *use* an authorized
binding, a viewer gets nothing at all — not even the list. That last one is
stricter than most of this console and is the easy one to get wrong.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ..conftest import PASSWORD


async def _seed_user(engine: AsyncEngine, display_name: str, subject: str) -> None:
    async with engine.begin() as connection:
        user_id = uuid4()
        await connection.execute(
            text(
                "INSERT INTO users (id, status, display_name, is_platform_admin, "
                "created_at) VALUES (:id, 'active', :name, false, now())"
            ),
            {"id": user_id, "name": display_name},
        )
        await connection.execute(
            text(
                "INSERT INTO auth_identities "
                "(id, user_id, provider, subject, password_hash, created_at) "
                "SELECT gen_random_uuid(), :id, 'local', :subject, "
                "  (SELECT password_hash FROM auth_identities LIMIT 1), now()"
            ),
            {"id": user_id, "subject": subject},
        )


def _member(
    client: TestClient, scope: dict[str, str], workspace_id: str, email: str, role: str
) -> dict[str, str]:
    invited = client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        headers=scope,
        json={"email": email, "role": role},
    )
    assert invited.status_code == 201, invited.text
    login = client.post(
        "/api/v1/auth/sessions", json={"subject": email, "password": PASSWORD}
    )
    assert login.status_code == 201, login.text
    return {
        "X-CSRF-Token": login.cookies["tiny_hermes_csrf"],
        "X-Workspace-Id": workspace_id,
    }


@pytest.fixture
def secret_ref(client: TestClient, scope: dict[str, str]) -> str:
    created = client.post(
        "/api/v1/secrets",
        headers=scope,
        json={"name": "feishu-encrypt-key", "scope": "workspace", "plaintext": "s" * 32},
    )
    assert created.status_code == 201, created.text
    # The **id**: that is what `CredentialResolver` resolves, and therefore
    # what a binding must store. The name is only what a person picks by.
    return str(created.json()["id"])


def _create(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    secret_ref: str,
    **overrides: Any,
) -> Any:
    body: dict[str, Any] = {
        "channel": "feishu",
        "agent_id": agent_id,
        "app_id": "cli_a1b2c3",
        "encrypt_key_ref": secret_ref,
        **overrides,
    }
    return client.post("/api/v1/channel-bindings", headers=headers, json=body)


def test_a_binding_carries_the_app_secret_reference_it_replies_with(
    client: TestClient, scope: dict[str, str], published_agent: str, secret_ref: str
) -> None:
    """Replying to a Feishu message needs the app secret (exchanged for a
    tenant token). It is a **reference**, like the encrypt key — the value
    never touches this table — and it is returned so a console can show
    which secret a binding was wired to."""
    created = _create(
        client, scope, published_agent, secret_ref, app_secret_ref=secret_ref
    )

    assert created.status_code == 201, created.text
    assert created.json()["app_secret_ref"] == secret_ref
    assert "s" * 32 not in created.text  # the value, never


def test_a_binding_without_an_app_secret_is_allowed(
    client: TestClient, scope: dict[str, str], published_agent: str, secret_ref: str
) -> None:
    """§929's drill needs a receive-only binding — it counts inbound events
    and never replies. A binding with no app secret is that, not an error."""
    created = _create(client, scope, published_agent, secret_ref)

    assert created.status_code == 201, created.text
    assert created.json()["app_secret_ref"] is None


def test_an_app_secret_naming_no_workspace_secret_is_refused(
    client: TestClient, scope: dict[str, str], published_agent: str, secret_ref: str
) -> None:
    """Same reasoning as the encrypt key: a reference to a secret that does
    not exist fails when it is used — inside an outbound call nobody is
    watching — instead of here, where the person who typed it is."""
    refused = _create(
        client, scope, published_agent, secret_ref, app_secret_ref="no-such-secret"
    )

    assert refused.status_code == 400, refused.text
    assert refused.json()["code"] == "channel_key_unknown"


def test_a_binding_can_be_created_and_then_listed(
    client: TestClient, scope: dict[str, str], published_agent: str, secret_ref: str
) -> None:
    created = _create(client, scope, published_agent, secret_ref)

    assert created.status_code == 201, created.text
    listed = client.get("/api/v1/channel-bindings", headers=scope)
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()] == [created.json()["id"]]


def test_no_response_ever_carries_the_key_itself(
    client: TestClient, scope: dict[str, str], published_agent: str, secret_ref: str
) -> None:
    """§4.6: `管理元数据，不查看明文`.

    Checked against the whole response body rather than field by field: a
    check that named the fields would keep passing on the day somebody adds
    one, which is exactly when it should fail.
    """
    created = _create(client, scope, published_agent, secret_ref)
    listed = client.get("/api/v1/channel-bindings", headers=scope)

    assert "s" * 32 not in created.text
    assert "s" * 32 not in listed.text
    # The reference is fine — that is what it is for.
    assert secret_ref in created.text


def test_a_feishu_binding_without_a_key_reference_is_refused(
    client: TestClient, scope: dict[str, str], published_agent: str
) -> None:
    """Migration 0037's CHECK says a Feishu binding must have one. Refused
    here with something an administrator can act on, rather than reaching
    the database and coming back as an integrity error."""
    refused = _create(client, scope, published_agent, "", encrypt_key_ref=None)

    assert refused.status_code == 400, refused.text
    assert refused.json()["code"] == "channel_key_required"


def test_a_key_reference_that_names_no_secret_is_refused(
    client: TestClient, scope: dict[str, str], published_agent: str
) -> None:
    """A binding pointing at a secret that does not exist accepts deliveries
    it can never decrypt — and fails at the far end, in a webhook, where the
    person who made the mistake is not looking."""
    refused = _create(client, scope, published_agent, "no-such-secret")

    assert refused.status_code == 400, refused.text
    assert refused.json()["code"] == "channel_key_unknown"


def test_the_same_agent_is_not_bound_to_one_channel_twice(
    client: TestClient, scope: dict[str, str], published_agent: str, secret_ref: str
) -> None:
    first = _create(client, scope, published_agent, secret_ref)
    assert first.status_code == 201, first.text

    again = _create(client, scope, published_agent, secret_ref)

    assert again.status_code == 409, again.text


async def test_a_viewer_cannot_even_see_that_a_channel_exists(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    published_agent: str,
    secret_ref: str,
) -> None:
    """§4.6 gives a viewer `否` on channels — not `只读`.

    The row names an Agent and an app id, which together say this workspace
    publishes that Agent into that Feishu tenant. A viewer is not told.
    """
    assert _create(client, scope, published_agent, secret_ref).status_code == 201
    await _seed_user(engine, "Vera", "vera@example.com")
    viewer = _member(client, scope, workspace_id, "vera@example.com", "viewer")

    refused = client.get("/api/v1/channel-bindings", headers=viewer)

    assert refused.status_code == 403, refused.text


async def test_a_developer_may_see_a_binding_but_not_make_one(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    published_agent: str,
    secret_ref: str,
) -> None:
    """§4.6: a developer `使用已授权绑定`. Using it means knowing it is
    there; creating one is publishing an Agent to the outside world, which
    the same line gives to an administrator."""
    assert _create(client, scope, published_agent, secret_ref).status_code == 201
    # Made before the developer logs in: this `TestClient` has one cookie jar,
    # so `scope`'s CSRF stops matching the session the moment somebody else
    # signs in on it.
    second_agent = str(
        client.post(
            "/api/v1/agents", headers=scope, json={"name": "Second", "alias": "second"}
        ).json()["id"]
    )
    await _seed_user(engine, "Dev", "dev@example.com")
    developer = _member(client, scope, workspace_id, "dev@example.com", "developer")

    assert client.get("/api/v1/channel-bindings", headers=developer).status_code == 200

    refused = _create(client, developer, second_agent, secret_ref)

    assert refused.status_code == 403, refused.text


def test_disabling_a_binding_closes_the_door_without_deleting_the_trail(
    client: TestClient, scope: dict[str, str], published_agent: str, secret_ref: str
) -> None:
    """Disabled rather than deleted: `channel_events` references the binding,
    and those rows are the record of what this channel already delivered."""
    binding_id = _create(client, scope, published_agent, secret_ref).json()["id"]

    disabled = client.post(
        f"/api/v1/channel-bindings/{binding_id}/disable", headers=scope
    )

    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["status"] == "disabled"
    # Still listed, so somebody can see that this channel used to be open.
    listed = client.get("/api/v1/channel-bindings", headers=scope).json()
    assert [item["status"] for item in listed] == ["disabled"]


async def test_a_binding_in_another_workspace_is_invisible(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    published_agent: str,
    secret_ref: str,
) -> None:
    """§23 assertion 1, on the route added last.

    Asked as a **workspace** administrator, not the bootstrap user: that one
    is a platform administrator, and §4.6 lets them act across workspaces so
    long as it is audited. Their `200` with an empty list is correct, and a
    test that used them would have been asserting the wrong subject.
    """
    assert _create(client, scope, published_agent, secret_ref).status_code == 201
    await _seed_user(engine, "Ada", "ada@example.com")
    theirs = _member(client, scope, workspace_id, "ada@example.com", "workspace_admin")
    elsewhere = {**theirs, "X-Workspace-Id": str(uuid4())}

    listed = client.get("/api/v1/channel-bindings", headers=elsewhere)

    assert listed.status_code == 403, listed.text


def test_a_key_reference_must_be_one_the_resolver_can_actually_resolve(
    client: TestClient, scope: dict[str, str], published_agent: str, secret_ref: str
) -> None:
    """The reference stored must be the shape `CredentialResolver` reads.

    It reads a Secret **id**, or else an environment-variable name. A
    secret's *name* is neither. `secret_exists` looked the name up and said
    yes, so a binding created cleanly and then failed at the first real
    delivery with `CredentialMissing` and a 500 — validation and use were
    checking different things, which is why the console could report success
    for a binding that could never work.
    """
    created = _create(client, scope, published_agent, secret_ref)
    assert created.status_code == 201, created.text

    listed = client.get("/api/v1/channel-bindings", headers=scope).json()
    stored = listed[0]["encrypt_key_ref"]

    # A uuid, not the name it was chosen by.
    UUID(stored)


def test_a_binding_can_learn_the_app_secret_it_was_created_without(
    client: TestClient, scope: dict[str, str], published_agent: str, secret_ref: str
) -> None:
    """The gap that made the reply path unusable on a real tenant.

    A binding created before outbound existed has no `app_secret_ref`, and
    `uq_channel_bindings_target` allows one binding per (workspace, channel,
    agent) — a constraint that `disable` does **not** release. So there was
    no way to attach the secret and no way to replace the binding either:
    the channel was permanently receive-only, and the dispatcher settled
    every one of its replies `no_credential`.
    """
    created = _create(client, scope, published_agent, secret_ref)
    assert created.status_code == 201, created.text
    binding_id = created.json()["id"]

    updated = client.patch(
        f"/api/v1/channel-bindings/{binding_id}",
        headers=scope,
        json={"app_secret_ref": secret_ref},
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["app_secret_ref"] == secret_ref
    # Untouched, because they were not named. A PATCH that reset every
    # absent field would silently strip the encrypt key and break inbound
    # while fixing outbound.
    assert updated.json()["encrypt_key_ref"] == secret_ref
    assert updated.json()["app_id"] == "cli_a1b2c3"


def test_an_update_naming_a_secret_that_does_not_exist_is_refused(
    client: TestClient, scope: dict[str, str], published_agent: str, secret_ref: str
) -> None:
    created = _create(client, scope, published_agent, secret_ref)
    binding_id = created.json()["id"]

    refused = client.patch(
        f"/api/v1/channel-bindings/{binding_id}",
        headers=scope,
        json={"app_secret_ref": str(uuid4())},
    )

    assert refused.status_code == 400, refused.text
    assert refused.json()["code"] == "channel_key_unknown"


def test_a_feishu_binding_cannot_have_its_encrypt_key_cleared(
    client: TestClient, scope: dict[str, str], published_agent: str, secret_ref: str
) -> None:
    """The CHECK in migration 0037 would refuse this at the database, which
    is a 500. Refused here, because a binding with no key is one that would
    accept forged deliveries — and the caller deserves to be told which
    field they broke."""
    created = _create(client, scope, published_agent, secret_ref)
    binding_id = created.json()["id"]

    refused = client.patch(
        f"/api/v1/channel-bindings/{binding_id}",
        headers=scope,
        json={"encrypt_key_ref": None},
    )

    assert refused.status_code == 400, refused.text
    assert refused.json()["code"] == "channel_key_required"


def test_an_app_secret_can_be_removed_to_make_a_binding_receive_only(
    client: TestClient, scope: dict[str, str], published_agent: str, secret_ref: str
) -> None:
    """The reverse of the first test, and the reason `null` has to mean
    "clear" rather than "unchanged": §929's drill needs a binding that
    replies to nobody, and turning an existing one back into that is how an
    operator runs the drill without deleting the conversation history."""
    created = _create(
        client, scope, published_agent, secret_ref, app_secret_ref=secret_ref
    )
    binding_id = created.json()["id"]

    updated = client.patch(
        f"/api/v1/channel-bindings/{binding_id}",
        headers=scope,
        json={"app_secret_ref": None},
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["app_secret_ref"] is None


async def test_a_developer_may_not_rewire_a_binding(
    client: TestClient,
    engine: AsyncEngine,
    scope: dict[str, str],
    workspace_id: str,
    published_agent: str,
    secret_ref: str,
) -> None:
    """§4.6 again: a developer may *use* an authorized binding. Pointing one
    at a different credential is managing it, which is the administrator's
    line — and the check has to be on the update route too, not only on
    create."""
    created = _create(client, scope, published_agent, secret_ref)
    binding_id = created.json()["id"]
    await _seed_user(engine, "Dev Rewire", "dev-rewire@example.com")
    developer = _member(
        client, scope, workspace_id, "dev-rewire@example.com", "developer"
    )

    refused = client.patch(
        f"/api/v1/channel-bindings/{binding_id}",
        headers=developer,
        json={"app_secret_ref": secret_ref},
    )

    assert refused.status_code == 403, refused.text


def test_an_unknown_binding_cannot_be_updated(
    client: TestClient, scope: dict[str, str]
) -> None:
    missing = client.patch(
        f"/api/v1/channel-bindings/{uuid4()}",
        headers=scope,
        json={"app_secret_ref": None},
    )

    assert missing.status_code == 404, missing.text


def test_a_binding_says_which_transport_it_receives_on(
    client: TestClient, scope: dict[str, str], published_agent: str, secret_ref: str
) -> None:
    """创建响应体和列表都要带 transport：看不见当前值的开关，切换时等于蒙着眼睛点。

    没有 `GET /{binding_id}` 详情路由（这个资源只有 POST、GET 列表、PATCH 和
    POST disable），所以「详情」在这里就是 201 的响应体本身。
    """
    created = _create(client, scope, published_agent, secret_ref)
    assert created.status_code == 201, created.text
    binding_id = created.json()["id"]

    assert created.json()["transport"] == "webhook"
    listed = client.get("/api/v1/channel-bindings", headers=scope).json()
    row = next(item for item in listed if item["id"] == binding_id)
    assert row["transport"] == "webhook"


def test_switching_a_binding_to_the_long_connection_through_the_api(
    client: TestClient, scope: dict[str, str], published_agent: str, secret_ref: str
) -> None:
    """PATCH 一次，再 GET 一次——判据是读回来的值变了，不是 PATCH 返回了 200。

    绑定带着 `app_id` 和 `app_secret_ref` 建出来，因为长连接是**拿这两样去
    握手**的：没有它们，scheduler 会跳过这个绑定（`api/cli.py` 的
    `continue`），切过去只会得到一个看起来正常、实际收不到消息的配置。
    """
    created = _create(
        client, scope, published_agent, secret_ref, app_secret_ref=secret_ref
    )
    assert created.status_code == 201, created.text
    binding_id = created.json()["id"]

    patched = client.patch(
        f"/api/v1/channel-bindings/{binding_id}",
        headers=scope,
        json={"transport": "long_connection"},
    )
    assert patched.status_code == 200, patched.text

    listed = client.get("/api/v1/channel-bindings", headers=scope).json()
    row = next(item for item in listed if item["id"] == binding_id)
    assert row["transport"] == "long_connection"


def test_the_long_connection_is_refused_without_app_credentials(
    client: TestClient, scope: dict[str, str], published_agent: str, secret_ref: str
) -> None:
    """收信专用绑定切长连接必须被拒。

    长连接靠 `app_id` + app secret 换 token 建 WebSocket；没有它们，
    `_long_connections`（`api/cli.py`）只会写一行 warning 然后 `continue`。
    如果这里放行，控制台会显示「长连接」、提示去重启 scheduler、管理员照做，
    然后消息永远不来——一个没有任何界面能解释的死配置。
    """
    created = _create(client, scope, published_agent, secret_ref)
    assert created.status_code == 201, created.text
    assert created.json()["app_secret_ref"] is None
    binding_id = created.json()["id"]

    refused = client.patch(
        f"/api/v1/channel-bindings/{binding_id}",
        headers=scope,
        json={"transport": "long_connection"},
    )

    assert refused.status_code == 400, refused.text
    assert refused.json()["code"] == "channel_transport_unusable"
    listed = client.get("/api/v1/channel-bindings", headers=scope).json()
    row = next(item for item in listed if item["id"] == binding_id)
    assert row["transport"] == "webhook"


def test_a_long_connection_binding_cannot_have_its_app_secret_cleared(
    client: TestClient, scope: dict[str, str], published_agent: str, secret_ref: str
) -> None:
    """另一个方向的同一件事：判据是**改完之后**的状态。

    绑定已经在长连接上跑着，这次 PATCH 只清 `app_secret_ref`——没提
    `transport`，但结果一样是一个 scheduler 会跳过的绑定。
    """
    created = _create(
        client, scope, published_agent, secret_ref, app_secret_ref=secret_ref
    )
    assert created.status_code == 201, created.text
    binding_id = created.json()["id"]
    switched = client.patch(
        f"/api/v1/channel-bindings/{binding_id}",
        headers=scope,
        json={"transport": "long_connection"},
    )
    assert switched.status_code == 200, switched.text

    refused = client.patch(
        f"/api/v1/channel-bindings/{binding_id}",
        headers=scope,
        json={"app_secret_ref": None},
    )

    assert refused.status_code == 400, refused.text
    assert refused.json()["code"] == "channel_transport_unusable"
    listed = client.get("/api/v1/channel-bindings", headers=scope).json()
    row = next(item for item in listed if item["id"] == binding_id)
    assert row["app_secret_ref"] == secret_ref


def test_a_transport_this_platform_has_no_code_for_is_refused_as_bad_input(
    client: TestClient, scope: dict[str, str], published_agent: str, secret_ref: str
) -> None:
    """乱值是 422，不是 500。

    CHECK 约束仍然是合法性的真相所在，但它是**兜底**：让请求一路走到
    `UPDATE` 再炸 `IntegrityError`，而全项目没有任何 `IntegrityError`
    处理器（`api/app.py` 只注册了 `AppError` 和 `AuditedDenial`），调用方
    看到的就是 500。挡在前面的是字段上的 `Literal`——这个字段没有长度限制，
    422 全靠它。
    """
    created = _create(client, scope, published_agent, secret_ref)
    assert created.status_code == 201, created.text
    binding_id = created.json()["id"]

    refused = client.patch(
        f"/api/v1/channel-bindings/{binding_id}",
        headers=scope,
        json={"transport": "carrier_pigeon"},
    )

    assert refused.status_code == 422, refused.text


def test_transport_cannot_be_cleared_because_there_is_no_such_state(
    client: TestClient, scope: dict[str, str], published_agent: str, secret_ref: str
) -> None:
    """`null` 对这个字段没有含义——列是 `NOT NULL`（migration 0052）。

    模型的 docstring 说「a field sent as `null` is cleared」，对凭据字段成立，
    对 `transport` 不成立：清空它没有任何对应的状态，而未捕获的
    `IntegrityError` 会把它变成 500。
    """
    created = _create(client, scope, published_agent, secret_ref)
    assert created.status_code == 201, created.text
    binding_id = created.json()["id"]

    refused = client.patch(
        f"/api/v1/channel-bindings/{binding_id}",
        headers=scope,
        json={"transport": None},
    )

    assert refused.status_code == 422, refused.text


def test_the_long_connection_is_refused_when_the_named_secret_no_longer_resolves(
    client: TestClient, scope: dict[str, str], published_agent: str, secret_ref: str
) -> None:
    """引用非空不等于引用还解析得出来。

    `_long_connections`（`api/cli.py`）有**两个** `continue`：一个是
    `app_id`/`app_secret_ref` 为 null，另一个是 `credentials.resolve()` 抛
    `CredentialMissing`——而 `status is not ACTIVE` 也算
    （`model_catalog/infrastructure/credentials.py`）。只堵住第一个，同一个
    死配置还是造得出来：把 secret disable 掉再切长连接，控制台显示「长连接」、
    挂着重启提示、管理员照做，scheduler 在第二个 `continue` 上写一行
    `logger.exception` 然后跳过，消息永远不来。

    也不能只校验这次 PATCH 里出现的引用：`channel_key_unknown` 那个循环读的是
    `changes`，一个这次没被碰到的失效引用永远不会被重新校验，而它正是这里
    唯一的 app secret。
    """
    created = _create(
        client, scope, published_agent, secret_ref, app_secret_ref=secret_ref
    )
    assert created.status_code == 201, created.text
    binding_id = created.json()["id"]
    disabled = client.post(f"/api/v1/secrets/{secret_ref}/disable", headers=scope)
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["status"] == "disabled"

    refused = client.patch(
        f"/api/v1/channel-bindings/{binding_id}",
        headers=scope,
        json={"transport": "long_connection"},
    )

    assert refused.status_code == 400, refused.text
    assert refused.json()["code"] == "channel_transport_unusable"
    listed = client.get("/api/v1/channel-bindings", headers=scope).json()
    row = next(item for item in listed if item["id"] == binding_id)
    assert row["transport"] == "webhook"


def test_creating_a_binding_refuses_a_field_it_would_otherwise_swallow(
    client: TestClient, scope: dict[str, str], published_agent: str, secret_ref: str
) -> None:
    """请求被接受、字段被丢掉、状态码是成功——三件事不能同时发生。

    `CreateChannelBindingRequest` 没有 `transport`，也没有 `extra="forbid"`
    （这个 repo 别处用了，见 `agents/domain/models.py`），所以带上它会拿到
    201 和一个 `transport: "webhook"` 的响应体。POST 上不接 `transport` 是
    有意的——真要接，after-state 校验也得搬进 `create()`——但「不接」必须说
    出来，而不是默默吃掉。
    """
    created = client.post(
        "/api/v1/channel-bindings",
        headers=scope,
        json={
            "channel": "feishu",
            "agent_id": published_agent,
            "app_id": "cli_a1b2c3",
            "encrypt_key_ref": secret_ref,
            "app_secret_ref": secret_ref,
            "transport": "long_connection",
        },
    )

    assert created.status_code == 422, created.text
    assert client.get("/api/v1/channel-bindings", headers=scope).json() == []


async def test_the_list_says_whether_a_long_connection_is_actually_connected(
    client: TestClient,
    scope: dict[str, str],
    published_agent: str,
    secret_ref: str,
    engine: AsyncEngine,
) -> None:
    """心跳写进了库，还得有人够得着——这一条守的就是那半段。

    `transport` 说的是**配置**：一根死了十小时的长连接照样读回
    `long_connection`。2026-09-03 就是这样，控制台整晚显示正常，而发现它的
    方式是有人发消息发不出去。

    三态一起断言，因为分开哪一个都能被一个错的实现满足：
    - webhook 绑定：`not_applicable`（它没有「连着」这回事）
    - 切成长连接、还没有心跳：`never`（**不是** `not_applicable`——配成长连接
      却从没连上过是个真问题，合并会让它看起来正常）
    - 刚写过心跳：`connected`
    """
    created = _create(
        client, scope, published_agent, secret_ref, app_secret_ref=secret_ref
    )
    assert created.status_code == 201, created.text
    binding_id = created.json()["id"]

    def state_now() -> str:
        listed = client.get("/api/v1/channel-bindings", headers=scope).json()
        return next(item for item in listed if item["id"] == binding_id)[
            "long_connection_state"
        ]

    assert created.json()["long_connection_state"] == "not_applicable"
    assert created.json()["long_connection_seen_at"] is None

    patched = client.patch(
        f"/api/v1/channel-bindings/{binding_id}",
        headers=scope,
        json={"transport": "long_connection"},
    )
    assert patched.status_code == 200, patched.text
    assert state_now() == "never"

    # 直接写那一列，而不是真的跑一根 socket：这条测试问的是「API 交不交得
    # 出来」，socket 那半段由 `test_long_connection_lifecycle.py` 的心跳测试
    # 守着。两条分开，坏的时候才知道坏在哪一半。
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE channel_bindings SET long_connection_seen_at = now()"
                " WHERE id = :b"
            ),
            {"b": UUID(binding_id)},
        )

    assert state_now() == "connected"

"""Resolving an image reference through the binding that received it.

Written after `image_unavailable` on a Run where everything else was right.
The source had been assembled with `CredentialResolver(None, kek)` — a
resolver with no store, which answers `CredentialMissing` for every id it
is ever asked about. Nothing failed at assembly, nothing failed at import,
and the first sign was a red card in a chat window.

So this exercises the lookup against a real database and a real secret:
binding → app secret → the credentials the fetch would use.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.channels.infrastructure.run_images import ChannelImageSource

#: A real 32-byte KEK, because the whole point is exercising the path that
#: unwraps a stored Secret. An environment-variable ref would take the other
#: branch of `CredentialResolver` — the one that needs no store at all — and
#: the first version of this test did exactly that: it passed with the
#: storeless resolver that caused the bug it was written for.
KEK = b"0123456789abcdef0123456789abcdef"


async def _stored_secret(engine: AsyncEngine, workspace_id: str, value: str) -> UUID:
    """One active workspace Secret, sealed the way the service seals them."""
    from tiny_hermes.secrets.domain.envelope import seal

    envelope = seal(value.encode("utf-8"), KEK, "test-key")
    secret_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO secrets (id, name, scope, workspace_id, status, mask,"
                " ciphertext, nonce, wrapped_dek, wrap_nonce, key_id, created_at,"
                " updated_at) VALUES (:i, 'feishu-app-secret', 'workspace', :w,"
                " 'active', '****', :c, :n, :d, :dn, 'test-key', now(), now())"
            ),
            {
                "i": secret_id,
                "w": UUID(workspace_id),
                "c": envelope.ciphertext,
                "n": envelope.nonce,
                "d": envelope.wrapped_dek,
                "dn": envelope.wrap_nonce,
            },
        )
    return secret_id


async def _wire(
    engine: AsyncEngine, workspace_id: str, agent_id: str, *, secret_ref: str | None
) -> UUID:
    """A binding, a conversation and a Session, the way a real delivery leaves them."""
    binding_id, session_id, subject = uuid4(), uuid4(), uuid4()
    async with engine.begin() as connection:
        owner = await connection.execute(text("SELECT id FROM users LIMIT 1"))
        await connection.execute(
            text(
                "INSERT INTO channel_bindings (id, workspace_id, channel, agent_id,"
                " status, created_by, created_at, encrypt_key_ref, app_id,"
                " app_secret_ref) VALUES (:i, :w, 'feishu', :a, 'active', :u, now(),"
                " 'K', 'cli_x', :sec)"
            ),
            {
                "i": binding_id,
                "w": UUID(workspace_id),
                "a": UUID(agent_id),
                "u": owner.scalar_one(),
                "sec": secret_ref,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO end_users (id, workspace_id, created_at)"
                " VALUES (:i, :w, now())"
            ),
            {"i": subject, "w": UUID(workspace_id)},
        )
        await connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, agent_id, session_mode,"
                " caller_type, caller_id, next_run_sequence, next_message_sequence,"
                " created_at) VALUES (:i, :w, :a, 'persistent', 'end_user', :c, 1, 1,"
                " now())"
            ),
            {"i": session_id, "w": UUID(workspace_id), "a": UUID(agent_id), "c": subject},
        )
        await connection.execute(
            text(
                "INSERT INTO channel_conversations (id, channel_binding_id,"
                " external_user_id, session_id, created_at)"
                " VALUES (gen_random_uuid(), :b, 'ou_zhang', :s, now())"
            ),
            {"b": binding_id, "s": session_id},
        )
    return session_id


class _Recorded:
    """Stands in for the network, and records what it was asked to do."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def request(
        self, method: str, url: str, *, json: Any = None, headers: Any = None
    ) -> Any:
        del json
        self.calls.append((method, url, dict(headers or {})))
        import httpx

        if "tenant_access_token" in url:
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "t-abc", "expire": 7200}
            )
        return httpx.Response(
            200, content=b"\x89PNG\r\n", headers={"Content-Type": "image/png"}
        )

    async def post(self, url: str, *, json: Any = None, headers: Any = None) -> Any:
        return await self.request("POST", url, json=json, headers=headers)


async def test_the_source_resolves_a_secret_through_a_real_session(
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: a resolver with no store answers `CredentialMissing`
    for every id, and nothing says so until a person sees a red card."""
    del monkeypatch
    secret_id = await _stored_secret(engine, workspace_id, "s3cret")
    session_id = await _wire(
        engine, workspace_id, published_agent, secret_ref=str(secret_id)
    )
    recorded = _Recorded()

    url = await ChannelImageSource(
        async_sessionmaker(engine, expire_on_commit=False),
        lambda _: recorded,  # pyright: ignore[reportArgumentType]
        KEK,
    ).data_url_for("feishu:om_1:img_k", session_id)

    assert url.startswith("data:image/png;base64,")
    # The token was exchanged for the binding's own secret, and the download
    # addressed the message the reference names.
    assert any("tenant_access_token" in call[1] for call in recorded.calls)
    assert any("/messages/om_1/resources/img_k" in call[1] for call in recorded.calls)


async def test_a_receive_only_binding_cannot_fetch(
    engine: AsyncEngine, workspace_id: str, published_agent: str
) -> None:
    """§929's drill binding has no app secret. Refused with a reason rather
    than sending the model a question with no picture."""
    session_id = await _wire(engine, workspace_id, published_agent, secret_ref=None)

    with pytest.raises(LookupError):
        await ChannelImageSource(
            async_sessionmaker(engine, expire_on_commit=False),
            lambda _: _Recorded(),  # pyright: ignore[reportArgumentType]
            None,
        ).data_url_for("feishu:om_1:img_k", session_id)


async def test_a_session_with_no_channel_cannot_fetch(
    engine: AsyncEngine, workspace_id: str, published_agent: str
) -> None:
    """An ordinary console Session. Nothing about it can name a binding, and
    inventing one would fetch with somebody else's credentials."""
    del workspace_id, published_agent

    with pytest.raises(LookupError):
        await ChannelImageSource(
            async_sessionmaker(engine, expire_on_commit=False),
            lambda _: _Recorded(),  # pyright: ignore[reportArgumentType]
            None,
        ).data_url_for("feishu:om_1:img_k", uuid4())

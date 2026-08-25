"""Talking to Feishu, against a stub that behaves the way Feishu does.

A stub cannot prove the vendor accepts these bytes — only a tenant can, and
the verification record says so. What it can prove is the two things this
platform gets wrong on its own: that a 200 from Feishu is not a success,
and that the app secret is spent once at the token endpoint and never rides
along on a message.
"""

import json
from typing import Any

import httpx
import pytest
from tiny_hermes.channels.infrastructure.feishu_sender import (
    FeishuApiRefused,
    FeishuSender,
)


class _Feishu:
    """Records what was sent and answers with whatever it was handed."""

    def __init__(self, *answers: dict[str, Any]) -> None:
        self._answers = list(answers)
        self.sent: list[tuple[str, Any, dict[str, str]]] = []

    async def post(
        self,
        url: str,
        *,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        self.sent.append((url, json, dict(headers or {})))
        answer = self._answers.pop(0) if self._answers else {"code": 0}
        status = int(answer.pop("_status", 200))
        return httpx.Response(status, json=answer)


def _ok_token(expire: int = 7200) -> dict[str, Any]:
    return {"code": 0, "msg": "ok", "tenant_access_token": "t-abc", "expire": expire}


async def test_a_message_goes_out_under_a_token_exchanged_for_the_secret() -> None:
    feishu = _Feishu(_ok_token(), {"code": 0, "msg": "success"})

    await FeishuSender(feishu).send_text(
        app_id="cli_x", app_secret="s3cret", open_id="ou_zhang", text="上周有 12 单。"  # noqa: S106
    )

    assert len(feishu.sent) == 2
    token_url, token_body, _ = feishu.sent[0]
    message_url, message_body, message_headers = feishu.sent[1]

    assert token_url.endswith("/auth/v3/tenant_access_token/internal")
    assert token_body == {"app_id": "cli_x", "app_secret": "s3cret"}
    assert "/im/v1/messages" in message_url
    assert "receive_id_type=open_id" in message_url
    assert message_headers["Authorization"] == "Bearer t-abc"
    assert message_body["receive_id"] == "ou_zhang"
    assert message_body["msg_type"] == "text"
    # Feishu takes the content as a JSON *string*, not as an object. Getting
    # this wrong is a 200 with a non-zero code, which is exactly the failure
    # the next test exists for.
    assert json.loads(message_body["content"]) == {"text": "上周有 12 单。"}


async def test_the_app_secret_never_rides_along_with_the_message() -> None:
    """The token endpoint is the only place it is allowed to appear."""
    feishu = _Feishu(_ok_token(), {"code": 0})

    await FeishuSender(feishu).send_text(
        app_id="cli_x", app_secret="s3cret", open_id="ou_zhang", text="hi"  # noqa: S106
    )

    _, message_body, message_headers = feishu.sent[1]
    rendered = json.dumps(message_body) + json.dumps(message_headers)
    assert "s3cret" not in rendered


async def test_a_200_carrying_a_non_zero_code_is_a_failure() -> None:
    """Feishu reports its own errors inside a 200 body.

    A client that checked `response.status_code` and stopped would call a
    refused message delivered, stamp the delivery replied, and never retry —
    the message would be lost with every log line saying it went out. That
    is this repository's own signature bug, one layer further out.
    """
    feishu = _Feishu(_ok_token(), {"code": 230001, "msg": "bot is not in the chat"})

    with pytest.raises(FeishuApiRefused) as refused:
        await FeishuSender(feishu).send_text(
            app_id="cli_x", app_secret="s3cret", open_id="ou_zhang", text="hi"  # noqa: S106
        )

    assert refused.value.code == 230001
    assert "bot is not in the chat" in str(refused.value)


async def test_a_token_endpoint_that_refuses_stops_before_sending() -> None:
    feishu = _Feishu({"code": 10003, "msg": "invalid app_secret"})

    with pytest.raises(FeishuApiRefused):
        await FeishuSender(feishu).send_text(
            app_id="cli_x", app_secret="wrong", open_id="ou_zhang", text="hi"  # noqa: S106
        )

    assert len(feishu.sent) == 1


async def test_an_http_error_from_the_edge_is_a_failure_too() -> None:
    """A 5xx never reaches Feishu's own error envelope — there is no `code`
    to read, and treating a missing `code` as zero would read a gateway
    error as a delivered message."""
    feishu = _Feishu({"_status": 502, "error": "bad gateway"})

    with pytest.raises(FeishuApiRefused):
        await FeishuSender(feishu).send_text(
            app_id="cli_x", app_secret="s3cret", open_id="ou_zhang", text="hi"  # noqa: S106
        )


async def test_a_second_message_reuses_the_token_it_already_holds() -> None:
    """Feishu rate-limits the token endpoint, and a reply-per-message
    deployment would hit it. The cache is per app, so two apps do not share
    one token — which would be a cross-tenant leak, not an optimisation."""
    clock = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
    feishu = _Feishu(_ok_token(), {"code": 0}, {"code": 0})
    sender = FeishuSender(feishu, now=lambda: next(clock))

    for _ in range(2):
        await sender.send_text(
            app_id="cli_x", app_secret="s3cret", open_id="ou_zhang", text="hi"  # noqa: S106
        )

    urls = [url for url, _, _ in feishu.sent]
    assert sum("tenant_access_token" in url for url in urls) == 1


async def test_a_token_is_fetched_again_once_it_has_expired() -> None:
    """A clock that is *set* rather than a scripted sequence of ticks.

    The first version scripted the ticks and asserted the wrong thing —
    it counted how many times the sender reads the clock, which is an
    implementation detail, not the behaviour. Moving the clock forward past
    the lifetime says what the test means.
    """
    clock = [0.0]
    feishu = _Feishu(_ok_token(expire=7200), {"code": 0}, _ok_token(), {"code": 0})
    sender = FeishuSender(feishu, now=lambda: clock[0])

    await sender.send_text(
        app_id="cli_x", app_secret="s3cret", open_id="ou_zhang", text="hi"  # noqa: S106
    )
    clock[0] = 10_000.0
    await sender.send_text(
        app_id="cli_x", app_secret="s3cret", open_id="ou_zhang", text="hi"  # noqa: S106
    )

    urls = [url for url, _, _ in feishu.sent]
    assert sum("tenant_access_token" in url for url in urls) == 2


async def test_two_apps_do_not_share_one_token() -> None:
    feishu = _Feishu(_ok_token(), {"code": 0}, _ok_token(), {"code": 0})
    sender = FeishuSender(feishu)

    await sender.send_text(
        app_id="cli_x", app_secret="s1", open_id="ou_a", text="hi"  # noqa: S106
    )
    await sender.send_text(
        app_id="cli_y", app_secret="s2", open_id="ou_b", text="hi"  # noqa: S106
    )

    urls = [url for url, _, _ in feishu.sent]
    assert sum("tenant_access_token" in url for url in urls) == 2


async def test_a_message_carries_a_deduplication_key() -> None:
    """Feishu's own `uuid`, so a retry cannot deliver twice.

    Written after a live tenant received five copies of one reply. The send
    had succeeded every time; only reading the response failed, and the
    dispatcher read that as "it did not happen". The response bug is fixed
    in `outbound/client.py`, but the shape stays: anything that fails after
    the request left this process cannot be distinguished from something
    that failed before, and only the vendor can settle it.
    """
    feishu = _Feishu(_ok_token(), {"code": 0})

    await FeishuSender(feishu).send_text(
        app_id="cli_x",
        app_secret="s3cret",  # noqa: S106
        open_id="ou_zhang",
        text="hi",
        delivery_key="7f1c3a2e-0000-4000-8000-000000000001",
    )

    _, message_body, _ = feishu.sent[1]
    assert message_body["uuid"] == "7f1c3a2e-0000-4000-8000-000000000001"


async def test_a_send_without_a_deduplication_key_still_works() -> None:
    """Not every caller has one to give. Absent means absent — a generated
    key would be a different value on every retry, which is worse than none:
    it would look like deduplication while never deduplicating."""
    feishu = _Feishu(_ok_token(), {"code": 0})

    await FeishuSender(feishu).send_text(
        app_id="cli_x", app_secret="s3cret", open_id="ou_zhang", text="hi"  # noqa: S106
    )

    _, message_body, _ = feishu.sent[1]
    assert "uuid" not in message_body

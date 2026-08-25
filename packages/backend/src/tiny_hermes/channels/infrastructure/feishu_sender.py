"""The one place this platform speaks to Feishu's Open API.

Everything here goes out through `SafeOutboundClient`, which means through
the egress proxy — §16.5 has no exemption for a vendor this platform
happens to trust, and `open.feishu.cn` is approved at the platform and
workspace layers like any other target.

The trap this module exists to contain: **Feishu reports its own errors
inside a 200**. `{"code": 230001, "msg": "bot is not in the chat"}` arrives
with a perfectly ordinary status line. A client that checked
`response.status_code` would call a refused message delivered, the
dispatcher would stamp the delivery replied, and the message would be lost
with every log line saying it went out — this repository's signature bug,
one layer further out than usual.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any, Protocol, cast

import httpx

logger = logging.getLogger(__name__)

FEISHU_BASE_URL = "https://open.feishu.cn/open-apis"

#: Feishu's documented ceiling on the `uuid` field. A key longer than this
#: is truncated rather than refused: it stays stable across the retries of one
#: delivery, which is the only property deduplication needs.
MAX_DELIVERY_KEY_CHARS = 50

#: Taken off a token's own `expire`, so a token is refreshed before Feishu
#: stops honouring it. A request that raced the expiry would fail for a
#: reason that looks like a bad secret.
_EXPIRY_MARGIN_SECONDS = 300


class FeishuApiRefused(Exception):
    """Feishu did not accept this. `code` is theirs; `0` means the refusal
    never carried one — a gateway error, or a body that was not their
    envelope at all."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"feishu refused with code {code}: {message}")
        self.code = code
        self.message = message


class OutboundPost(Protocol):
    """`request` as well as `post`, because updating a card is a PATCH.

    Feishu routes the update by method on the same path as a read, so the
    method is not incidental — a POST there means something else.
    """

    async def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response: ...

    """Only the verb this module uses. Narrow so a sender cannot reach past
    it into the rest of `SafeOutboundClient`."""

    async def post(
        self,
        url: str,
        *,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response: ...


@dataclass(frozen=True)
class _Token:
    value: str
    expires_at: float


class FeishuSender:
    """Exchanges an app secret for a token, then sends text as the bot.

    The token cache is keyed by `app_id` and lives for the process. Two apps
    never share an entry: a shared token would be one tenant's bot speaking
    with another tenant's authority, which is a cross-tenant leak rather
    than a cache miss.
    """

    def __init__(
        self,
        client: OutboundPost,
        *,
        base_url: str = FEISHU_BASE_URL,
        now: Callable[[], float] = monotonic,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._now = now
        self._tokens: dict[str, _Token] = {}

    async def send_text(
        self,
        *,
        app_id: str,
        app_secret: str,
        open_id: str,
        text: str,
        delivery_key: str | None = None,
    ) -> None:
        """Send one text message as the bot.

        `delivery_key` becomes Feishu's `uuid`, which is their own
        deduplication: the same key sent twice delivers once. It matters
        because a failure *after* the request left this process is
        indistinguishable from one before it, so a retry can always be a
        second delivery. A live tenant received five copies of one reply
        that way. Absent when the caller has no stable key — a generated one
        would differ per attempt and deduplicate nothing while looking like
        it did.
        """
        await self._send(
            app_id=app_id,
            app_secret=app_secret,
            open_id=open_id,
            msg_type="text",
            # A JSON *string*, not an object. Feishu's own encoding, and
            # getting it wrong is a 200 with a non-zero code.
            content=json.dumps({"text": text}, ensure_ascii=False),
            delivery_key=delivery_key,
        )

    async def send_card(
        self,
        *,
        app_id: str,
        app_secret: str,
        open_id: str,
        card: dict[str, Any],
        delivery_key: str | None = None,
    ) -> str | None:
        """An `interactive` message — §19.2's status card.

        Returns Feishu's `message_id`, which is the only handle `update_card`
        accepts. `None` when the answer carried none: the caller then knows
        this card can never be updated and has to send a new message instead,
        which is worse than updating and much better than silence.

        Same envelope as a text message with a different `msg_type`, which
        is why both go through `_send`: the token exchange, the `uuid`
        deduplication and the "a 200 is not a success" check are not
        specific to what is being sent, and a second copy of them would be a
        second place to get that wrong.
        """
        envelope = await self._send(
            app_id=app_id,
            app_secret=app_secret,
            open_id=open_id,
            msg_type="interactive",
            content=json.dumps(card, ensure_ascii=False),
            delivery_key=delivery_key,
        )
        data: Any = envelope.get("data")
        if not isinstance(data, dict):
            return None
        found: Any = cast(dict[str, Any], data).get("message_id")
        return found if isinstance(found, str) and found else None

    async def update_card(
        self,
        *,
        app_id: str,
        app_secret: str,
        message_id: str,
        card: dict[str, Any],
    ) -> None:
        """Rewrite a card that is already in the conversation.

        `PATCH /im/v1/messages/{message_id}`. Feishu's own constraints, none
        of which this platform gets to relax: the card must carry
        `config.update_multi: true` **both when sent and when updated**, only
        interactive cards can be updated, the window is 14 days, the result
        must stay under 30 KB, and the token has to be the one that sent it.

        No `uuid` here. Deduplication is for creating a message twice; an
        update is idempotent by construction — the same card patched twice
        leaves the same card.
        """
        token = await self._token(app_id, app_secret)
        answer = await self._client.request(
            "PATCH",
            f"{self._base_url}/im/v1/messages/{message_id}",
            json={"content": json.dumps(card, ensure_ascii=False)},
            headers={"Authorization": f"Bearer {token}"},
        )
        self._envelope(answer)

    async def _send(
        self,
        *,
        app_id: str,
        app_secret: str,
        open_id: str,
        msg_type: str,
        content: str,
        delivery_key: str | None,
    ) -> dict[str, Any]:
        token = await self._token(app_id, app_secret)
        message: dict[str, Any] = {
            "receive_id": open_id,
            "msg_type": msg_type,
            "content": content,
        }
        if delivery_key is not None:
            message["uuid"] = delivery_key[:MAX_DELIVERY_KEY_CHARS]
        answer = await self._client.post(
            f"{self._base_url}/im/v1/messages?receive_id_type=open_id",
            json=message,
            headers={"Authorization": f"Bearer {token}"},
        )
        return self._envelope(answer)

    async def token_for(self, app_id: str, app_secret: str) -> str:
        """The cached tenant token, for another caller in this package.

        `FeishuImageFetcher` needs one for the same app. Sharing this cache
        rather than building a second is not an optimisation: Feishu
        rate-limits the token endpoint, and two caches for one app would
        double the calls to it.
        """
        return await self._token(app_id, app_secret)

    async def _token(self, app_id: str, app_secret: str) -> str:
        held = self._tokens.get(app_id)
        if held is not None and held.expires_at > self._now():
            return held.value

        answer = await self._client.post(
            f"{self._base_url}/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
        )
        envelope = self._envelope(answer)
        value = envelope.get("tenant_access_token")
        if not isinstance(value, str) or not value:
            raise FeishuApiRefused(0, "token response carried no token")
        expire: Any = envelope.get("expire")
        lifetime = expire if isinstance(expire, int) and expire > 0 else 7200
        self._tokens[app_id] = _Token(
            value=value,
            expires_at=self._now() + max(lifetime - _EXPIRY_MARGIN_SECONDS, 60),
        )
        return value

    def _envelope(self, answer: httpx.Response) -> dict[str, Any]:
        """Feishu's own envelope, or a refusal.

        A 5xx from an edge never reaches the envelope, so there is no `code`
        to read — and defaulting a missing `code` to zero would read a
        gateway error as a delivered message. Absent means refused.
        """
        try:
            parsed: Any = answer.json()
        except ValueError as error:
            raise FeishuApiRefused(
                0, f"status {answer.status_code}, body is not JSON"
            ) from error
        if not isinstance(parsed, dict):
            raise FeishuApiRefused(
                0, f"status {answer.status_code}, body is not an object"
            )
        envelope = cast(dict[str, Any], parsed)
        code: Any = envelope.get("code")
        if code != 0:
            raise FeishuApiRefused(
                code if isinstance(code, int) else 0,
                str(envelope.get("msg") or f"status {answer.status_code}"),
            )
        return envelope


__all__ = ["FEISHU_BASE_URL", "FeishuApiRefused", "FeishuSender", "OutboundPost"]

"""Fetching an image out of Feishu, as bytes a model request can carry.

Here rather than beside the model adapter, because the download is
authenticated with the binding's own `tenant_access_token`. An adapter
holding a channel's app secret would be a credential in the wrong module —
the same separation that keeps `feishu_sender` out of `runs`.
"""

import base64

from tiny_hermes.channels.infrastructure.feishu_sender import (
    FeishuApiRefused,
    FeishuSender,
    OutboundPost,
)

#: What one image may weigh. Feishu permits 100 MB; a model request carrying
#: that is not a request, it is an outage — and providers charge for what
#: they are sent. 8 MB is generous for a photograph and small enough that a
#: malicious or careless upload cannot take a Run down with it.
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class ImageTooLarge(Exception):
    """Refused rather than truncated.

    Half a JPEG, base64'd into a prompt, is described confidently by the
    model with nothing to tell the reader it was cut. An honest failure is
    the smaller harm.
    """


class FeishuImageFetcher:
    """Downloads one image and returns it as a `data:` URL.

    A data URL rather than a stored object: the bytes exist only for the
    duration of one model request. Storing them would need a home, a
    lifecycle and an owner, and `artifacts` is none of those for an input —
    its `run_id` is required and an image arrives before the Run does.
    """

    def __init__(
        self, client: OutboundPost, *, base_url: str = "https://open.feishu.cn/open-apis"
    ) -> None:
        self._client = client
        # Reuses the sender's token cache and its "a 200 is not a success"
        # envelope check. Two token caches for one app would double the calls
        # to an endpoint Feishu rate-limits.
        self._tokens = FeishuSender(client, base_url=base_url)
        self._base_url = base_url

    async def data_url(
        self, *, app_id: str, app_secret: str, message_id: str, file_key: str
    ) -> str:
        token = await self._tokens.token_for(app_id, app_secret)
        answer = await self._client.request(
            "GET",
            f"{self._base_url}/im/v1/messages/{message_id}"
            f"/resources/{file_key}?type=image",
            headers={"Authorization": f"Bearer {token}"},
        )
        body = answer.content
        media_type = answer.headers.get("Content-Type", "")
        if answer.status_code != 200 or media_type.startswith("application/json"):
            # Feishu reports its own failures inside a JSON body, sometimes
            # with a 200. A picture is never JSON, so the content type is the
            # honest discriminator here — reading `code` would work too, but
            # only for the responses that have one.
            raise FeishuApiRefused(
                answer.status_code, f"image download refused: {body[:200]!r}"
            )
        if len(body) > MAX_IMAGE_BYTES:
            raise ImageTooLarge(
                f"image is {len(body)} bytes, over the {MAX_IMAGE_BYTES} ceiling"
            )
        encoded = base64.b64encode(body).decode("ascii")
        return f"data:{media_type};base64,{encoded}"


__all__: list[str] = ["MAX_IMAGE_BYTES", "FeishuImageFetcher", "ImageTooLarge"]

"""Fetching an image out of Feishu, as bytes a model request can carry.

Everything here is about the two ways this goes wrong at scale rather than
the happy path, because the happy path is one GET.

Feishu allows a resource up to **100 MB**. A model request carrying that is
not a request; it is an outage. So this caps well below the vendor's
ceiling, and the cap is a refusal rather than a truncation — half a JPEG
base64'd into a prompt is worse than an honest failure.

And the download uses the binding's own `tenant_access_token`, which is why
this lives in `channels` and not beside the model adapter: an adapter that
held a channel's app secret would be a credential in the wrong module.
"""

from typing import Any

import httpx
import pytest
from tiny_hermes.channels.infrastructure.feishu_images import (
    MAX_IMAGE_BYTES,
    FeishuImageFetcher,
    ImageTooLarge,
)


class _Feishu:
    def __init__(self, *answers: httpx.Response) -> None:
        self._answers = list(answers)
        self.sent: list[tuple[str, str]] = []

    async def request(
        self, method: str, url: str, *, json: Any = None, headers: Any = None
    ) -> httpx.Response:
        del json, headers
        self.sent.append((method, url))
        return self._answers.pop(0)

    async def post(self, url: str, *, json: Any = None, headers: Any = None) -> httpx.Response:
        del json, headers
        self.sent.append(("POST", url))
        return self._answers.pop(0)


def _token() -> httpx.Response:
    return httpx.Response(
        200, json={"code": 0, "tenant_access_token": "t-abc", "expire": 7200}
    )


def _image(body: bytes = b"\x89PNG\r\n", media_type: str = "image/png") -> httpx.Response:
    return httpx.Response(200, content=body, headers={"Content-Type": media_type})


async def _fetch(*answers: httpx.Response, **kwargs: Any) -> str:
    feishu = _Feishu(*answers)
    fetcher = FeishuImageFetcher(feishu, **kwargs)
    return await fetcher.data_url(
        app_id="cli_x",
        app_secret="s3cret",  # noqa: S106
        message_id="om_1",
        file_key="img_v2_abc",
    )


async def test_an_image_comes_back_as_a_data_url() -> None:
    url = await _fetch(_token(), _image())

    assert url.startswith("data:image/png;base64,")


async def test_the_download_addresses_the_message_and_the_key() -> None:
    feishu = _Feishu(_token(), _image())

    await FeishuImageFetcher(feishu).data_url(
        app_id="cli_x",
        app_secret="s3cret",  # noqa: S106
        message_id="om_1",
        file_key="img_v2_abc",
    )

    method, url = feishu.sent[1]
    assert method == "GET"
    assert "/im/v1/messages/om_1/resources/img_v2_abc" in url
    assert "type=image" in url


async def test_the_media_type_comes_from_the_response() -> None:
    """Read, never guessed. The sender's platform already decided what this
    file is, and sniffing bytes here would put a second answer beside it."""
    url = await _fetch(_token(), _image(media_type="image/jpeg"))

    assert url.startswith("data:image/jpeg;base64,")


async def test_an_image_past_the_cap_is_refused_not_truncated() -> None:
    """Feishu permits 100 MB. Half a JPEG base64'd into a prompt is worse
    than an honest failure: the model would describe a corrupt image with
    no sign to anyone that it had been cut."""
    with pytest.raises(ImageTooLarge):
        await _fetch(_token(), _image(body=b"x" * (MAX_IMAGE_BYTES + 1)))


async def test_the_cap_is_far_below_what_the_vendor_allows() -> None:
    """Stated as an assertion so raising it is a deliberate act. A request
    carrying a 100 MB image is not a request, it is an outage."""
    assert MAX_IMAGE_BYTES <= 8 * 1024 * 1024


async def test_a_refused_download_is_reported_rather_than_returning_nothing() -> None:
    from tiny_hermes.channels.infrastructure.feishu_sender import FeishuApiRefused

    with pytest.raises(FeishuApiRefused):
        await _fetch(_token(), httpx.Response(404, json={"code": 234001, "msg": "not found"}))


async def test_a_non_image_response_is_refused() -> None:
    """A JSON error body served with 200 is Feishu's own failure shape, and
    it is not a picture however hard it is squinted at."""
    from tiny_hermes.channels.infrastructure.feishu_sender import FeishuApiRefused

    with pytest.raises(FeishuApiRefused):
        await _fetch(
            _token(),
            httpx.Response(200, json={"code": 99991663, "msg": "permission denied"}),
        )

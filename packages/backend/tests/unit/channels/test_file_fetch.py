"""Fetching a file out of Feishu for the sandbox's inbox (§1.27, v2.27)."""

from typing import Any

import httpx
import pytest
from tiny_hermes.channels.infrastructure.feishu_images import (
    MAX_FILE_BYTES,
    FeishuFileFetcher,
    FileTooLarge,
)
from tiny_hermes.channels.infrastructure.feishu_sender import FeishuApiRefused


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
    return httpx.Response(200, json={"code": 0, "tenant_access_token": "t-abc", "expire": 7200})


async def _fetch(feishu: _Feishu) -> bytes:
    return await FeishuFileFetcher(feishu).file_bytes(
        app_id="cli_x",
        app_secret="s3cret",  # noqa: S106
        message_id="om_1",
        file_key="file_k",
    )


async def test_a_file_is_fetched_as_a_file_resource() -> None:
    feishu = _Feishu(_token(), httpx.Response(200, content=b"PK\\x03\\x04 xlsx bytes"))

    body = await _fetch(feishu)

    assert body == b"PK\\x03\\x04 xlsx bytes"
    assert feishu.sent[-1] == (
        "GET",
        "https://open.feishu.cn/open-apis/im/v1/messages/om_1/resources/file_k?type=file",
    )


async def test_a_file_over_the_ceiling_is_refused_not_cut() -> None:
    """Half a spreadsheet opens as a broken one, with nothing saying why."""
    feishu = _Feishu(_token(), httpx.Response(200, content=b"x" * (MAX_FILE_BYTES + 1)))

    with pytest.raises(FileTooLarge):
        await _fetch(feishu)


async def test_a_refusal_from_feishu_is_not_taken_for_the_file() -> None:
    feishu = _Feishu(
        _token(),
        httpx.Response(
            400,
            json={"code": 234003, "msg": "File not in msg"},
            headers={"Content-Type": "application/json"},
        ),
    )

    with pytest.raises(FeishuApiRefused):
        await _fetch(feishu)

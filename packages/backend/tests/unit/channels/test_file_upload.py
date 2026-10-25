"""Sending a workspace file into a Feishu conversation (§1.27, v2.27):
upload, then a file message that names what was uploaded."""

import json
from typing import Any

import httpx
import pytest
from tiny_hermes.channels.infrastructure.feishu_files import FeishuFileUploader, feishu_file_type
from tiny_hermes.channels.infrastructure.feishu_sender import FeishuApiRefused, FeishuSender


class _Feishu:
    def __init__(self, *answers: httpx.Response) -> None:
        self._answers = list(answers)
        self.sent: list[dict[str, Any]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        content: bytes | None = None,
        headers: Any = None,
    ) -> httpx.Response:
        self.sent.append(
            {"method": method, "url": url, "json": json, "content": content, "headers": headers}
        )
        return self._answers.pop(0)

    async def post(self, url: str, *, json: Any = None, headers: Any = None) -> httpx.Response:
        return await self.request("POST", url, json=json, headers=headers)


def _token() -> httpx.Response:
    return httpx.Response(200, json={"code": 0, "tenant_access_token": "t-abc", "expire": 7200})


async def test_an_upload_is_a_multipart_form_feishu_answers_with_a_key() -> None:
    feishu = _Feishu(_token(), httpx.Response(200, json={"code": 0, "data": {"file_key": "fk_1"}}))

    key = await FeishuFileUploader(feishu).upload(
        app_id="cli_x",
        app_secret="s3cret",  # noqa: S106
        name="周报.xlsx",
        body=b"PK\x03\x04 bytes",
    )

    assert key == "fk_1"
    upload = feishu.sent[-1]
    assert upload["url"] == "https://open.feishu.cn/open-apis/im/v1/files"
    assert upload["headers"]["Authorization"] == "Bearer t-abc"
    content_type = upload["headers"]["Content-Type"]
    assert content_type.startswith("multipart/form-data; boundary=")
    parsed = httpx.Request(
        "POST", "http://x", content=upload["content"], headers={"Content-Type": content_type}
    )
    form = parsed.read()
    assert b'name="file_type"\r\n\r\nxls\r\n' in form
    assert 'name="file_name"\r\n\r\n周报.xlsx\r\n'.encode() in form
    assert b"PK\x03\x04 bytes" in form


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("a.PDF", "pdf"),
        ("a.docx", "doc"),
        ("a.xlsx", "xls"),
        ("a.pptx", "ppt"),
        ("a.csv", "stream"),
        ("noext", "stream"),
    ],
)
def test_feishu_s_own_kinds_are_used_where_it_has_one(name: str, kind: str) -> None:
    assert feishu_file_type(name) == kind


async def test_a_refused_upload_is_an_error_not_a_key() -> None:
    feishu = _Feishu(_token(), httpx.Response(200, json={"code": 234001, "msg": "bad file"}))

    with pytest.raises(FeishuApiRefused):
        await FeishuFileUploader(feishu).upload(
            app_id="cli_x",
            app_secret="s3cret",  # noqa: S106
            name="a.pdf",
            body=b"%PDF",
        )


async def test_the_file_message_names_the_upload_and_carries_a_delivery_key() -> None:
    feishu = _Feishu(_token(), httpx.Response(200, json={"code": 0, "data": {}}))

    await FeishuSender(feishu).send_file(
        app_id="cli_x",
        app_secret="s3cret",  # noqa: S106
        open_id="ou_zhang",
        file_key="fk_1",
        delivery_key="file-abc",
    )

    message = feishu.sent[-1]
    assert message["url"].endswith("/im/v1/messages?receive_id_type=open_id")
    assert message["json"]["msg_type"] == "file"
    assert json.loads(message["json"]["content"]) == {"file_key": "fk_1"}
    assert message["json"]["uuid"] == "file-abc"

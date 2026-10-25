"""Uploading a file to Feishu, so a message can carry it (§1.27, v2.27)."""

from typing import Any, Protocol, cast
from uuid import uuid4

import httpx

from tiny_hermes.channels.infrastructure.feishu_sender import (
    FEISHU_BASE_URL,
    FeishuApiRefused,
    FeishuSender,
)

#: Feishu's own kinds; anything else is uploaded as `stream`, which every
#: client still opens by its name.
_KINDS = {
    "pdf": "pdf",
    "doc": "doc",
    "docx": "doc",
    "xls": "xls",
    "xlsx": "xls",
    "ppt": "ppt",
    "pptx": "ppt",
    "mp4": "mp4",
    "opus": "opus",
}


def feishu_file_type(name: str) -> str:
    _, dot, extension = name.rpartition(".")
    return _KINDS.get(extension.lower(), "stream") if dot else "stream"


class OutboundUpload(Protocol):
    """`request` with a raw body, which a multipart upload needs and a JSON
    message does not — narrower senders keep their narrower protocol."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response: ...

    async def post(
        self, url: str, *, json: Any = None, headers: dict[str, str] | None = None
    ) -> httpx.Response: ...


class FeishuFileUploader:
    def __init__(self, client: OutboundUpload, *, base_url: str = FEISHU_BASE_URL) -> None:
        self._client = client
        self._tokens = FeishuSender(client, base_url=base_url)
        self._base_url = base_url.rstrip("/")

    async def upload(self, *, app_id: str, app_secret: str, name: str, body: bytes) -> str:
        """`POST /im/v1/files`; the `file_key` a file message names."""
        token = await self._tokens.token_for(app_id, app_secret)
        boundary = uuid4().hex
        answer = await self._client.request(
            "POST",
            f"{self._base_url}/im/v1/files",
            content=_multipart(
                boundary,
                {"file_type": feishu_file_type(name), "file_name": name},
                body,
            ),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        envelope = self._tokens.envelope_of(answer)
        data: Any = envelope.get("data")
        key: Any = cast(dict[str, Any], data).get("file_key") if isinstance(data, dict) else None
        if not isinstance(key, str) or not key:
            raise FeishuApiRefused(0, "upload response carried no file key")
        return key


def _multipart(boundary: str, fields: dict[str, str], body: bytes) -> bytes:
    """The form by hand: the outbound client sends bytes, not form objects.

    The file part's own filename is a placeholder; Feishu shows `file_name`,
    and a name with non-ASCII characters has no single portable header form.
    """
    parts: list[bytes] = []
    for field, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="upload"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n".encode()
        + body
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts)


__all__ = ["FeishuFileUploader", "OutboundUpload", "feishu_file_type"]

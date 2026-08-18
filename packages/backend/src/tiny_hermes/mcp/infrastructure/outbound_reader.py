"""Asking a server what it can do, across the egress boundary.

`tools/list` over JSON-RPC, sent through `SafeOutboundClient` — so it crosses
the proxy and is measured against the outbound scope exactly like every other
request this process makes. There is no separate MCP transport here and no
socket of its own: a second way out would be a second thing to remember when
the boundary changes.

The credential is resolved at the moment of the call and put in a header. It is
never returned and never lands in a capability record.

**What comes back is data.** A tool description is text somebody else wrote,
and it goes into a model's context. Nothing here follows it.
"""

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiny_hermes.mcp.application.service import McpUnreachable
from tiny_hermes.model_catalog.infrastructure.credentials import (
    CredentialMissing,
    CredentialResolver,
)
from tiny_hermes.outbound.client import SafeOutboundClient
from tiny_hermes.outbound.errors import OutboundError
from tiny_hermes.secrets.infrastructure.sql_store import SqlSecretStore
from tiny_hermes.tools.domain.mcp import (
    MAX_CAPABILITY_BYTES,
    McpCapabilities,
    parse_capabilities,
)

#: The JSON-RPC method every MCP server answers with its tool list.
TOOLS_LIST = "tools/list"


class OutboundCapabilityReader:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client_factory: Callable[[], AbstractAsyncContextManager[SafeOutboundClient]],
        kek: bytes | None = None,
    ) -> None:
        self._sessions = session_factory
        self._client_factory = client_factory
        self._kek = kek

    async def read(self, url: str, credential_ref: str | None) -> McpCapabilities:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if credential_ref is not None:
            async with self._sessions() as session:
                resolver = CredentialResolver(SqlSecretStore(session), self._kek)
                try:
                    token = await resolver.resolve(credential_ref)
                except CredentialMissing as missing:
                    # Named without the ref's value: an operator reading this
                    # learns that a credential is missing, not what it is.
                    raise McpUnreachable(
                        "the credential this server needs is not available"
                    ) from missing
            headers["Authorization"] = f"Bearer {token}"
        async with self._client_factory() as client:
            try:
                response = await client.request(
                    "POST",
                    url,
                    json={"jsonrpc": "2.0", "id": 1, "method": TOOLS_LIST},
                    headers=headers,
                )
            except OutboundError as failed:
                # Every way out fails the same way from here: the boundary said
                # no, the target did not answer, the answer was too big. All of
                # them mean "no capabilities", and the reason travels for an
                # operator to read.
                raise McpUnreachable(str(failed)) from failed
        if response.status_code >= 400:
            raise McpUnreachable(f"the server answered {response.status_code}")
        body = response.content[: MAX_CAPABILITY_BYTES + 1]
        error = _rpc_error(body)
        if error is not None:
            raise McpUnreachable(f"the server refused tools/list: {error}")
        return parse_capabilities(body)


def _rpc_error(body: bytes) -> str | None:
    """A JSON-RPC error, if the answer is one.

    Read before parsing capabilities so a well-formed refusal is reported as a
    refusal rather than as "this answer has no tools array" — the second is
    true and useless.
    """
    try:
        document = json.loads(body)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    envelope = cast(dict[str, Any], document)
    failure: Any = envelope.get("error")
    if failure is None:
        return None
    if isinstance(failure, dict):
        return str(cast(dict[str, Any], failure).get("message", failure))
    return str(failure)

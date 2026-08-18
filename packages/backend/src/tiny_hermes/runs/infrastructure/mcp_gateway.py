"""Revalidating and calling MCP servers, on the platform's side of the boundary.

The same three decisions as `http_tool_sender`, for the same reasons: the
platform sends it rather than the sandbox, the credential is resolved here and
returned nowhere, and every request goes through `SafeOutboundClient` and so
through the egress proxy with the Run's layers named.

One decision is this module's own. A server that does not answer during
revalidation costs its tools and nothing else: the Run goes on without them and
records that it happened. Failing the whole Run because one of three servers is
down would make an Agent's reliability the product of everybody else's uptime,
and a Run that silently behaved as though the tool had never been bound would
be worse — hence the record.
"""

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiny_hermes.agents.domain.models import McpToolBinding
from tiny_hermes.mcp.infrastructure.tables import McpServerRow, McpServerVersionRow
from tiny_hermes.model_catalog.infrastructure.credentials import (
    CredentialMissing,
    CredentialResolver,
)
from tiny_hermes.outbound.client import SafeOutboundClient
from tiny_hermes.outbound.errors import OutboundError, OutboundRefused
from tiny_hermes.runs.ports.http_calls import EgressClaim
from tiny_hermes.runs.ports.mcp import BoundMcpTool, McpAnswer, McpRevalidation
from tiny_hermes.secrets.infrastructure.sql_store import SqlSecretStore
from tiny_hermes.tools.domain.mcp import (
    MAX_RESULT_BYTES,
    McpCapabilities,
    McpRefused,
    bound_tools,
    parse_capabilities,
)

TOOLS_LIST = "tools/list"
TOOLS_CALL = "tools/call"


class OutboundMcpGateway:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client_factory: Callable[
            [EgressClaim], AbstractAsyncContextManager[SafeOutboundClient]
        ],
        kek: bytes | None = None,
    ) -> None:
        self._sessions = session_factory
        self._client_factory = client_factory
        self._kek = kek

    async def revalidate(
        self, bindings: tuple[McpToolBinding, ...], claim: EgressClaim
    ) -> McpRevalidation:
        found: list[BoundMcpTool] = []
        unreachable: list[str] = []
        missing: list[str] = []
        for binding in bindings:
            record = await self._server_of(binding.mcp_server_version_id)
            if record is None:
                # The rows are gone. Publishing checked them, so this can only
                # follow a deletion; skipped rather than failing the Run, the
                # same as a deleted skill.
                continue
            name, url, credential_ref = record
            try:
                capabilities = await self._list(url, credential_ref, claim)
            except (OutboundError, McpRefused, CredentialMissing):
                unreachable.append(name)
                continue
            offered = bound_tools(capabilities, list(binding.tools))
            offered_names = {tool.name for tool in offered}
            missing.extend(
                f"{name}.{wanted}"
                for wanted in binding.tools
                if wanted not in offered_names
            )
            found.extend(
                BoundMcpTool(
                    server_name=name,
                    version_id=binding.mcp_server_version_id,
                    tool=tool,
                )
                for tool in offered
            )
        return McpRevalidation(
            tools=tuple(found),
            unreachable=tuple(unreachable),
            missing=tuple(missing),
        )

    async def call(
        self,
        bound: BoundMcpTool,
        arguments: dict[str, object],
        claim: EgressClaim,
    ) -> McpAnswer:
        record = await self._server_of(bound.version_id)
        if record is None:  # pragma: no cover - revalidation found it
            return McpAnswer(content="", refusal="server_unavailable")
        _, url, credential_ref = record
        try:
            body = await self._rpc(
                url,
                credential_ref,
                claim,
                method=TOOLS_CALL,
                params={"name": bound.tool.name, "arguments": arguments},
            )
        except CredentialMissing:
            return McpAnswer(content="", refusal="credential_unavailable")
        except OutboundRefused as refused:
            # The boundary said no, not the server. Named with the scope's own
            # reason so a person can tell "never approved" from "was down".
            return McpAnswer(content="", refusal=refused.reason.value)
        except OutboundError:
            return McpAnswer(content="", refusal="server_unreachable")
        if len(body) > MAX_RESULT_BYTES:
            # Refused rather than truncated, the rule `skill.load` set.
            return McpAnswer(content="", refusal="result_too_large")
        return _answer(body)

    async def _list(
        self, url: str, credential_ref: str | None, claim: EgressClaim
    ) -> McpCapabilities:
        body = await self._rpc(url, credential_ref, claim, method=TOOLS_LIST, params={})
        return parse_capabilities(body)

    async def _rpc(
        self,
        url: str,
        credential_ref: str | None,
        claim: EgressClaim,
        *,
        method: str,
        params: dict[str, object],
    ) -> bytes:
        headers = {"Accept": "application/json"}
        if credential_ref is not None:
            async with self._sessions() as session:
                resolver = CredentialResolver(SqlSecretStore(session), self._kek)
                headers["Authorization"] = (
                    f"Bearer {await resolver.resolve(credential_ref)}"
                )
        async with self._client_factory(claim) as client:
            response = await client.request(
                "POST",
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                headers=headers,
            )
        if response.status_code >= 400:
            raise OutboundError(f"the server answered {response.status_code}")
        return response.content

    async def _server_of(self, version_id: UUID) -> tuple[str, str, str | None] | None:
        async with self._sessions() as session:
            found = await session.execute(
                select(
                    McpServerRow.name, McpServerRow.url, McpServerRow.credential_ref
                )
                .join(
                    McpServerVersionRow,
                    McpServerVersionRow.mcp_server_id == McpServerRow.id,
                )
                .where(McpServerVersionRow.id == version_id)
            )
            row = found.first()
        return None if row is None else (row[0], row[1], row[2])


def _answer(body: bytes) -> McpAnswer:
    """One `tools/call` reply, as a tool result can carry it.

    A JSON-RPC error is a refusal with the server's own message; anything else
    comes back as text. The reply is never interpreted further — it is somebody
    else's words on their way into a model's context, and this platform reads
    none of it.
    """
    try:
        document = json.loads(body)
    except ValueError:
        return McpAnswer(content=body.decode("utf-8", errors="replace"))
    if not isinstance(document, dict):
        return McpAnswer(content=json.dumps(document, ensure_ascii=False))
    envelope = cast(dict[str, Any], document)
    failure: Any = envelope.get("error")
    if failure is not None:
        message = (
            str(cast(dict[str, Any], failure).get("message", failure))
            if isinstance(failure, dict)
            else str(failure)
        )
        return McpAnswer(content=message, refusal="server_refused")
    result: Any = envelope.get("result", envelope)
    if isinstance(result, str):
        return McpAnswer(content=result)
    return McpAnswer(content=json.dumps(result, ensure_ascii=False))

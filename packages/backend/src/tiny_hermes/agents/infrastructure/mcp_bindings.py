"""The Agent Catalog's view of the MCP catalog.

The twin of `http_tool_bindings.py`, and the same one line simpler: an MCP
server belongs to exactly one workspace, so visibility is a single comparison.
"""

from collections.abc import Sequence
from urllib.parse import urlsplit
from uuid import UUID

from tiny_hermes.agents.ports.mcp import McpBindingView
from tiny_hermes.mcp.application.service import McpStore
from tiny_hermes.mcp.domain.models import McpServerStatus


class CatalogMcpBindings:
    def __init__(self, store: McpStore) -> None:
        self._store = store

    async def visible_versions(
        self, workspace_id: UUID, version_ids: Sequence[UUID]
    ) -> Sequence[McpBindingView]:
        views: list[McpBindingView] = []
        for version_id in dict.fromkeys(version_ids):
            version = await self._store.get_version(version_id)
            if version is None:
                continue
            server = await self._store.get_server(version.mcp_server_id)
            if server is None or server.workspace_id != workspace_id:
                continue
            host = urlsplit(server.url).hostname
            if host is None:  # pragma: no cover - registration refuses one
                continue
            views.append(
                McpBindingView(
                    mcp_server_id=server.id,
                    version_id=version.id,
                    server_name=server.name,
                    host=host,
                    tool_names=version.tool_names,
                    active=version.status is McpServerStatus.ACTIVE,
                )
            )
        return views

"""The Agent Catalog's view of the HTTP tool catalog.

The twin of `skill_bindings.py`, and one line simpler: an HTTP tool belongs to
exactly one workspace — there is no platform scope for somebody else's API —
so visibility is a single comparison.
"""

from collections.abc import Sequence
from urllib.parse import urlsplit
from uuid import UUID

from tiny_hermes.agents.ports.http_tools import HttpToolBindingView
from tiny_hermes.http_tools.application.service import HttpToolStore
from tiny_hermes.http_tools.domain.models import HttpToolStatus


class CatalogHttpToolBindings:
    def __init__(self, store: HttpToolStore) -> None:
        self._store = store

    async def visible_versions(
        self, workspace_id: UUID, version_ids: Sequence[UUID]
    ) -> Sequence[HttpToolBindingView]:
        views: list[HttpToolBindingView] = []
        for version_id in dict.fromkeys(version_ids):
            version = await self._store.get_version(version_id)
            if version is None:
                continue
            tool = await self._store.get_tool(version.http_tool_id)
            if tool is None or tool.workspace_id != workspace_id:
                continue
            host = urlsplit(tool.base_url).hostname
            if host is None:  # pragma: no cover - registration refuses one
                continue
            views.append(
                HttpToolBindingView(
                    http_tool_id=tool.id,
                    version_id=version.id,
                    tool_name=tool.name,
                    host=host,
                    operation_ids=tuple(
                        operation.operation_id for operation in version.operations
                    ),
                    active=version.status is HttpToolStatus.ACTIVE,
                )
            )
        return views

"""What publishing needs to know about a bound MCP server version.

The twin of `http_tools.py`, and narrow for the same reason: the Agent Catalog
decides whether a draft may be published, not what an MCP server is or who may
register one.

`tool_names` is the snapshot's, not the server's answer today. Publishing
checks that the names an author bound were in what somebody reviewed; whether
the server still offers them is §16.2's revalidation, which happens before each
Run because it can change between one and the next.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class McpBindingView:
    mcp_server_id: UUID
    version_id: UUID
    #: The server's own name, the middle segment of every call name this
    #: version generates. Carried so a refusal can say `docs` rather than a
    #: uuid.
    server_name: str
    #: The host of the server's registered URL, measured against the Agent's
    #: `network.allow`.
    host: str
    tool_names: tuple[str, ...]
    active: bool


class McpBindingReader(Protocol):
    async def visible_versions(
        self, workspace_id: UUID, version_ids: Sequence[UUID]
    ) -> Sequence[McpBindingView]:
        """The named versions this workspace may bind.

        Ids that do not exist and ids belonging elsewhere are both absent from
        the answer, and the caller may not tell them apart.
        """
        ...

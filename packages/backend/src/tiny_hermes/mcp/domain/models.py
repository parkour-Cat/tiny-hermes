"""What the catalog stores for an MCP server, apart from how it stores it.

Product design §16.2. The shape is the HTTP tool catalog's, and deliberately
so: a server is a name and an address in a workspace, and a version is one
immutable snapshot of what that server said it could do.

**Why a snapshot has versions at all.** An MCP server's capabilities are not a
document somebody uploads — they are whatever the server answers today. So a
version here is the platform writing down *what was reviewed*: these tool names,
these schemas, at this moment, approved by this person. A binding names that
snapshot, which is what makes "the subset an administrator agreed to" a fact
rather than a memory.

**And why the runtime does not simply replay it.** §16.2 requires the bound
subset to be revalidated before every Run, so what the model is actually told
comes from a fresh `tools/list` — restricted to the bound names. The snapshot
fixes *which names* may be offered; the server still decides what each one
takes. That split is the whole design: the reviewable thing is the name set,
and pretending a schema is unchanged when the server has moved on would be
telling the model something the far end will reject.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from tiny_hermes.tools.domain.mcp import McpTool

#: Same shape as a skill name, an Agent alias and an HTTP tool name, and for
#: the same reason: it becomes part of a tool name a model types back.
NAME_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
NAME_MAX_LENGTH = 48


class McpServerStatus(StrEnum):
    ACTIVE = "active"
    #: Stops new bindings. A published Agent that already binds this version
    #: keeps working — §15.1's rule, which is about immutable versions rather
    #: than about skills.
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True)
class McpServer:
    id: UUID
    workspace_id: UUID
    name: str
    #: Where `tools/list` and `tools/call` go. Checked against the workspace's
    #: outbound scope at registration, the same as an HTTP tool's base URL.
    url: str
    #: Names an environment variable or a Secret id; never a value.
    credential_ref: str | None
    current_version_id: UUID | None
    #: When the platform last successfully read this server's capabilities.
    #: Reported rather than inferred from a version's timestamp: a server that
    #: has answered unchanged for a month is a different fact from one nobody
    #: has reached since it was registered.
    last_validated_at: datetime | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class McpServerVersion:
    """One reviewed snapshot of what a server advertises."""

    id: UUID
    mcp_server_id: UUID
    version_number: int
    content_hash: str
    tools: tuple[McpTool, ...]
    status: McpServerStatus
    created_by: UUID
    created_at: datetime

    @property
    def bindable(self) -> bool:
        return self.status is McpServerStatus.ACTIVE

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(tool.name for tool in self.tools)

    def tool(self, name: str) -> McpTool | None:
        return next((item for item in self.tools if item.name == name), None)

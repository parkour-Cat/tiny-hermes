"""What a Run needs from the MCP servers its Version bound.

Two questions, because a Run asks two. *What may I offer the model this slice?*
— §16.2 revalidates the bound subset before the Run works, so the answer comes
from asking the servers rather than from replaying a snapshot. And *make this
call* — which goes out the same way every other outbound request does.

Revalidation is per slice rather than per round. Per round would turn one
remote's hiccup into a Run that behaves differently from one model call to the
next; per Run and never again would mean a Run resumed a week later works from
a week-old shape. A slice is the unit the Worker already reasons in.
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from tiny_hermes.agents.domain.models import McpToolBinding
from tiny_hermes.runs.ports.http_calls import EgressClaim
from tiny_hermes.tools.domain.mcp import McpTool


@dataclass(frozen=True)
class BoundMcpTool:
    """One tool an Agent may call, as this slice found it.

    `tool` is what the server says *now*, restricted to a name the Version
    bound. Telling the model a schema the server has moved on from would be
    describing a call the far end will reject.
    """

    server_name: str
    version_id: UUID
    tool: McpTool


@dataclass(frozen=True)
class McpRevalidation:
    """What the bound subset came back as, and what did not come back."""

    tools: tuple[BoundMcpTool, ...]
    #: Servers this platform could not read this slice, by name. Their tools
    #: are simply absent — a Run that lost one of three servers is better off
    #: than one that cannot start — but the Run records that it happened, so a
    #: model that "did not have the tool" is distinguishable from a model that
    #: chose not to use it.
    unreachable: tuple[str, ...] = ()
    #: Bound names no server advertises any more, as `server.tool`. Recorded
    #: for the same reason: a capability that quietly vanished is a Run whose
    #: behaviour changed with nobody publishing anything.
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class McpAnswer:
    """What a call came back as, in terms a tool result can carry."""

    content: str
    #: Set when the call did not happen or the server reported failure. A short
    #: code, and the only thing here that is not the far end's own words.
    refusal: str | None = None

    @property
    def failed(self) -> bool:
        return self.refusal is not None


class McpGateway(Protocol):
    async def revalidate(
        self, bindings: tuple[McpToolBinding, ...], claim: EgressClaim
    ) -> McpRevalidation:
        """Ask each bound server what it offers, and keep the bound names."""
        ...

    async def call(
        self,
        bound: BoundMcpTool,
        arguments: dict[str, object],
        claim: EgressClaim,
    ) -> McpAnswer:
        """Make one `tools/call`, across the outbound boundary."""
        ...

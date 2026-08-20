"""A directory held in memory, for a proxy that has no database yet.

The SQL one arrives with the scope tables (§4 of the plan). This exists so the
process is runnable and testable before them, and so the port has two
implementations from the start — a protocol with one implementation is a
protocol nobody has checked.

It is not a test double: a single-tenant deployment that approves one platform
scope and runs no workspace scoping is served correctly by exactly this.
"""

from dataclasses import dataclass, field
from uuid import UUID

from tiny_hermes.egress.domain.decision import CallerClaim, CallerKind, ScopeLayers
from tiny_hermes.outbound.domain.address_policy import Address
from tiny_hermes.outbound.domain.scope import OutboundScope


@dataclass
class MemoryScopeDirectory:
    platform: OutboundScope = field(default_factory=OutboundScope.nothing)
    workspaces: dict[UUID, OutboundScope] = field(default_factory=dict[UUID, OutboundScope])
    agents: dict[UUID, OutboundScope] = field(default_factory=dict[UUID, OutboundScope])
    runs: dict[UUID, OutboundScope] = field(default_factory=dict[UUID, OutboundScope])
    sandboxes: dict[Address, CallerClaim] = field(
        default_factory=dict[Address, CallerClaim]
    )

    async def layers_for(self, claim: CallerClaim) -> ScopeLayers:
        return ScopeLayers(
            platform=self.platform,
            workspace=_named(self.workspaces, claim.workspace_id),
            agent=_named(self.agents, claim.agent_version_id),
            run=_named(self.runs, claim.run_id),
        )

    async def sandbox_claim(self, address: Address) -> CallerClaim | None:
        return self.sandboxes.get(address)

    def register_sandbox(
        self,
        address: Address,
        *,
        workspace_id: UUID,
        agent_version_id: UUID | None = None,
        run_id: UUID | None = None,
    ) -> None:
        self.sandboxes[address] = CallerClaim(
            kind=CallerKind.SANDBOX,
            workspace_id=workspace_id,
            agent_version_id=agent_version_id,
            run_id=run_id,
        )


def _named(
    known: dict[UUID, OutboundScope], wanted: UUID | None
) -> OutboundScope | None:
    """A layer the claim did not name is absent; one it named and nobody knows
    is empty. The difference decides whether the chain narrows or closes, and
    getting it backwards would let an unknown id open everything the layer
    above approved."""
    if wanted is None:
        return None
    return known.get(wanted, OutboundScope.nothing())

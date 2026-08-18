"""What each layer approved, read from the database the platform already has.

The proxy holds no scope of its own: it asks. That is what makes an
administrator's change take effect on the next connection rather than on the
next restart, and it is why this adapter opens its own short session per
request instead of holding one.

The agent layer is read out of the published `AgentSpec`, not out of a table —
`network` is versioned with everything else an Agent binds, so a Run reaching a
target is measured against what its own version declared rather than against
whatever the Agent says today.
"""

from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiny_hermes.agents.infrastructure.tables import AgentVersionRow
from tiny_hermes.egress.domain.decision import CallerClaim, CallerKind, ScopeLayers
from tiny_hermes.outbound.domain.address_policy import Address
from tiny_hermes.outbound.domain.scope import OutboundScope
from tiny_hermes.outbound.infrastructure.tables import OutboundScopeRow
from tiny_hermes.runs.infrastructure.tables import RunRow
from tiny_hermes.sandbox.infrastructure.tables import SandboxEgressAddressRow


class SqlScopeDirectory:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def layers_for(self, claim: CallerClaim) -> ScopeLayers:
        async with self._sessions() as session:
            return ScopeLayers(
                platform=await _level(session, "platform", None),
                workspace=(
                    None
                    if claim.workspace_id is None
                    else await _level(session, "workspace", claim.workspace_id)
                ),
                agent=(
                    None
                    if claim.agent_version_id is None
                    else await _agent(session, claim.agent_version_id)
                ),
                # The Run layer arrives with delegation in M2E. The intersection
                # has been four layers since it was written, so nothing here
                # changes when it does.
                run=None,
            )

    async def sandbox_claim(self, address: Address) -> CallerClaim | None:
        """Which Run's sandbox is at this address, and what it may be measured
        against.

        The address is the whole of a sandbox's identity: it presents nothing,
        because a process inside a container that holds a credential is one
        that can lend it. `None` is an unknown caller, which the proxy refuses
        before it parses a target — so an address nobody registered cannot even
        use the proxy as a resolver.

        The workspace and the Agent Version come from the Run rather than from
        the registration, so a sandbox is measured against the version its Run
        is executing and nothing has to keep two copies of that agreeing.
        """
        async with self._sessions() as session:
            found = (
                await session.execute(
                    select(RunRow.workspace_id, RunRow.agent_version_id, RunRow.id)
                    .join(
                        SandboxEgressAddressRow,
                        SandboxEgressAddressRow.run_id == RunRow.id,
                    )
                    .where(SandboxEgressAddressRow.address == str(address))
                )
            ).first()
        if found is None:
            return None
        workspace_id, agent_version_id, run_id = found
        return CallerClaim(
            kind=CallerKind.SANDBOX,
            workspace_id=workspace_id,
            agent_version_id=agent_version_id,
            run_id=run_id,
        )


async def _level(
    session: AsyncSession, level: str, workspace_id: UUID | None
) -> OutboundScope:
    query = select(OutboundScopeRow.entry).where(OutboundScopeRow.level == level)
    query = query.where(
        OutboundScopeRow.workspace_id.is_(None)
        if workspace_id is None
        else OutboundScopeRow.workspace_id == workspace_id
    )
    entries = (await session.scalars(query)).all()
    return OutboundScope.of(entries)


async def _agent(session: AsyncSession, version_id: UUID) -> OutboundScope:
    """What this published version declared, or nothing.

    A version id nobody recognizes comes back empty rather than absent, which
    closes the chain. An Agent that declared no `network` also comes back
    empty — an Agent that never asked for the network does not get it because
    its workspace has some.
    """
    spec = await session.scalar(
        select(AgentVersionRow.spec).where(AgentVersionRow.id == version_id)
    )
    if not isinstance(spec, dict):
        return OutboundScope.nothing()
    document: dict[str, Any] = spec
    network = document.get("network")
    if not isinstance(network, dict):
        return OutboundScope.nothing()
    allow = cast(dict[str, object], network).get("allow")
    if not isinstance(allow, list):
        return OutboundScope.nothing()
    entries = cast(list[object], allow)
    return OutboundScope.of(str(entry) for entry in entries)

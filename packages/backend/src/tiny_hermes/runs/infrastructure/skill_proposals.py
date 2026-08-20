"""A Run's way into the catalog, and the only write it is allowed to make.

Its own session and its own transaction, for the reason the skill library uses
one: this happens in the middle of answering a tool call, and joining the
slice's transaction would hold a write open across a model round.

The consequence is worth stating rather than hiding. A round that opened a
proposal and was then rolled back leaves the proposal behind, pointing at a Run
whose turn is not in the transcript. That is the honest side of the trade: the
alternative is a proposal that vanishes because a container failed to freeze,
and a reviewer who is shown a proposal too many can reject it, while one who is
shown none never knows a suggestion existed.
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiny_hermes.agents.infrastructure.tables import AgentVersionRow
from tiny_hermes.runs.infrastructure.tables import RunRow, SessionRow
from tiny_hermes.runs.ports.proposals import ProposalOutcome
from tiny_hermes.skills.application.service import (
    InvalidSkillPackage,
    ProposalLimitReached,
    SkillCatalog,
    SkillCatalogError,
)
from tiny_hermes.skills.domain.package import SkillFile
from tiny_hermes.skills.infrastructure.sql_store import SqlSkillStore
from tiny_hermes.skills.infrastructure.tables import SkillVersionRow


class SqlSkillProposals:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def propose(
        self,
        *,
        run_id: UUID,
        skill_version_id: UUID | None,
        files: Sequence[tuple[str, str]],
    ) -> ProposalOutcome:
        async with self._sessions.begin() as session:
            found = (
                await session.execute(
                    select(RunRow.workspace_id, AgentVersionRow.published_by)
                    .join(SessionRow, SessionRow.id == RunRow.session_id)
                    .join(AgentVersionRow, AgentVersionRow.id == RunRow.agent_version_id)
                    .where(RunRow.id == run_id)
                )
            ).first()
            if found is None:  # pragma: no cover - the Worker holds this Run
                return ProposalOutcome(refusal="this Run is not on record")
            workspace_id, published_by = found
            skill_id = None
            if skill_version_id is not None:
                skill_id = await session.scalar(
                    select(SkillVersionRow.skill_id).where(
                        SkillVersionRow.id == skill_version_id
                    )
                )
                if skill_id is None:
                    return ProposalOutcome(refusal="that skill version is not on record")
            catalog = SkillCatalog(SqlSkillStore(session))
            try:
                proposal = await catalog.propose_from_run(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    # The person who published the Agent Version. See
                    # `propose_from_run` for why it is them and not the caller
                    # who happened to start this Run.
                    created_by=published_by,
                    files=[SkillFile(path=path, text=text) for path, text in files],
                    request_id=f"run-{run_id}",
                    skill_id=skill_id,
                    base_version_id=skill_version_id,
                )
            except ProposalLimitReached as refused:
                return ProposalOutcome(refusal=str(refused))
            except InvalidSkillPackage as refused:
                return ProposalOutcome(refusal=refused.reason)
            except SkillCatalogError as refused:
                # Every remaining catalog refusal is something the model could
                # write differently — a name that does not match the skill, a
                # skill that is not there any more.
                return ProposalOutcome(refusal=str(refused) or type(refused).__name__)
            return ProposalOutcome(proposal_id=proposal.id)

"""A Run's one write into memory, and the policy that stands between them.

Its own session and its own transaction, the reason `SqlSkillProposals` gives:
this runs in the middle of answering a tool call, and joining the slice's
transaction would hold a write open across a model round. The honest cost is
the same too — a round that proposed a memory and was then rolled back leaves
the candidate behind, pointing at a Run whose turn is not in the transcript. A
reviewer shown one candidate too many can refuse it; one shown none never knew
a suggestion existed.

The scope is read off the Run, never taken from the caller: a Run proposes a
memory about the person it is working with — the Session's `CallerIdentity` —
and about nobody else. A Run only ever writes a **private** candidate; §14.2's
shared memory has its own two doors and a running Agent is neither.

What happens to the candidate is `policy.decide`, not anything decided here.
The rule check's answer is passed to it rather than folded into it, so the
policy stays readable as three branches and a change to the rules cannot
quietly change what a policy means.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiny_hermes.agents.infrastructure.tables import AgentVersionRow
from tiny_hermes.memory.domain.policy import (
    CandidateOutcome,
    MemoryPolicy,
    decide,
    status_for,
)
from tiny_hermes.memory.domain.risk import assess
from tiny_hermes.memory.domain.scope import MemoryKind
from tiny_hermes.memory.infrastructure.tables import MemoryRow
from tiny_hermes.runs.domain.models import CallerType
from tiny_hermes.runs.infrastructure.tables import RunRow, SessionRow
from tiny_hermes.runs.ports.memories import CandidateResult
from tiny_hermes.tenancy.infrastructure.tables import WorkspaceRow


class SqlMemoryCandidates:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def propose(self, *, run_id: UUID, body: str) -> CandidateResult:
        async with self._sessions.begin() as session:
            found = (
                await session.execute(
                    select(
                        RunRow.workspace_id,
                        AgentVersionRow.agent_id,
                        SessionRow.caller_type,
                        SessionRow.caller_id,
                        WorkspaceRow.memory_policy,
                    )
                    .join(SessionRow, SessionRow.id == RunRow.session_id)
                    .join(
                        AgentVersionRow,
                        AgentVersionRow.id == RunRow.agent_version_id,
                    )
                    .join(WorkspaceRow, WorkspaceRow.id == RunRow.workspace_id)
                    .where(RunRow.id == run_id)
                )
            ).first()
            if found is None:  # pragma: no cover - the Worker holds this Run
                return CandidateResult(
                    CandidateOutcome.REFUSED, detail="this Run is not on record"
                )
            workspace_id, agent_id, caller_type, caller_id, policy_value = found

            # The rule check runs regardless of policy; `decide` consults it
            # only under `low_risk_auto`. Kept out of the branch so a reviewer
            # reading a `pending` candidate could still be shown why it did not
            # qualify, and so the two decisions stay separable.
            low_risk = assess(body).low_risk
            decision = decide(
                MemoryPolicy(policy_value), MemoryKind.PRIVATE, low_risk=low_risk
            )
            if decision.outcome is CandidateOutcome.REFUSED:
                return CandidateResult(CandidateOutcome.REFUSED, detail=decision.reason)

            now = datetime.now(UTC)
            memory_id = uuid4()
            session.add(
                MemoryRow(
                    id=memory_id,
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    kind=MemoryKind.PRIVATE.value,
                    subject_type=CallerType(caller_type).value,
                    subject_id=caller_id,
                    body=body,
                    status=status_for(decision.outcome).value,
                    origin="run",
                    origin_run_id=run_id,
                    context={},
                    created_by=None,
                    updated_at=now,
                )
            )
            return CandidateResult(decision.outcome, memory_id=memory_id)

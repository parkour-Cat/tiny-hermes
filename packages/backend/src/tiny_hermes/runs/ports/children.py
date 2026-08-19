"""A Run's one way to hand work to another Agent.

One method, and it **asks** rather than creates. What a child ends up permitted
to do is the intersection of its parent's scope and the delegation policy, and
this port deliberately does not let the caller pass a scope: a Run that could
name its children's permissions would be a Run deciding what §13 says the
published policy decides.

The answer names every child that was created, because the parent has to be
able to say who it is waiting for. A refusal comes back as a sentence rather
than an exception for the reason every other tool answer does — a model left
without an answer to a call it made will retry it or invent what it returned.
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from tiny_hermes.runs.domain.models import WaitPolicy


@dataclass(frozen=True)
class DelegationRequest:
    """One piece of work, and which bound alias is to do it."""

    alias: str
    #: The whole task. A child sees this and nothing else — §13's seventh
    #: clause keeps the parent's transcript out of it — so this is not a
    #: summary of the work, it is the work.
    instruction: str
    #: Artifact ids the parent is passing down. Never paths: §13's eighth
    #: clause moves files as authorizations, and a path would be the shape of
    #: a shared directory. Whether the parent may pass any of these on is
    #: decided where the grants are written.
    artifacts: tuple[str, ...] = ()


@dataclass(frozen=True)
class DelegatedChild:
    run_id: UUID
    #: Its own, never the parent's. A child holding the parent's Session would
    #: share the parent's SessionWorkspace, which is the shape §13's eighth
    #: clause exists to prevent.
    session_id: UUID
    alias: str


@dataclass(frozen=True)
class DelegationResult:
    """What was created, or why nothing was.

    Never partial. Either every requested child exists or none does: a parent
    told it delegated three pieces of work and given two would wait for a piece
    nobody is doing, and it has no way to notice.
    """

    children: tuple[DelegatedChild, ...] = ()
    #: Empty when children were created. A sentence, not a code — the model
    #: reads it and has to be able to act on it.
    refusal: str = ""

    @property
    def refused(self) -> bool:
        return not self.children


@dataclass(frozen=True)
class DelegationWait:
    """What a parent is waiting for, decided when the delegation was made.

    Both fields are settled here rather than when the wait is later evaluated,
    for the reason §16.3 hashes an approved call: a policy read at settlement
    time is a policy that could have changed while the Run was asleep, and the
    parent would then be woken by a rule it never agreed to.
    """

    #: The children this wait is about. Named explicitly rather than derived
    #: from "every child of this Run", because a long Run may delegate twice
    #: and the second wait is not about the first batch.
    child_run_ids: tuple[UUID, ...]
    policy: WaitPolicy
    #: How long the parent will hang on before the deadline makes it
    #: `paused(external_timeout)`.
    seconds: int


class ChildRuns(Protocol):
    async def delegate(
        self, *, parent_run_id: UUID, requests: tuple[DelegationRequest, ...]
    ) -> DelegationResult:
        """Create one child Run per request, or none of them.

        The parent is named by id rather than passed in as a scope, a spec or a
        depth: every one of those is a fact about a row, and an argument for it
        is an argument somebody can get wrong. §13's third clause in particular
        is decided from the parent's own `depth` here, where a child Agent
        cannot reach it.
        """
        ...

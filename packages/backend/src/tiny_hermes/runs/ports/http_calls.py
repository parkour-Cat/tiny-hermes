"""Where a Run's HTTP tool calls actually go.

One method, for the reason `SkillLibrary` has one: whether this operation may
be called, by whom, and against which host was all decided before the Run
existed. What is left is sending a composed request and bringing an answer
back.

The credential is named here and resolved on the other side of this port. A
Worker holding a bearer token would be a Worker that can put one in a prompt.
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from tiny_hermes.tools.domain.http_calls import HttpRequestPlan


@dataclass(frozen=True)
class EgressClaim:
    """Which layers this call asks to be measured against.

    §16.5's chain is platform ∩ workspace ∩ Agent ∩ Run, and the Agent layer is
    where an Agent's own `network.allow` lives. A call that named no layers
    would be measured against the platform alone — every Agent in the
    installation reaching everything the platform approved. Naming them can
    only narrow: the proxy looks each id up itself and never believes what the
    caller says about it.
    """

    workspace_id: UUID
    agent_version_id: UUID
    run_id: UUID


@dataclass(frozen=True)
class HttpToolAnswer:
    """What came back, as a tool result needs it.

    `refusal` is set when the call did not happen — the boundary said no, the
    credential is missing, the target was unreachable. It is a short code, and
    it is the only thing in this object that is not the far end's own words.
    """

    status_code: int | None
    body: str
    refusal: str | None = None

    @property
    def failed(self) -> bool:
        return self.refusal is not None or (
            self.status_code is not None and self.status_code >= 400
        )


class HttpToolSender(Protocol):
    async def send(
        self,
        plan: HttpRequestPlan,
        credential_ref: str | None,
        claim: EgressClaim,
    ) -> HttpToolAnswer:
        """Send one composed request through the platform's outbound face."""
        ...

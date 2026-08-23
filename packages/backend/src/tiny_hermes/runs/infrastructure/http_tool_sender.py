"""Sending an HTTP tool call, on the platform's side of the boundary.

Shaped after `ModelRouter`, which resolves a credential and makes a call for
the same reasons: a session per resolution, a client per call, and the key held
only between the two.

Three decisions worth stating.

**The platform sends it, not the sandbox.** The request leaves through
`SafeOutboundClient` and therefore through the egress proxy, where §16.5's
four-layer scope is checked — the same path every other outbound call this
process makes. A sandbox that made these calls itself would need the credential
inside the container, and a credential inside a container is one the model can
read.

**The credential is resolved here and returned nowhere.** `HttpToolAnswer`
carries a status and a body, so nothing travelling back toward the conversation
has ever held one.

**Nothing here says which environment variable is unset.** A model told that
would learn the shape of this deployment's configuration from a Run it was
merely given.
"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiny_hermes.model_catalog.infrastructure.credentials import (
    CredentialMissing,
    CredentialResolver,
)
from tiny_hermes.outbound.client import SafeOutboundClient
from tiny_hermes.outbound.errors import (
    OutboundRefused,
    OutboundTooLarge,
    OutboundTooManyRedirects,
    OutboundUnreachable,
)
from tiny_hermes.runs.infrastructure.credential_echo import without_credential
from tiny_hermes.runs.ports.http_calls import EgressClaim, HttpToolAnswer
from tiny_hermes.secrets.infrastructure.sql_store import SqlSecretStore
from tiny_hermes.tools.domain.http_calls import MAX_RESPONSE_BYTES, HttpRequestPlan


class OutboundHttpToolSender:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client_factory: Callable[
            [EgressClaim], AbstractAsyncContextManager[SafeOutboundClient]
        ],
        kek: bytes | None = None,
    ) -> None:
        self._sessions = session_factory
        self._client_factory = client_factory
        self._kek = kek

    async def send(
        self,
        plan: HttpRequestPlan,
        credential_ref: str | None,
        claim: EgressClaim,
    ) -> HttpToolAnswer:
        headers = dict(plan.headers)
        # `None` when this tool carries no credential, which is also the
        # value `without_credential` reads as "nothing to look for".
        token: str | None = None
        if credential_ref is not None:
            async with self._sessions() as session:
                resolver = CredentialResolver(SqlSecretStore(session), self._kek)
                try:
                    token = await resolver.resolve(credential_ref)
                except CredentialMissing:
                    return HttpToolAnswer(
                        status_code=None, body="", refusal="credential_unavailable"
                    )
            headers["Authorization"] = f"Bearer {token}"
        # The claim is built per call rather than per Worker: the layers are
        # facts about this Run, and a client that carried one Run's ids into
        # another's would be measuring the wrong Agent.
        async with self._client_factory(claim) as client:
            try:
                response = await client.request(plan.method, plan.url, headers=headers)
            except OutboundRefused as refused:
                # The boundary said no, not the far end. Named with the scope's
                # own reason so a person reading the transcript can tell "this
                # workspace never approved that host" from "the API was down".
                return HttpToolAnswer(
                    status_code=None, body="", refusal=refused.reason.value
                )
            except OutboundTooManyRedirects:
                return HttpToolAnswer(
                    status_code=None, body="", refusal="too_many_redirects"
                )
            except OutboundTooLarge:
                return HttpToolAnswer(
                    status_code=None, body="", refusal="response_too_large"
                )
            except OutboundUnreachable:
                return HttpToolAnswer(
                    status_code=None, body="", refusal="endpoint_unreachable"
                )
        body = response.content
        if len(body) > MAX_RESPONSE_BYTES:
            # Refused rather than truncated, the rule `skill.load` set: a model
            # handed half a document has no way to know it is holding half. The
            # status is kept, because "it answered and the answer was too big"
            # is different from "it never answered".
            return HttpToolAnswer(
                status_code=response.status_code, body="", refusal="response_too_large"
            )
        return HttpToolAnswer(
            status_code=response.status_code,
            # `errors="replace"` rather than a refusal: an endpoint answering in
            # an encoding this platform did not expect still said something, and
            # a mangled character is more use to the model than nothing.
            body=without_credential(body.decode("utf-8", errors="replace"), token),
        )

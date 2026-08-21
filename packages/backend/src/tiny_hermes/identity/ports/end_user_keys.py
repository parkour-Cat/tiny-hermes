"""What `EndUserIdentityService` needs to turn a `channel_issuers` row into
one PEM key. A protocol, not `OutboundJwksKeySource` directly, so exchange's
orchestration logic can be tested without a socket — the same reason
`SkillCatalog` takes a `TarballSource` protocol instead of
`OutboundTarballSource` itself.
"""

from typing import Protocol


class EndUserKeySource(Protocol):
    async def resolve(
        self, *, public_key: str | None, jwks_url: str | None, token: str
    ) -> str | None:
        """A PEM public key, or `None` if this row's key cannot be resolved.

        `None` is not an error value here — a JWKS endpoint that is down, or
        a `kid` that matches nothing published, is routine enough that the
        caller decides what it costs (§8: still the price of a real
        signature check, same as everything else), not this port.
        """
        ...

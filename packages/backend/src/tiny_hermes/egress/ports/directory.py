"""Where the proxy learns what each layer approved.

Two lookups, and the split between them is the security shape of the whole
process. `layers_for` answers "what may this claim reach", and the claim is
already established. `sandbox_claim` *establishes* one, from the only thing a
process inside a container cannot forge by holding it: the address its packets
came from.

Nothing here writes. A proxy that could change a scope would be a proxy worth
attacking for something other than reaching one host.
"""

from typing import Protocol

from tiny_hermes.egress.domain.decision import CallerClaim, ScopeLayers
from tiny_hermes.outbound.domain.address_policy import Address


class ScopeDirectory(Protocol):
    async def layers_for(self, claim: CallerClaim) -> ScopeLayers:
        """What each named layer approved.

        A layer the claim does not name comes back as `None` — a model call
        belongs to the platform and names no workspace. A layer that is named
        and unknown comes back **empty**, not absent: an id nobody recognizes
        must close the chain rather than drop out of it.
        """
        ...

    async def sandbox_claim(self, address: Address) -> CallerClaim | None:
        """Which Run's sandbox lives at this address, if any.

        `None` is an unknown caller, which the proxy refuses before it parses a
        target. Registered by whatever creates the sandbox network, so the
        mapping's lifetime is the container's rather than the Run's.
        """
        ...

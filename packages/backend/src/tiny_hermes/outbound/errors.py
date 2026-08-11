"""What can go wrong on the way out, in terms the caller can act on.

Every one of these carries whether the other end may have done something.
`external_effect_unknown` is not a detail: it is what decides whether a Run may
be retried, and guessing it wrong either loses work or bills for it twice.
"""

from tiny_hermes.outbound.domain.address_policy import RefusalReason


class OutboundError(Exception):
    """Base for every refusal or failure of an outbound call."""

    #: Whether the far end may have acted on a request it never answered.
    external_effect_unknown: bool = False


class OutboundRefused(OutboundError):
    """The platform declined to make the call.

    Nothing was sent, so nothing happened anywhere. The resolved address is kept
    for the audit trail and is deliberately not part of `str(self)`: a refusal
    shown to a workspace member must not become a way to map the network the
    platform runs on.
    """

    def __init__(self, reason: RefusalReason, *, address: str | None = None) -> None:
        super().__init__(f"outbound call refused: {reason.value}")
        self.reason = reason
        self.address = address


class OutboundUnreachable(OutboundError):
    """The call was attempted and did not produce a response."""

    def __init__(self, detail: str, *, effect_unknown: bool) -> None:
        super().__init__(detail)
        self.external_effect_unknown = effect_unknown


class OutboundTooManyRedirects(OutboundError):
    """The endpoint kept pointing somewhere else."""

    external_effect_unknown = True


class OutboundTooLarge(OutboundError):
    """The response exceeded what this platform is willing to hold in memory.

    The endpoint answered, so it did whatever it was asked to do; only the
    answer is unusable.
    """

    external_effect_unknown = True

"""Turning the image references in a round into bytes the provider can send.

Here rather than in the provider, because fetching a channel's image is
authenticated with that channel's credentials — a model adapter holding a
binding's app secret would be a credential in the wrong module. The Worker
asks an injected port and hands the provider a finished map; the provider
only sends what it was given, and refuses a block naming something absent.
"""

import logging
from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from tiny_hermes.runs.domain.models import CanonicalMessage, ImageBlock

#: Why a failure here degrades rather than raising, reversed from the first
#: version by what it did to a real conversation.
#:
#: Raising was reasoned from the current turn: a question about a picture,
#: answered without the picture, is a confident lie. True — and a Session
#: replays its whole history on every round, so one permanently unfetchable
#: image failed *every future Run* in that conversation. Four of them made a
#: live Session unusable forever, and `image_unavailable` was the only thing
#: anybody could get out of it.
#:
#: The reply is a marker instead. The model is told plainly that an image
#: could not be retrieved, which is neither silence nor a confident answer
#: about something it cannot see.


logger = logging.getLogger(__name__)


class ImageSource(Protocol):
    """Whatever knows how to turn one reference into a `data:` URL.

    Narrow on purpose: the Worker must not learn which surface a reference
    came from, and this is the whole of what it needs.

    `session_id` is a parameter rather than something the implementation is
    constructed with, because a Worker is one per process and a Session is
    one per Run. Binding it at construction would fetch a second workspace's
    image with the first workspace's token.
    """

    async def data_url_for(self, reference: str, session_id: UUID) -> str: ...


async def resolve_images(
    messages: Sequence[CanonicalMessage],
    source: ImageSource | None,
    session_id: UUID,
) -> dict[str, str]:
    """Every distinct image reference in this round, fetched.

    Distinct is the point. A conversation replays its earlier turns on every
    round, so one photograph is seen again and again — resolving per
    occurrence would re-download it once per round for the life of the
    Session.

    Ordered rather than gathered concurrently: images are rare, a round
    carries one or two, and a burst of parallel downloads against a
    rate-limited vendor buys nothing worth the failure modes.
    """
    wanted = [
        block.reference
        for message in messages
        for block in message.blocks
        if isinstance(block, ImageBlock)
    ]
    if not wanted or source is None:
        return {}
    found: dict[str, str] = {}
    for reference in wanted:
        if reference in found:
            continue
        try:
            found[reference] = await source.data_url_for(reference, session_id)
        except Exception:
            # One bad reference must not take a good one with it, and none of
            # them may take the conversation.
            logger.warning(
                "image could not be fetched: %s", reference, exc_info=True
            )
    return found


__all__ = ["ImageSource", "resolve_images"]

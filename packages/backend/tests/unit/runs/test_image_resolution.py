"""Resolving the images a round carries, before the request is built.

The Worker does this, not the provider. Fetching a Feishu image is
authenticated with that binding's app secret, and a model adapter holding a
channel's credentials would be a credential in the wrong module — so the
Worker asks an injected port and hands the provider a finished map.

The port is optional. A deployment with no channel that sends images has
nothing to inject, and a Run whose messages carry no image must not need
one — that is most Runs.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from tiny_hermes.runs.application.images import resolve_images
from tiny_hermes.runs.domain.models import CanonicalMessage, ImageBlock, TextBlock


class _Resolver:
    def __init__(self, answers: dict[str, str] | None = None, fails: bool = False) -> None:
        self.answers = answers or {}
        self.fails = fails
        self.asked: list[str] = []

    async def data_url_for(self, reference: str, session_id: UUID) -> str:
        del session_id
        self.asked.append(reference)
        if self.fails:
            raise RuntimeError("feishu said no")
        return self.answers[reference]


SESSION = uuid4()


def _turn(*blocks: Any) -> CanonicalMessage:
    return CanonicalMessage(role="user", blocks=tuple(blocks))


async def test_a_round_with_no_images_asks_nothing() -> None:
    """Most Runs. Touching the resolver here would make every text-only Run
    depend on a channel being reachable."""
    resolver = _Resolver()

    found = await resolve_images((_turn(TextBlock(text="hi")),), resolver, SESSION)

    assert found == {}
    assert resolver.asked == []


async def test_each_reference_is_resolved_once() -> None:
    resolver = _Resolver({"feishu:om_1:k": "data:image/png;base64,AAA"})

    found = await resolve_images(
        (
            _turn(ImageBlock(reference="feishu:om_1:k", media_type="image/png")),
            _turn(ImageBlock(reference="feishu:om_1:k", media_type="image/png")),
        ),
        resolver,
        SESSION,
    )

    assert found == {"feishu:om_1:k": "data:image/png;base64,AAA"}
    # A conversation replays every earlier turn on every round, so the same
    # picture is seen again and again. Fetching per occurrence would download
    # it once per round for the life of the Session.
    assert resolver.asked == ["feishu:om_1:k"]


async def test_no_resolver_and_no_images_is_fine() -> None:
    """A deployment with no image-capable channel injects nothing."""
    assert await resolve_images((_turn(TextBlock(text="hi")),), None, SESSION) == {}


async def test_an_image_with_no_resolver_is_refused() -> None:
    """Rather than dropped. Sending the question without the picture gets a
    confident answer about nothing, and there is no configuration mistake
    more silent than that."""
    from tiny_hermes.runs.application.images import ImagesUnresolvable

    with pytest.raises(ImagesUnresolvable):
        await resolve_images(
            (_turn(ImageBlock(reference="feishu:om_1:k", media_type="image/png")),),
            None,
            SESSION,
        )


async def test_a_failing_fetch_is_refused_rather_than_skipped() -> None:
    from tiny_hermes.runs.application.images import ImagesUnresolvable

    with pytest.raises(ImagesUnresolvable):
        await resolve_images(
            (_turn(ImageBlock(reference="feishu:om_1:k", media_type="image/png")),),
            _Resolver(fails=True),
            SESSION,
        )

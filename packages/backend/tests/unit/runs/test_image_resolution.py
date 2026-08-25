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


async def test_an_image_with_no_resolver_resolves_to_nothing() -> None:
    """A deployment with no image-capable channel. The block becomes a
    marker downstream rather than failing the round — see
    `test_an_unfetchable_image_does_not_stop_the_round` for why raising here
    turned out to be wrong."""
    found = await resolve_images(
        (_turn(ImageBlock(reference="feishu:om_1:k", media_type="image/png")),),
        None,
        SESSION,
    )

    assert found == {}





async def test_an_unfetchable_image_does_not_stop_the_round() -> None:
    """A design decision reversed by what it did to a real conversation.

    The first version raised, on the reasoning that a question about a
    picture answered without the picture is a confident lie. That is true of
    the *current* turn — and a Session replays its whole history on every
    round, so one permanently unfetchable picture failed every future Run in
    that conversation. Four such messages made a live Session unusable
    forever, with `image_unavailable` as the only thing anybody could send
    to it.

    So a failure degrades to a marker instead. The model is told plainly
    that an image could not be retrieved, which is neither silence nor a
    confident answer about something it cannot see.
    """
    found = await resolve_images(
        (_turn(ImageBlock(reference="feishu:om_1:k", media_type="image/png")),),
        _Resolver(fails=True),
        SESSION,
    )

    assert found == {}


async def test_the_pictures_that_do_resolve_still_arrive() -> None:
    """One bad reference must not take a good one with it."""
    resolver = _Resolver({"feishu:om_1:good": "data:image/png;base64,AAA"})

    found = await resolve_images(
        (
            _turn(
                ImageBlock(reference="feishu:om_1:good", media_type="image/png"),
                ImageBlock(reference="feishu:om_1:gone", media_type="image/png"),
            ),
        ),
        resolver,
        SESSION,
    )

    assert found == {"feishu:om_1:good": "data:image/png;base64,AAA"}

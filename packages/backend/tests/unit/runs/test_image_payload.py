"""Sending an image to an OpenAI-compatible endpoint, or refusing to.

Two things have to be true before an image goes out, and they fail in
opposite ways if either is missing.

The endpoint has to *accept* images. Declared per endpoint, never inferred
from the model name — the same rule §7.4.2 applies to `context_window`,
and for the same reason: DeepSeek's vision support lives under a different
model id (`deepseek-v4-flash-vision-exp`), and a name-sniffing check would
be a guess that silently becomes wrong the next time a vendor renames
something.

And the bytes have to be *available*. `ImageBlock` carries a reference,
because a transcript row must not hold a megabyte; `build_payload` is a
pure function, because a request builder that reads storage cannot be
tested or replayed. So the caller resolves references to data URLs and
hands them in — the seam is explicit rather than either half bending.
"""

from typing import Any

from tiny_hermes.agents.domain.models import DeterministicModelPolicy
from tiny_hermes.model_catalog.domain.models import ModelEndpointSpec, UsageQuality
from tiny_hermes.runs.domain.models import CanonicalMessage, ImageBlock, TextBlock
from tiny_hermes.runs.infrastructure.openai_model import build_payload
from tiny_hermes.runs.ports.model import ModelRequest

PIXEL = "data:image/png;base64,iVBORw0KGgo="


def _spec(*, accepts_images: bool = True) -> ModelEndpointSpec:
    return ModelEndpointSpec(
        name="vision-endpoint",
        base_url="https://example.invalid/v1",
        model="deepseek-v4-flash-vision-exp",
        context_window=128_000,
        max_output_tokens=4_096,
        usage_quality=UsageQuality.PROVIDER,
        credential_ref="KEY",
        accepts_images=accepts_images,
    )


def _turn() -> CanonicalMessage:
    return CanonicalMessage(
        role="user",
        blocks=(
            TextBlock(text="这张图里是什么?"),
            ImageBlock(reference="a1", media_type="image/png"),
        ),
    )


def _payload(**overrides: Any) -> dict[str, Any]:
    settings: dict[str, Any] = {"images": {"a1": PIXEL}}
    settings.update(overrides)
    return build_payload(
        _spec(accepts_images=settings.pop("accepts_images", True)),
        ModelRequest(
            policy=DeterministicModelPolicy(),
            personality="You are careful.",
            messages=(_turn(),),
            round_index=1,
        ),
        **settings,
    )


def test_an_image_goes_out_as_a_content_part() -> None:
    sent = _payload()["messages"]

    user = [m for m in sent if m["role"] == "user"][-1]
    kinds = [part["type"] for part in user["content"]]
    assert "text" in kinds
    assert "image_url" in kinds


def test_the_image_carries_the_resolved_data_url() -> None:
    sent = _payload()["messages"]

    user = [m for m in sent if m["role"] == "user"][-1]
    image = next(p for p in user["content"] if p["type"] == "image_url")
    assert image["image_url"]["url"] == PIXEL


def test_a_text_only_turn_still_sends_a_plain_string() -> None:
    """The wire shape every other endpoint expects. Wrapping every message
    in a parts array to accommodate images would change every request this
    platform has ever sent, for the sake of the rare one that has a photo."""
    payload = build_payload(
        _spec(),
        ModelRequest(
            policy=DeterministicModelPolicy(),
            personality="You are careful.",
            messages=(CanonicalMessage(role="user", blocks=(TextBlock(text="hi"),)),),
            round_index=1,
        ),
    )

    user = [m for m in payload["messages"] if m["role"] == "user"][-1]
    assert user["content"] == "hi"


def test_an_endpoint_that_does_not_accept_images_refuses_before_sending() -> None:
    """Refused here, not by the endpoint.

    Sending anyway earns a provider 400 whose text is the vendor's, in the
    vendor's terms, surfaced to a person in Feishu as `endpoint_status:400`.
    A named refusal is something the platform can explain and an
    administrator can act on: this endpoint needs a model that accepts
    images.
    """
    import pytest
    from tiny_hermes.runs.infrastructure.openai_model import ImagesNotAccepted

    with pytest.raises(ImagesNotAccepted):
        _payload(accepts_images=False)


def test_an_unresolved_reference_refuses_rather_than_sending_a_gap() -> None:
    """A caller that forgot to resolve one. Dropping the block would send a
    question about a picture with no picture, and the model would answer
    about nothing — confidently."""
    import pytest
    from tiny_hermes.runs.infrastructure.openai_model import ImageUnavailable

    with pytest.raises(ImageUnavailable):
        _payload(images={})


def test_the_default_is_to_accept_nothing() -> None:
    """Every endpoint registered before this field existed reads as
    text-only, which is what they are."""
    spec = ModelEndpointSpec(
        name="plain",
        base_url="https://example.invalid/v1",
        model="deepseek-v4-flash",
        context_window=128_000,
        max_output_tokens=4_096,
        usage_quality=UsageQuality.PROVIDER,
        credential_ref="KEY",
    )

    assert spec.accepts_images is False

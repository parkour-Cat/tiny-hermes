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

import logging
from typing import Any

import pytest
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


def test_an_endpoint_that_does_not_accept_images_says_so_in_the_request() -> None:
    """Not sent, and not silently dropped either.

    The picture is left out — this endpoint would refuse it with a vendor
    400 — and a marker takes its place so the model answers that it cannot
    see the image. Raising here was the first design and it was wrong for
    the same reason the resolver's was: a Session replays its history, so
    one image on a text-only endpoint failed every future Run in that
    conversation.
    """
    sent = _payload(accepts_images=False)["messages"]

    user = [m for m in sent if m["role"] == "user"][-1]
    kinds = [part["type"] for part in user["content"]]
    assert "image_url" not in kinds
    assert any("could not be retrieved" in part.get("text", "") for part in user["content"])


def test_an_unresolved_reference_is_named_rather_than_left_as_a_gap() -> None:
    """The picture could not be fetched. The model is told that, rather than
    being handed a question about a picture with no picture — which it would
    answer confidently — or the round being failed, which permanently broke
    a live conversation."""
    sent = _payload(images={})["messages"]

    user = [m for m in sent if m["role"] == "user"][-1]
    assert any("could not be retrieved" in part.get("text", "") for part in user["content"])


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


def test_the_request_says_out_loud_how_many_images_it_carries(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """What was actually sent, at INFO, once per request.

    Not scaffolding. Every layer of this feature was individually provable —
    the endpoint flag, the download, the data URL, the payload builder, the
    vendor — and every one of them passed while a live conversation still
    answered "I cannot see the picture". Nothing in a green suite says which
    request went out, so the only remaining question, *did this round carry
    the image*, took a database, seven probes and the vendor's own API to
    answer. This is the line that answers it in one grep.
    """
    with caplog.at_level(logging.INFO, logger="tiny_hermes.runs.infrastructure.openai_model"):
        build_payload(
            _spec(accepts_images=True),
            ModelRequest(
                policy=DeterministicModelPolicy(),
                personality="You are careful.",
                messages=(_turn(), _turn()),
                round_index=1,
            ),
            images={"a1": PIXEL},
        )

    assert "images attached=2 missing=0 accepts_images=True" in caplog.text


def test_the_diagnostic_counts_what_was_left_out_separately(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`attached` and `missing` are different failures with different fixes —
    an endpoint that was never declared to accept images versus a reference
    the resolver could not fetch. One number for both would name neither."""
    with caplog.at_level(logging.INFO, logger="tiny_hermes.runs.infrastructure.openai_model"):
        build_payload(
            _spec(accepts_images=False),
            ModelRequest(
                policy=DeterministicModelPolicy(),
                personality="You are careful.",
                messages=(_turn(),),
                round_index=1,
            ),
            images={"a1": PIXEL},
        )

    assert "images attached=0 missing=1 accepts_images=False" in caplog.text


def test_a_request_with_no_images_stays_quiet() -> None:
    """Almost every request has no picture in it. A line per round saying so
    would bury the one that matters."""
    caplog_free = build_payload(
        _spec(),
        ModelRequest(
            policy=DeterministicModelPolicy(),
            personality="You are careful.",
            messages=(CanonicalMessage(role="user", blocks=(TextBlock(text="hi"),)),),
            round_index=1,
        ),
    )

    assert caplog_free["messages"][-1]["content"] == "hi"


def test_the_marker_speaks_only_for_the_message_it_sits_in() -> None:
    """It is injected per message, so it must not describe the conversation.

    The wording used to say "in this conversation". A live session proved
    what that costs: two images belonged to messages their sender had
    *recalled* and could never be fetched again, so the phrase appeared on
    every round — and the model read it as a standing fact about all images,
    refusing to look at the seven that were attached beside it. Replaying
    that history with the same seven images and this sentence removed, the
    model described the picture correctly.

    So this is the rule CLAUDE.md states for comments, applied to a string
    the model reads: it must not claim more than the code does.
    """
    sent = _payload(images={})["messages"]

    user = [m for m in sent if m["role"] == "user"][-1]
    marker = next(
        part["text"] for part in user["content"] if "could not be retrieved" in part.get("text", "")
    )
    assert "this message" in marker
    assert "conversation" not in marker


def test_two_unfetchable_images_are_counted_once() -> None:
    """One turn can carry several pictures, and the count is stated once.

    Written because the first attempt at the message-scoped wording built
    the sentence from a count *and* a pluralised noun that already carried
    the count, reading "[2 2 images ...]". The singular case hid it.
    """
    turn = CanonicalMessage(
        role="user",
        blocks=(
            TextBlock(text="这两张呢?"),
            ImageBlock(reference="a1", media_type="image/png"),
            ImageBlock(reference="a2", media_type="image/png"),
        ),
    )
    payload = build_payload(
        _spec(),
        ModelRequest(
            policy=DeterministicModelPolicy(),
            personality="You are careful.",
            messages=(turn,),
            round_index=1,
        ),
        images={},
    )

    user = [m for m in payload["messages"] if m["role"] == "user"][-1]
    marker = next(
        p["text"] for p in user["content"] if "could not be retrieved" in p.get("text", "")
    )
    assert marker == "[2 images in this message could not be retrieved and are not shown]"

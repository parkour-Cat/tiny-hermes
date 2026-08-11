"""The adapter against a real socket: retries, refusals, and what gets sent.

The stand-in is a local server this repository controls. No test here calls a
vendor endpoint — CI has no credential and must never need one, and a suite that
depends on somebody else's uptime goes red for reasons unrelated to this code.
The one run against a real endpoint is a documented manual step in the
verification record.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from ipaddress import ip_network
from typing import Any

import pytest
import uvicorn
from tiny_hermes.model_catalog.domain.models import ModelEndpointSpec
from tiny_hermes.outbound.client import SafeOutboundClient
from tiny_hermes.outbound.domain.address_policy import (
    Address,
    AddressVerdict,
    Network,
    verdict,
)
from tiny_hermes.runs.domain.models import CanonicalMessage
from tiny_hermes.runs.infrastructure.openai_model import (
    SAFETY_PREAMBLE,
    OpenAICompatibleProvider,
    RetryPolicy,
)
from tiny_hermes.runs.ports.model import ModelRequest, StopReason, UsageQuality

CREDENTIAL = "TINY_HERMES_TEST_MODEL_KEY"


@dataclass
class FakeModel:
    """An OpenAI-compatible endpoint, as far as one round can tell."""

    seen: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    authorizations: list[str] = field(default_factory=list[str])
    #: Statuses to answer with before succeeding, consumed one per request.
    failures: list[int] = field(default_factory=list[int])
    answer: str = "the answer"
    usage: dict[str, Any] | None = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            # uvicorn opens with a lifespan scope, which carries no headers and
            # is not a request. Counting it would make every "how many calls did
            # the endpoint see" assertion off by one.
            await _acknowledge_lifespan(receive, send)
            return
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        self.seen.append(json.loads(body or b"{}"))
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope["headers"]
        }
        self.authorizations.append(headers.get("authorization", ""))

        if self.failures:
            status, payload = self.failures.pop(0), {"error": "later"}
        else:
            status = 200
            payload = {
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": self.answer},
                    }
                ],
                "usage": self.usage
                if self.usage is not None
                else {"prompt_tokens": 11, "completion_tokens": 7},
            }
        encoded = json.dumps(payload).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(encoded)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": encoded})


async def _acknowledge_lifespan(receive: Any, send: Any) -> None:
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


@pytest.fixture
async def endpoint(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[FakeModel, str]]:
    monkeypatch.setenv(CREDENTIAL, "not-a-real-key")
    app = FakeModel()
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(1_000):
            if server.started:
                break
            await asyncio.sleep(0.01)
        address: Any = server.servers[0].sockets[0].getsockname()
        yield app, f"http://127.0.0.1:{int(address[1])}/v1"
    finally:
        server.should_exit = True
        await task


def loopback_is_reachable(
    addresses: Sequence[Address], approved: Sequence[Network]
) -> AddressVerdict:
    """See `test_safe_client`: a stand-in endpoint has nowhere else to live, and
    production must never be able to approve loopback."""
    if addresses and all(entry.is_loopback for entry in addresses):
        return AddressVerdict(allowed=True, address=addresses[0])
    return verdict(addresses, approved)


def spec(base_url: str, **overrides: Any) -> ModelEndpointSpec:
    fields: dict[str, Any] = {
        "name": "stand-in",
        "kind": "openai_compatible",
        "base_url": base_url,
        "model": "stand-in-large",
        "context_window": 8_192,
        "max_output_tokens": 512,
        "usage_quality": "provider",
        "credential_ref": CREDENTIAL,
    }
    fields.update(overrides)
    return ModelEndpointSpec.model_validate(fields)


def client() -> SafeOutboundClient:
    return SafeOutboundClient(
        approved=[ip_network("127.0.0.0/8")],
        policy=loopback_is_reachable,
        connect_timeout=2.0,
        read_timeout=5.0,
    )


def request(*turns: tuple[str, str]) -> ModelRequest:
    messages = tuple(
        CanonicalMessage(role="user" if role == "user" else "assistant", text=text)
        for role, text in (turns or (("user", "hello"),))
    )
    from tiny_hermes.agents.domain.models import DeterministicModelPolicy

    return ModelRequest(
        policy=DeterministicModelPolicy(),
        personality="You are a careful assistant.",
        messages=messages,
        round_index=1,
    )


class Recorded:
    """Backoff without waiting for it."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


async def test_a_round_completes_against_the_endpoint(
    endpoint: tuple[FakeModel, str]
) -> None:
    app, base_url = endpoint
    async with client() as outbound:
        response = await OpenAICompatibleProvider(spec(base_url), outbound).complete(request())

    assert response.stop_reason is StopReason.COMPLETED
    assert response.text == "the answer"
    assert response.input_tokens == 11
    assert response.usage_quality is UsageQuality.PROVIDER
    assert len(app.seen) == 1


async def test_the_platforms_rules_precede_the_agents_persona(
    endpoint: tuple[FakeModel, str]
) -> None:
    """An Agent cannot talk its way past a rule it is placed underneath."""
    app, base_url = endpoint
    async with client() as outbound:
        await OpenAICompatibleProvider(spec(base_url), outbound).complete(
            request(("user", "hello"), ("assistant", "hi"), ("user", "again"))
        )

    sent = app.seen[0]["messages"]
    assert sent[0] == {"role": "system", "content": SAFETY_PREAMBLE}
    assert sent[1]["content"] == "You are a careful assistant."
    assert [entry["role"] for entry in sent[2:]] == ["user", "assistant", "user"]
    assert app.seen[0]["max_tokens"] == 512
    # Not streaming, and not by omission of a default somewhere: nothing in
    # phase three consumes partial text, so none is asked for.
    assert "stream" not in app.seen[0]


async def test_the_credential_is_sent_and_appears_nowhere_else(
    endpoint: tuple[FakeModel, str]
) -> None:
    app, base_url = endpoint
    async with client() as outbound:
        response = await OpenAICompatibleProvider(spec(base_url), outbound).complete(request())

    assert app.authorizations == ["Bearer not-a-real-key"]
    assert "not-a-real-key" not in repr(response)
    assert "not-a-real-key" not in json.dumps(app.seen[0])


async def test_a_rate_limit_is_retried_and_then_succeeds(
    endpoint: tuple[FakeModel, str]
) -> None:
    app, base_url = endpoint
    app.failures = [429, 429]
    sleeper = Recorded()
    async with client() as outbound:
        response = await OpenAICompatibleProvider(
            spec(base_url), outbound, sleep=sleeper
        ).complete(request())

    assert response.stop_reason is StopReason.COMPLETED
    assert len(app.seen) == 3
    assert len(sleeper.delays) == 2
    # Jittered, so a fleet of Workers meeting the same rate limit does not
    # resynchronize onto the same second.
    assert all(0 <= delay <= 1.0 for delay in sleeper.delays)


async def test_a_persistent_rate_limit_gives_up_after_three_attempts(
    endpoint: tuple[FakeModel, str]
) -> None:
    app, base_url = endpoint
    app.failures = [429, 429, 429, 429]
    sleeper = Recorded()
    async with client() as outbound:
        response = await OpenAICompatibleProvider(
            spec(base_url), outbound, sleep=sleeper
        ).complete(request())

    assert response.stop_reason is StopReason.FAILED
    assert response.failure == "endpoint_status:429"
    assert len(app.seen) == 3


async def test_an_unauthorized_endpoint_is_not_retried(
    endpoint: tuple[FakeModel, str]
) -> None:
    """A rejection on the merits will be rejected again.

    Three attempts would turn one clear failure into a three-times-slower
    identical failure, and the count is asserted on the server's side because
    that is where a wasted call actually lands.
    """
    app, base_url = endpoint
    app.failures = [401, 401, 401]
    sleeper = Recorded()
    async with client() as outbound:
        response = await OpenAICompatibleProvider(
            spec(base_url), outbound, sleep=sleeper
        ).complete(request())

    assert response.failure == "endpoint_status:401"
    assert len(app.seen) == 1
    assert sleeper.delays == []


async def test_a_forbidden_address_is_refused_without_a_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The metadata service, reached by a literal so this needs no DNS.

    The credential is set because the provider resolves it before it dials: a
    call that cannot be authenticated is not worth attempting, and without this
    the test would pass for the wrong reason.
    """
    monkeypatch.setenv(CREDENTIAL, "not-a-real-key")
    sleeper = Recorded()
    async with SafeOutboundClient() as outbound:
        response = await OpenAICompatibleProvider(
            spec("https://169.254.169.254/v1"), outbound, sleep=sleeper
        ).complete(request())

    assert response.stop_reason is StopReason.FAILED
    assert response.failure == "outbound_refused:link_local"
    assert sleeper.delays == []


async def test_an_absent_credential_fails_the_round_rather_than_the_worker(
    endpoint: tuple[FakeModel, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, base_url = endpoint
    monkeypatch.delenv(CREDENTIAL, raising=False)
    async with client() as outbound:
        response = await OpenAICompatibleProvider(spec(base_url), outbound).complete(request())

    assert response.failure == "credential_missing"
    assert app.seen == []


async def test_an_endpoint_that_reports_no_usage_is_not_guessed_at(
    endpoint: tuple[FakeModel, str]
) -> None:
    app, base_url = endpoint
    app.usage = {}
    async with client() as outbound:
        response = await OpenAICompatibleProvider(spec(base_url), outbound).complete(request())

    assert response.stop_reason is StopReason.COMPLETED
    assert response.usage_quality is UsageQuality.UNAVAILABLE
    assert response.billable_tokens == 0


async def test_the_retry_budget_can_be_lowered_but_the_default_is_three(
    endpoint: tuple[FakeModel, str]
) -> None:
    app, base_url = endpoint
    app.failures = [503, 503, 503]
    async with client() as outbound:
        await OpenAICompatibleProvider(
            spec(base_url), outbound, policy=RetryPolicy(max_attempts=1), sleep=Recorded()
        ).complete(request())

    assert len(app.seen) == 1

"""The shipped example is a real spec, not a plausible-looking dict.

An example that fails validation is worse than no example: it is the first
thing a new administrator clicks at the end of the setup wizard, and it
fails on a deployment they have no reason to trust yet.

These are unit tests over the spec alone. Whether it survives *publishing* —
endpoint, network, ceilings — is an integration question, and
`tests/integration/agents/test_example_agent.py` asks it against the real
service.
"""

from uuid import uuid4

from tiny_hermes.agents.domain.examples import EXAMPLES, example_for
from tiny_hermes.agents.domain.models import AgentSpec
from tiny_hermes.tools.domain.registry import IMPLEMENTED_TOOLS


def test_every_shipped_example_is_a_valid_spec() -> None:
    for example in EXAMPLES:
        AgentSpec.model_validate(example.spec(uuid4()))


def test_every_shipped_example_binds_only_tools_this_platform_implements() -> None:
    # Publishing refuses an unknown name, so this would be caught — but at the
    # moment the administrator clicks the button, with a message about their
    # deployment rather than about our example.
    for example in EXAMPLES:
        spec = AgentSpec.model_validate(example.spec(uuid4()))
        assert set(spec.tools) <= set(IMPLEMENTED_TOOLS)


def test_the_example_needs_nothing_a_fresh_deployment_lacks() -> None:
    """§21 puts this at the end of a wizard, and nothing else has run yet.

    Each of these keys is something an administrator would first have to go
    and configure elsewhere — a registered HTTP tool, a reachable MCP server,
    a workspace network allow-list, a published skill, another Agent to
    delegate to. An example that needed one of them could not be created by
    the wizard that offers it.
    """
    for example in EXAMPLES:
        spec = AgentSpec.model_validate(example.spec(uuid4()))
        assert spec.http_tools == ()
        assert spec.mcp_tools == ()
        assert spec.skills == ()
        assert spec.delegation is None
        assert spec.network is None


def test_the_example_declares_something_the_platform_can_actually_check() -> None:
    """§12.2. A completion condition with nothing checkable is refused, and
    an example that merely *said* it was done would teach the reader the
    opposite of how this platform treats a model's claim."""
    for example in EXAMPLES:
        spec = AgentSpec.model_validate(example.spec(uuid4()))
        assert spec.completion is not None
        assert (
            spec.completion.expected_artifacts
            or spec.completion.verification_command is not None
        )


def test_slugs_are_unique_and_findable() -> None:
    slugs = [example.slug for example in EXAMPLES]
    assert len(slugs) == len(set(slugs))
    for slug in slugs:
        assert example_for(slug) is not None
    assert example_for("no-such-example") is None

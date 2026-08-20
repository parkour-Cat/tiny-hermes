"""§5's two-gate check: whether an end user may call one Agent alias.

Two independent layers, and neither is enough alone. `AgentSpec.end_user_access`
is the platform-side switch an Agent's own author flips (or, ninth in the
series, leaves unwritten and so unchanged in the content hash). The
enterprise's credential names which aliases *this* end user's employer has
handed them, in its own `agents` claim (`identity/domain/end_user_credential.py`).
Both gates must be open, and the two ways to fail them are answered
differently on purpose (module docstring of `end_user_credential.py`, design
§8): a credential naming an alias whose gate is shut is the Agent author's
problem to fix, so the refusal names the alias; a gate standing open for an
alias the credential never mentioned is the enterprise's own assignment
decision, and the end user simply does not see it.
"""

from tiny_hermes.agents.domain.models import AgentSpec, normalize_agent_spec

from .test_agent_models import valid_spec
from .test_model_policy import DETERMINISTIC_HASH

# -- the widening, ninth in the series ---------------------------------------


def test_an_agent_that_never_declared_end_user_access_hashes_as_it_always_did() -> None:
    spec = AgentSpec.model_validate(valid_spec())

    document, content_hash = normalize_agent_spec(spec)

    assert content_hash == DETERMINISTIC_HASH
    assert "end_user_access" not in document
    assert spec.end_user_access is None


def test_a_declared_end_user_access_survives_normalization_and_changes_the_hash() -> None:
    spec = AgentSpec.model_validate({**valid_spec(), "end_user_access": {"enabled": True}})

    document, content_hash = normalize_agent_spec(spec)

    assert document["end_user_access"] == {"enabled": True}
    assert content_hash != DETERMINISTIC_HASH

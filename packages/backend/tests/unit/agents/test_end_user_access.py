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

from uuid import UUID, uuid4

import pytest
from tiny_hermes.agents.application.service import (
    AgentCatalog,
    EndUserAccessGateClosed,
    EndUserAccessNotAssigned,
)
from tiny_hermes.agents.domain.models import AgentSpec, normalize_agent_spec
from tiny_hermes.agents.infrastructure.memory_store import MemoryAgentStore
from tiny_hermes.tenancy.domain.models import Actor, Role

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


# -- publishing one Agent, ready to be resolved ------------------------------


async def _published(
    *, end_user_access_enabled: bool | None
) -> tuple[AgentCatalog, MemoryAgentStore, UUID, str]:
    """A published Agent named "helper", with `end_user_access` set as asked.

    `None` means the field is never written at all — the default an Agent
    published before this task existed always had.
    """
    workspace_id = uuid4()
    actor = Actor(uuid4(), False)
    store = MemoryAgentStore()
    store.roles[(workspace_id, actor.id)] = Role.WORKSPACE_ADMIN
    catalog = AgentCatalog(store)

    agent = await catalog.create_agent(workspace_id, actor, "Helper", "helper", "req-1")
    spec_values = dict(valid_spec())
    if end_user_access_enabled is not None:
        spec_values["end_user_access"] = {"enabled": end_user_access_enabled}
    draft = await catalog.replace_draft(workspace_id, actor, agent.id, 1, spec_values, "req-2")
    await catalog.publish(workspace_id, actor, agent.id, draft.revision, "req-3")
    return catalog, store, workspace_id, "helper"


# -- the four combinations ----------------------------------------------------


async def test_gate_open_and_listed_is_permitted() -> None:
    catalog, _store, workspace_id, alias = await _published(end_user_access_enabled=True)

    found_agent, found_version = await catalog.resolve_end_user_agent(
        workspace_id, alias, (alias,)
    )

    assert found_agent.alias == alias
    assert found_version.version_number == 1


async def test_gate_open_but_not_listed_is_refused_as_not_assigned() -> None:
    catalog, _store, workspace_id, alias = await _published(end_user_access_enabled=True)

    with pytest.raises(EndUserAccessNotAssigned):
        await catalog.resolve_end_user_agent(workspace_id, alias, ())


async def test_gate_closed_but_listed_is_refused_as_gate_closed_and_names_the_alias() -> None:
    catalog, _store, workspace_id, alias = await _published(end_user_access_enabled=False)

    with pytest.raises(EndUserAccessGateClosed) as excinfo:
        await catalog.resolve_end_user_agent(workspace_id, alias, (alias,))

    assert excinfo.value.alias == alias


async def test_gate_closed_and_not_listed_is_refused_as_not_assigned() -> None:
    """Both problems at once resolves to the enterprise's, not the author's:
    listing it would not have been enough on its own either way, so naming
    the author's switch would send the wrong person to fix it."""
    catalog, _store, workspace_id, alias = await _published(end_user_access_enabled=False)

    with pytest.raises(EndUserAccessNotAssigned):
        await catalog.resolve_end_user_agent(workspace_id, alias, ())


async def test_an_agent_that_never_declared_end_user_access_is_refused_as_gate_closed() -> None:
    """The default an Agent published before this task existed always had:
    absent means closed, not "ask the enterprise"."""
    catalog, _store, workspace_id, alias = await _published(end_user_access_enabled=None)

    with pytest.raises(EndUserAccessGateClosed):
        await catalog.resolve_end_user_agent(workspace_id, alias, (alias,))


# -- the workspace-scoping requirement ---------------------------------------


async def test_an_alias_from_another_workspace_is_never_resolved() -> None:
    """§4's last bullet: the platform must not trust a credential's own claim
    that an alias belongs to its workspace. An enterprise's `aud` fixes which
    workspace a credential is verified for; resolving that alias against any
    other workspace's catalog would let a signed credential reach an Agent
    its own workspace never published.
    """
    catalog, _store, _workspace_id, alias = await _published(end_user_access_enabled=True)
    other_workspace_id = uuid4()

    with pytest.raises(EndUserAccessGateClosed):
        await catalog.resolve_end_user_agent(other_workspace_id, alias, (alias,))


# -- resolve_end_user_agent_by_id: the same two gates, keyed by the id a ----
# -- Session already carries rather than an alias a client typed ------------
#
# Task-9 review finding A: `create_run` had no way to re-evaluate either gate
# on submission, only `create_session` did, so closing `end_user_access` or
# letting an Agent's published version go away had no effect on a Session
# already holding a cookie for up to the 8-hour session TTL. The fix reuses
# this exact two-gate evaluation on every submission — keyed by `agent_id`
# because a Run submission has no alias to resolve, only the Session's own
# `agent_id` — through the same `_gate_check` `resolve_end_user_agent` uses,
# so the two can never drift apart.


async def test_by_id_gate_open_and_listed_is_permitted() -> None:
    catalog, store, workspace_id, alias = await _published(end_user_access_enabled=True)
    agent = await store.get_agent(workspace_id, next(iter(store.agents)))
    assert agent is not None

    found_agent, found_version = await catalog.resolve_end_user_agent_by_id(
        workspace_id, agent.id, (alias,)
    )

    assert found_agent.alias == alias
    assert found_version.version_number == 1


async def test_by_id_gate_closed_is_refused_and_names_the_alias() -> None:
    catalog, store, workspace_id, alias = await _published(end_user_access_enabled=False)
    agent = await store.get_agent(workspace_id, next(iter(store.agents)))
    assert agent is not None

    with pytest.raises(EndUserAccessGateClosed) as excinfo:
        await catalog.resolve_end_user_agent_by_id(workspace_id, agent.id, (alias,))

    assert excinfo.value.alias == alias


async def test_by_id_no_longer_listed_is_refused_as_not_assigned() -> None:
    """The session-snapshot half of the finding: the credential's own
    `agents` claim no longer names this Agent's alias — the same as if the
    enterprise had never delegated it, evaluated again rather than trusted
    from the moment the Session was created."""
    catalog, store, workspace_id, _alias = await _published(end_user_access_enabled=True)
    agent = await store.get_agent(workspace_id, next(iter(store.agents)))
    assert agent is not None

    with pytest.raises(EndUserAccessNotAssigned):
        await catalog.resolve_end_user_agent_by_id(workspace_id, agent.id, ())


async def test_by_id_matches_by_alias_resolution_for_every_combination() -> None:
    """The two entry points must never disagree: whatever
    `resolve_end_user_agent` decides for an alias, `resolve_end_user_agent_by_id`
    decides the same way for that alias's own Agent id — that agreement is
    the whole point of routing both through one shared gate check."""
    catalog, store, workspace_id, alias = await _published(end_user_access_enabled=True)
    agent = await store.get_agent(workspace_id, next(iter(store.agents)))
    assert agent is not None

    by_alias = await catalog.resolve_end_user_agent(workspace_id, alias, (alias,))
    by_id = await catalog.resolve_end_user_agent_by_id(workspace_id, agent.id, (alias,))

    assert by_alias == by_id

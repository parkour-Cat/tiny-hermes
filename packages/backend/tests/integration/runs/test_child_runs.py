"""One delegation, two children, and the two things that must not be shared.

§13's creation path end to end. This suite exists because both of its claims
would fail in a seam rather than in a function, and neither would look like a
failure when it happened.

**A child must not share the parent's workspace.** §13's eighth clause says
files move as authorizations rather than through a common directory. That is
not enforced by a check anywhere — it is a consequence of the child holding a
Session of its own, because a SessionWorkspace is keyed by Session. So the test
asserts the shape: the three Runs have three Sessions, and there is no
revision a parent and a child both point at. A regression here would be a
child quietly writing where its parent reads, which no error message would ever
mention.

**A tree must be one budget.** §12.4's `budget_root_run_id` already carries
this and M2A already shares it down a retry chain. The failure mode is a child
getting a budget row of its own, and the symptom is not an exception — it is a
safety valve that a Run can reset by delegating, which is only visible as three
counters that should have been one. So this counts rows and adds numbers up.

The children are driven by two Workers running at once rather than by one
Worker twice. A single Worker would prove that both Runs execute; two prove
that neither is waiting on the other, which is the property the parent's
Session FIFO would silently take away if a child were ever put in it.
"""

import asyncio
from collections.abc import Callable
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.scheduler import (
    SchedulerRuntime,
    SchedulerSettings,
)
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.infrastructure.deterministic_model import (
    DeterministicModelProvider,
)
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.infrastructure.sql_children import SqlChildRuns
from tiny_hermes.runs.ports.model import ModelRequest, ModelResponse

from ..conftest import VALID_SPEC


class Recorder:
    """The stand-in provider, with every request it answered kept.

    "The parent's memory is not in the child's request" is a statement about
    the bytes sent to the model, and this is the only place that can see them.
    """

    def __init__(self) -> None:
        self.inner = DeterministicModelProvider(delay_ms=0)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return await self.inner.complete(request)


def _worker(
    engine: AsyncEngine,
    workspace_id: str,
    name: str,
    model: Recorder | None = None,
) -> WorkerRuntime:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return WorkerRuntime(
        session_factory=sessions,
        model=model or DeterministicModelProvider(delay_ms=0),
        notifier=NullWakeUpNotifier(),
        children=SqlChildRuns(sessions),
        settings=WorkerSettings(
            worker_id=name,
            lease_seconds=30,
            max_slice_seconds=30,
            idle_poll_seconds=1,
            workspace_id=UUID(workspace_id),
        ),
    )


def _publish(
    client: TestClient,
    scope: dict[str, str],
    alias: str,
    spec: dict[str, Any],
) -> str:
    agent_id = str(
        client.post(
            "/api/v1/agents", headers=scope, json={"name": alias.title(), "alias": alias}
        ).json()["id"]
    )
    draft = client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": spec},
    )
    assert draft.status_code == 200, draft.text
    published = client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers=scope,
        json={"expected_revision": draft.json()["revision"]},
    )
    assert published.status_code == 201, published.text
    return agent_id


@pytest.fixture
def coordinator(client: TestClient, scope: dict[str, str]) -> str:
    """A parent bound to two children, and the two children themselves.

    The children bind nothing: §13's sixth clause makes their scope an
    intersection, and a child that holds nothing is the honest default for a
    delegation that asked for nothing. What this suite is about is the Run each
    one gets, not what it is allowed to do inside it.
    """
    for alias in ("reader", "checker"):
        _publish(
            client,
            scope,
            alias,
            {**VALID_SPEC, "model_policy": {"provider": "deterministic", "scenario": "complete"}},
        )
    return _publish(
        client,
        scope,
        "coordinator",
        {
            **VALID_SPEC,
            "model_policy": {"provider": "deterministic", "scenario": "delegate_once"},
            "tools": ["agent.delegate"],
            "delegation": {
                "max_parallel": 2,
                "children": [{"alias": "reader"}, {"alias": "checker"}],
            },
        },
    )


async def _rows(engine: AsyncEngine, sql: str, **params: object) -> list[Any]:
    async with engine.connect() as connection:
        result = await connection.execute(text(sql), params)
        return list(result.all())


def _scheduler(engine: AsyncEngine) -> SchedulerRuntime:
    return SchedulerRuntime(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        notifier=NullWakeUpNotifier(),
        settings=SchedulerSettings(max_recovery_attempts=3, event_retention_hours=24),
    )


async def _drain(
    engine: AsyncEngine, workspace_id: str, model: Recorder | None = None
) -> None:
    """Run everything there is to run: two Workers competing, and a Scheduler.

    Two Workers rather than one because "the children are not behind the
    parent" is the claim: they hold Sessions of their own, so two Workers can
    hold two of them at the same moment.

    The Scheduler is here because a parent waiting on children **does not wake
    itself** — nothing in a Worker settles a wait, by design. A drain without
    it would leave every parent in `waiting_external` forever, which is the
    honest shape of this deployment rather than a quirk of the harness.
    """
    workers = (
        _worker(engine, workspace_id, "worker-a", model),
        _worker(engine, workspace_id, "worker-b", model),
    )
    scheduler = _scheduler(engine)
    for _ in range(20):
        advanced = await asyncio.gather(*(worker.run_once() for worker in workers))
        before = await _rows(
            engine,
            "SELECT count(*) AS n FROM runs WHERE status = 'waiting_external'",
        )
        await scheduler.run_once()
        after = await _rows(
            engine,
            "SELECT count(*) AS n FROM runs WHERE status = 'waiting_external'",
        )
        if not any(advanced) and before[0].n == after[0].n == 0:
            return
    raise AssertionError("the Runs never settled")


async def test_one_delegation_creates_two_children_that_each_run(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    coordinator: str,
) -> None:
    """Both children reach a terminal state, and both were delegated.

    The parent finishes too. It does not wait for them yet — that is the next
    step of this phase — so "the parent completed" here is a statement about
    this step's scope rather than about §13 being satisfied.
    """
    workspace_id = scope["X-Workspace-Id"]
    parent_run = str(
        client.post(
            "/api/v1/runs",
            headers={**scope, "Idempotency-Key": "delegate-two"},
            json={"session_id": session_for(coordinator), "input": "reader,checker"},
        ).json()["id"]
    )

    await _drain(engine, workspace_id)

    children = await _rows(
        engine,
        "SELECT id, depth, status, session_id, budget_root_run_id "
        "FROM runs WHERE parent_run_id = :p ORDER BY created_at, id",
        p=UUID(parent_run),
    )
    assert len(children) == 2
    assert [row.depth for row in children] == [1, 1]
    assert [row.status for row in children] == ["completed", "completed"]


async def test_a_child_holds_its_own_session_and_therefore_its_own_workspace(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    coordinator: str,
) -> None:
    """§13's eighth clause, asserted as an absence rather than as a refusal.

    The test does not check that a shared directory is denied. It checks that
    there is no such directory to deny: three Runs, three Sessions, and no
    workspace revision two of them point at. A SessionWorkspace is keyed by
    Session, so this is the property that makes the clause true rather than a
    second place it is enforced.
    """
    workspace_id = scope["X-Workspace-Id"]
    parent_session = session_for(coordinator)
    parent_run = str(
        client.post(
            "/api/v1/runs",
            headers={**scope, "Idempotency-Key": "delegate-sessions"},
            json={"session_id": parent_session, "input": "reader,checker"},
        ).json()["id"]
    )

    await _drain(engine, workspace_id)

    children = await _rows(
        engine,
        "SELECT id, session_id FROM runs WHERE parent_run_id = :p",
        p=UUID(parent_run),
    )
    sessions = {row.session_id for row in children}
    assert len(sessions) == 2
    assert UUID(parent_session) not in sessions

    shared = await _rows(
        engine,
        "SELECT session_id, count(*) AS n FROM workspace_revisions "
        "WHERE session_id = ANY(:ids) GROUP BY session_id",
        ids=[UUID(parent_session), *sessions],
    )
    # Whatever revisions exist belong to one Session each. A revision two of
    # these Sessions shared would be the shared writable directory §13 forbids,
    # and it could only appear by a child being given the parent's Session.
    assert all(row.session_id in {UUID(parent_session), *sessions} for row in shared)
    assert len({row.session_id for row in shared}) == len(shared)


async def test_the_whole_tree_spends_one_budget_and_creating_a_child_resets_nothing(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    coordinator: str,
) -> None:
    """Red line four, counted rather than asserted about.

    Three Runs and **one** budget row. `consumed_model_calls` on it is the sum
    of every round all three did, which is the number that would silently
    become three separate small numbers if a child were ever given a budget of
    its own — a Run that can reset a safety valve by delegating.
    """
    workspace_id = scope["X-Workspace-Id"]
    parent_run = str(
        client.post(
            "/api/v1/runs",
            headers={**scope, "Idempotency-Key": "delegate-budget"},
            json={"session_id": session_for(coordinator), "input": "reader,checker"},
        ).json()["id"]
    )

    await _drain(engine, workspace_id)

    tree = await _rows(
        engine,
        "SELECT id, budget_root_run_id FROM runs WHERE id = :p OR parent_run_id = :p",
        p=UUID(parent_run),
    )
    assert len(tree) == 3
    # Every Run in the tree points at the Run somebody actually asked for.
    assert {row.budget_root_run_id for row in tree} == {UUID(parent_run)}

    scopes = await _rows(
        engine,
        "SELECT root_run_id, consumed_model_calls FROM run_budget_scopes "
        "WHERE root_run_id = ANY(:ids)",
        ids=[row.id for row in tree],
    )
    assert len(scopes) == 1, "a child must not get a budget row of its own"

    # Every model call all three Runs made, on one counter. The parent needed
    # two rounds — one to delegate and one to read the answer — and each child
    # needed one, so the tree spent four and the counter says four.
    assert scopes[0].consumed_model_calls == 4


async def test_a_child_cannot_delegate_again(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
) -> None:
    """§13's third clause, on the creation path, called directly.

    Directly rather than through a Run, because the point is that the refusal
    does not depend on the child's own configuration. This child is published
    with the delegation policy and the tool its parent has — a spec somebody
    bound wrongly, which is exactly the case the clause is about — and it is
    still refused, on its own `depth` and nothing else.
    """
    workspace_id = scope["X-Workspace-Id"]
    for alias in ("reader", "checker"):
        _publish(
            client,
            scope,
            alias,
            {**VALID_SPEC, "model_policy": {"provider": "deterministic", "scenario": "complete"}},
        )
    # A child bound exactly as its parent is: it may delegate on paper.
    delegating_child = {
        **VALID_SPEC,
        "model_policy": {"provider": "deterministic", "scenario": "complete"},
        "tools": ["agent.delegate"],
        "delegation": {"max_parallel": 2, "children": [{"alias": "reader"}]},
    }
    _publish(client, scope, "deputy", delegating_child)
    parent_agent = _publish(
        client,
        scope,
        "coordinator",
        {
            **VALID_SPEC,
            "model_policy": {"provider": "deterministic", "scenario": "delegate_once"},
            "tools": ["agent.delegate"],
            "delegation": {"max_parallel": 2, "children": [{"alias": "deputy"}]},
        },
    )
    parent_run = str(
        client.post(
            "/api/v1/runs",
            headers={**scope, "Idempotency-Key": "delegate-depth"},
            json={"session_id": session_for(parent_agent), "input": "deputy"},
        ).json()["id"]
    )
    await _drain(engine, workspace_id)

    child = (
        await _rows(
            engine, "SELECT id FROM runs WHERE parent_run_id = :p", p=UUID(parent_run)
        )
    )[0]

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    from tiny_hermes.runs.ports.children import DelegationRequest

    result = await SqlChildRuns(sessions).delegate(
        parent_run_id=child.id,
        requests=(DelegationRequest(alias="reader", instruction="Read it."),),
    )

    assert result.refused
    assert "cannot delegate further" in result.refusal
    grandchildren = await _rows(
        engine, "SELECT id FROM runs WHERE parent_run_id = :p", p=child.id
    )
    assert grandchildren == []


async def test_a_child_inherits_the_calling_subject_and_the_person_who_may_confirm(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    coordinator: str,
) -> None:
    """§13's fourth clause: identity, audit and data ownership carry down.

    Both halves matter and they are different facts. The `CallerIdentity` on
    the child's Session is what every later question about ownership reads —
    whose data this is, whose memory scope applies, whose name is in the audit
    trail. `end_user_id` is narrower: §16.3 says only the EndUser who started
    the work may answer a `user_confirmation`, and a child that did not carry
    it would be a Run that can be stopped by an approval nobody is allowed to
    give.
    """
    workspace_id = scope["X-Workspace-Id"]
    parent_session = session_for(coordinator)
    parent_run = str(
        client.post(
            "/api/v1/runs",
            headers={**scope, "Idempotency-Key": "delegate-subject"},
            json={"session_id": parent_session, "input": "reader,checker"},
        ).json()["id"]
    )

    await _drain(engine, workspace_id)

    parent = (
        await _rows(
            engine,
            "SELECT r.end_user_id, s.caller_type, s.caller_id FROM runs r "
            "JOIN sessions s ON s.id = r.session_id WHERE r.id = :p",
            p=UUID(parent_run),
        )
    )[0]
    children = await _rows(
        engine,
        "SELECT r.end_user_id, s.caller_type, s.caller_id FROM runs r "
        "JOIN sessions s ON s.id = r.session_id WHERE r.parent_run_id = :p",
        p=UUID(parent_run),
    )

    assert len(children) == 2
    # Not merely "not null": the same subject, which is the claim.
    assert {row.caller_type for row in children} == {parent.caller_type}
    assert {row.caller_id for row in children} == {parent.caller_id}
    assert {row.end_user_id for row in children} == {parent.end_user_id}
    assert parent.end_user_id is not None


async def test_a_child_does_not_inherit_the_parents_private_memories(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    coordinator: str,
) -> None:
    """§27.2.3, and the reason it is already true rather than newly enforced.

    A private memory is scoped by workspace **and agent** and subject. A child
    runs as its own Agent, so the parent's memories are outside its scope by
    construction — there is no filter here that could be forgotten, which is
    exactly why this is asserted against the bytes sent to the model rather
    than against a store. The parent's own request is checked too: a test where
    the line reached nobody would pass for the wrong reason.
    """
    workspace_id = scope["X-Workspace-Id"]
    # Worded to share keywords with the Run input below, because retrieval
    # orders by keyword relevance (§14.3 excludes vector memory) and a memory
    # nothing matched would be absent from the parent's request too — which
    # would make this pass for the wrong reason.
    remembered = "The reader and the checker both report to the duty manager."
    parent_session = session_for(coordinator)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO memories (id, workspace_id, agent_id, kind, "
                "subject_type, subject_id, body, status, origin, context, "
                "created_at, updated_at) "
                "SELECT :id, :workspace, av.agent_id, 'private', 'user', "
                "s.caller_id, :body, 'active', 'operator', '{}', now(), now() "
                "FROM sessions s JOIN agents a ON a.id = s.agent_id "
                "JOIN agent_versions av ON av.id = a.current_version_id "
                "WHERE s.id = :session"
            ),
            {
                "id": uuid4(),
                "workspace": UUID(workspace_id),
                "body": remembered,
                "session": UUID(parent_session),
            },
        )

    client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": "delegate-memory"},
        json={"session_id": parent_session, "input": "reader,checker"},
    )
    model = Recorder()
    await _drain(engine, workspace_id, model)

    # The children are the rounds whose conversation is the one sentence the
    # platform handed them — §13's seventh clause means the parent's transcript
    # is not in them, which is also what makes them identifiable here.
    children = [
        request
        for request in model.requests
        if any("Do the " in message.text for message in request.messages)
    ]
    parents = [request for request in model.requests if request not in children]

    assert len(children) == 2, "both children should have run"
    assert parents, "the parent should have run"
    # The parent was told, so the memory is reaching a Run at all. Without this
    # half, a retrieval that returned nothing to anybody would pass.
    assert any(remembered in line for request in parents for line in request.memories)
    # And no child was, which is §27.2.3.
    assert not any(
        remembered in line for request in children for line in request.memories
    )
    # Nor did it reach them by some other route than the memory field.
    assert not any(
        remembered in message.text
        for request in children
        for message in request.messages
    )


async def test_a_child_cannot_share_a_live_sandbox_with_its_parent(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    coordinator: str,
) -> None:
    """Asserted as a shape, the same way the workspace claim is.

    A sandbox reservation is keyed by `run_id` under a unique index over live
    claims, so a child holding its parent's sandbox is not a thing this schema
    can express. The test states that rather than driving a container: what
    would break the guarantee is somebody giving a child its parent's Run id,
    and that is what this would catch.
    """
    workspace_id = scope["X-Workspace-Id"]
    parent_run = str(
        client.post(
            "/api/v1/runs",
            headers={**scope, "Idempotency-Key": "delegate-sandbox"},
            json={"session_id": session_for(coordinator), "input": "reader,checker"},
        ).json()["id"]
    )

    await _drain(engine, workspace_id)

    tree = await _rows(
        engine,
        "SELECT id FROM runs WHERE id = :p OR parent_run_id = :p",
        p=UUID(parent_run),
    )
    assert len({row.id for row in tree}) == 3

    reservations = await _rows(
        engine,
        "SELECT run_id, sandbox_instance_id FROM sandbox_reservations "
        "WHERE run_id = ANY(:ids)",
        ids=[row.id for row in tree],
    )
    # However many there are, no instance is claimed by two of these Runs.
    instances = [row.sandbox_instance_id for row in reservations]
    assert len(instances) == len(set(instances))


def _files(scope: object) -> list[str]:
    """The `files` face of a stored delegation scope, as a list of ids."""
    return cast(list[str], cast(dict[str, Any], scope or {}).get("files", []))


async def _artifact(
    engine: AsyncEngine,
    workspace_id: str,
    run_id: UUID,
    artifact_id: UUID | None = None,
) -> UUID:
    """One Artifact belonging to a Run, written straight to the table.

    Inserted rather than produced by a command: what these tests are about is
    who may *read* it, and driving a container to overflow its output would put
    a sandbox between the assertion and the thing being asserted.
    """
    artifact_id = artifact_id or uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO artifacts (id, workspace_id, session_id, run_id, "
                "object_key, filename, media_type, size_bytes, sha256, truncated, "
                "expires_at, created_at) "
                "SELECT :id, :workspace, r.session_id, r.id, :key, 'notes.txt', "
                "'text/plain', 6, :digest, false, now() + interval '1 day', now() "
                "FROM runs r WHERE r.id = :run"
            ),
            {
                "id": artifact_id,
                "workspace": UUID(workspace_id),
                "key": f"artifacts/{artifact_id}",
                "digest": "a" * 64,
                "run": run_id,
            },
        )
    return artifact_id


async def test_a_file_reaches_a_child_as_a_grant_and_never_as_a_directory(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    coordinator: str,
) -> None:
    """§13's eighth clause, on the rows that make it true.

    The parent hands one file to one of its two children. What the child gets
    is a grant row and an id in its own recorded scope — not a path, not a
    mount, and nothing the other child can see. The sibling is the control: it
    was delegated in the same call and has no grant at all, so the test is
    about the authorization rather than about the delegation.
    """
    workspace_id = scope["X-Workspace-Id"]
    # The id is chosen before the Run so the input can name it. Rewriting the
    # first message afterwards would work too, and would mean the Run's own
    # transcript no longer said what it was asked to do.
    passed = uuid4()
    parent_run = str(
        client.post(
            "/api/v1/runs",
            headers={**scope, "Idempotency-Key": "delegate-files"},
            json={
                "session_id": session_for(coordinator),
                "input": f"reader#{passed},checker",
            },
        ).json()["id"]
    )
    # Owned by the parent Run, which is one of the two ways a Run may read a
    # file at all — the other being a grant like the one this creates.
    await _artifact(engine, workspace_id, UUID(parent_run), passed)

    await _drain(engine, workspace_id)

    children = await _rows(
        engine,
        "SELECT r.id, a.alias, r.delegation_scope FROM runs r "
        "JOIN agent_versions av ON av.id = r.agent_version_id "
        "JOIN agents a ON a.id = av.agent_id WHERE r.parent_run_id = :p",
        p=UUID(parent_run),
    )
    by_alias = {row.alias: row for row in children}
    assert set(by_alias) == {"reader", "checker"}

    # The scope records what this child was actually given, as ids.
    assert str(passed) in _files(by_alias["reader"].delegation_scope)
    assert _files(by_alias["checker"].delegation_scope) == []

    grants = await _rows(
        engine,
        "SELECT run_id, reason FROM artifact_grants WHERE artifact_id = :a",
        a=passed,
    )
    granted_to = {row.run_id for row in grants}
    assert by_alias["reader"].id in granted_to
    assert by_alias["checker"].id not in granted_to, (
        "a sibling delegated in the same call must not be able to read it"
    )
    assert {row.reason for row in grants} == {"delegated_down"}


async def test_a_parent_cannot_pass_on_a_file_it_cannot_read_itself(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    coordinator: str,
) -> None:
    """The same clause from the other end, and refused before any child exists.

    A parent naming somebody else's file is refused outright rather than having
    that one file dropped: a delegation that half happened would be a child
    working on a task whose inputs it was never given, and it would have no way
    to tell.
    """
    workspace_id = scope["X-Workspace-Id"]
    other_session = session_for(coordinator)
    other_run = str(
        client.post(
            "/api/v1/runs",
            headers={**scope, "Idempotency-Key": "delegate-files-other"},
            json={"session_id": other_session, "input": "reader"},
        ).json()["id"]
    )
    # Belongs to a different Run and is granted to nobody.
    stranger = await _artifact(engine, workspace_id, UUID(other_run))

    parent_session = session_for(coordinator)
    parent_run = str(
        client.post(
            "/api/v1/runs",
            headers={**scope, "Idempotency-Key": "delegate-files-refused"},
            json={"session_id": parent_session, "input": "reader"},
        ).json()["id"]
    )

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    from tiny_hermes.runs.ports.children import DelegationRequest

    result = await SqlChildRuns(sessions).delegate(
        parent_run_id=UUID(parent_run),
        requests=(
            DelegationRequest(
                alias="reader", instruction="Read it.", artifacts=(str(stranger),)
            ),
        ),
    )

    assert result.refused
    assert "cannot read" in result.refusal
    made = await _rows(
        engine, "SELECT id FROM runs WHERE parent_run_id = :p", p=UUID(parent_run)
    )
    assert made == [], "nothing may be created when the files were refused"


async def test_a_childs_own_files_are_granted_up_when_its_result_is_delivered(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    coordinator: str,
) -> None:
    """The upward half of §13's eighth clause.

    A child produces a file. When its result reaches the parent, the parent is
    granted it — so the ids the report names are things it can actually open,
    rather than a list of files it may not have.
    """
    workspace_id = scope["X-Workspace-Id"]
    parent_run = str(
        client.post(
            "/api/v1/runs",
            headers={**scope, "Idempotency-Key": "delegate-files-up"},
            json={"session_id": session_for(coordinator), "input": "reader,checker"},
        ).json()["id"]
    )

    # Let the parent delegate and the children run, but do not settle the wait
    # yet: the file has to exist before the child's result is written.
    workers = (
        _worker(engine, workspace_id, "worker-a"),
        _worker(engine, workspace_id, "worker-b"),
    )
    await asyncio.gather(*(worker.run_once() for worker in workers))
    child = (
        await _rows(
            engine,
            "SELECT id FROM runs WHERE parent_run_id = :p ORDER BY created_at LIMIT 1",
            p=UUID(parent_run),
        )
    )[0]
    produced = await _artifact(engine, workspace_id, child.id)

    await _drain(engine, workspace_id)

    grants = await _rows(
        engine,
        "SELECT run_id, reason FROM artifact_grants WHERE artifact_id = :a",
        a=produced,
    )
    assert [(row.run_id, row.reason) for row in grants] == [
        (UUID(parent_run), "delivered_up")
    ]

    result = (
        await _rows(
            engine, "SELECT delegation_result FROM runs WHERE id = :c", c=child.id
        )
    )[0]
    reported = cast(dict[str, Any], result.delegation_result or {})
    assert str(produced) in cast(list[str], reported.get("artifacts", []))


async def test_the_api_says_a_Run_is_part_of_a_tree(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    coordinator: str,
) -> None:
    """Through the HTTP boundary, not the table.

    Its own test because the boundary is where this went wrong once: the
    response model lists its fields, so a column and a snapshot can both be
    correct while the console is served a document with the tree missing. The
    suite read the database and passed; only the browser walk noticed. This is
    that walk's claim, moved to where it costs seconds.
    """
    workspace_id = scope["X-Workspace-Id"]
    parent_run = str(
        client.post(
            "/api/v1/runs",
            headers={**scope, "Idempotency-Key": "delegate-api"},
            json={"session_id": session_for(coordinator), "input": "reader,checker"},
        ).json()["id"]
    )

    await _drain(engine, workspace_id)

    document = client.get(f"/api/v1/runs/{parent_run}", headers=scope).json()
    assert document["parent_run_id"] is None
    assert document["depth"] == 0
    children = document["children"]
    assert len(children) == 2
    assert {child["status"] for child in children} == {"completed"}

    # And a child, from the same endpoint, names its parent back.
    child = client.get(f"/api/v1/runs/{children[0]['id']}", headers=scope).json()
    assert child["parent_run_id"] == parent_run
    assert child["depth"] == 1
    assert child["children"] == []

    # The delivered report is attributed to the platform through the API too,
    # and for the reason the field exists at all: a caller that cannot tell the
    # platform's words from the person's is reading a transcript that
    # misattributes them. Dropped at this boundary once already — see the
    # docstring above — so it is asserted here rather than assumed.
    messages = client.get(
        f"/api/v1/sessions/{document['session_id']}/messages", headers=scope
    ).json()
    delivered = [item for item in messages if item.get("author") == "platform"]
    assert len(delivered) == 1
    said = "".join(part.get("text", "") for part in delivered[0]["parts"])
    assert all(child["id"] in said for child in children)

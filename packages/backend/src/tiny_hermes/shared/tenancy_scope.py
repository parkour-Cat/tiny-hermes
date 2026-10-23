"""Which workspace this request acts in, for row-level security (§9.3, v2.20).

Set once a request's workspace is settled; every database transaction that
begins after that carries it as `app.workspace_id`, and the policies on every
table with a `workspace_id` then show and accept that workspace's rows only.
A query that forgot its own filter is answered for one workspace anyway.

Unset, nothing is restricted. Workers, the Scheduler, the controller and the
egress proxy work across workspaces by design, and so do login and platform
administration before a workspace is chosen. For those, isolation is still the
code's alone.

A context variable, so it ends with the request: each request is handled in a
task of its own, which starts from a copy of the context and never writes back.
"""

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID

from sqlalchemy import Connection, event, text
from sqlalchemy.orm import Session, SessionTransaction

_ACTING_IN: ContextVar[UUID | None] = ContextVar("tiny_hermes_acting_in", default=None)


def enter_workspace(workspace_id: UUID) -> None:
    """For the rest of this request. Called where a caller's workspace is settled."""
    _ACTING_IN.set(workspace_id)


@contextmanager
def acting_in(workspace_id: UUID) -> Generator[None]:
    """For a block, outside a request."""
    token = _ACTING_IN.set(workspace_id)
    try:
        yield
    finally:
        _ACTING_IN.reset(token)


def _scope_transaction(
    session: Session, transaction: SessionTransaction, connection: Connection
) -> None:
    """Transaction-local, so the next transaction on this pooled connection
    starts unscoped and takes whatever its own request says. A transaction that
    began before the workspace was settled stays unscoped to its end."""
    del session, transaction
    workspace = _ACTING_IN.get()
    if workspace is not None:
        connection.execute(
            text("SELECT set_config('app.workspace_id', :workspace, true)"),
            {"workspace": str(workspace)},
        )


event.listen(Session, "after_begin", _scope_transaction)

import asyncio
import contextlib
import logging
import signal
import socket
import time
import uuid
from collections.abc import Callable
from typing import Any, Protocol
from uuid import UUID

import uvicorn
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiny_hermes.audit.infrastructure.tables import AuditEventRow
from tiny_hermes.channels.application.feishu_service import FeishuChannelService
from tiny_hermes.channels.application.ingestion import ChannelIngestion
from tiny_hermes.channels.application.outbound import ChannelReplyDispatcher
from tiny_hermes.channels.application.webhook_service import FeishuWebhookService
from tiny_hermes.channels.infrastructure.feishu_long_connection import (
    DeliverFrame,
    FeishuLongConnection,
    LongConnectionBinding,
)
from tiny_hermes.channels.infrastructure.feishu_sender import FeishuSender
from tiny_hermes.channels.infrastructure.run_images import ChannelImageSource
from tiny_hermes.channels.infrastructure.sql_channel_store import SqlChannelStore
from tiny_hermes.channels.infrastructure.tables import ChannelBindingRow
from tiny_hermes.identity.infrastructure.sql_end_user_store import SqlEndUserStore
from tiny_hermes.memory.infrastructure.run_searches import SqlRunSessionSearches
from tiny_hermes.memory.infrastructure.sql_candidates import SqlMemoryCandidates
from tiny_hermes.model_catalog.infrastructure.credentials import (
    CredentialMissing,
    CredentialResolver,
)
from tiny_hermes.outbound.client import EgressRoute, SafeOutboundClient
from tiny_hermes.runs.application.model_router import ModelRouter
from tiny_hermes.runs.application.scheduler import (
    SchedulerRuntime,
    SchedulerSettings,
)
from tiny_hermes.runs.application.service import RunCoordination
from tiny_hermes.runs.application.worker import (
    WorkerRuntime,
    WorkerSettings,
    WorkspaceRuntime,
)
from tiny_hermes.runs.infrastructure.deterministic_model import (
    DeterministicModelProvider,
)
from tiny_hermes.runs.infrastructure.http_tool_sender import OutboundHttpToolSender
from tiny_hermes.runs.infrastructure.mcp_gateway import OutboundMcpGateway
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.infrastructure.openai_model import RetryPolicy
from tiny_hermes.runs.infrastructure.redis_notifier import RedisWakeUpNotifier
from tiny_hermes.runs.infrastructure.skill_library import SqlSkillLibrary
from tiny_hermes.runs.infrastructure.skill_proposals import SqlSkillProposals
from tiny_hermes.runs.infrastructure.sql_approvals import SqlApprovalGate
from tiny_hermes.runs.infrastructure.sql_artifact_reads import SqlArtifactReads
from tiny_hermes.runs.infrastructure.sql_children import SqlChildRuns
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.ports.http_calls import EgressClaim
from tiny_hermes.runs.ports.notifier import WakeUpNotifier
from tiny_hermes.sandbox.transport.adapter import SandboxClient
from tiny_hermes.sandbox.transport.client import ControllerClient
from tiny_hermes.secrets.domain.envelope import optional_kek
from tiny_hermes.secrets.infrastructure.sql_store import SqlSecretStore
from tiny_hermes.session_workspace.domain.models import WorkspaceQuota
from tiny_hermes.session_workspace.infrastructure.minio_store import MinioObjectStore
from tiny_hermes.shared.config import Settings, get_settings
from tiny_hermes.shared.database import build_session_factory
from tiny_hermes.shared.errors import AppError
from tiny_hermes.shared.logging import configure_logging

logger = logging.getLogger(__name__)


def main() -> None:
    uvicorn.run(
        "tiny_hermes.api.app:app",
        host="0.0.0.0",  # noqa: S104 - container port must accept traffic outside itself
        port=8000,
    )


def worker_main() -> None:
    configure_logging()
    asyncio.run(_worker())


async def _worker() -> None:
    settings = get_settings()
    worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    notifier = _notifier(settings)
    sessions = build_session_factory(settings)
    workspace = _workspace(settings)
    await _ensure_bucket(workspace)
    if workspace is not None:
        logger.info(
            "workspace runtime configured: quota_bytes=%s", workspace.quota.max_bytes
        )
    # One provider port, two providers behind it. Which one answers is decided
    # per round by the Agent Version the Run fixed at creation, so the Worker
    # never learns that endpoints exist.
    runtime = WorkerRuntime(
        session_factory=sessions,
        model=ModelRouter(
            deterministic=DeterministicModelProvider(settings.deterministic_model_delay_ms),
            session_factory=sessions,
            # A model call belongs to the platform and names no workspace: the
            # endpoint it reaches was approved by a platform administrator, and
            # a Run cannot widen that by being in one workspace rather than
            # another. Absent egress settings make this client refuse rather
            # than connect, which is what stops a misconfigured Worker from
            # quietly going direct.
            client_factory=lambda: SafeOutboundClient(
                egress=_egress(settings),
                connect_timeout=settings.outbound_connect_timeout_seconds,
                read_timeout=settings.outbound_read_timeout_seconds,
                max_redirects=settings.outbound_max_redirects,
                max_response_bytes=settings.outbound_max_response_bytes,
            ),
            retry=RetryPolicy(
                max_attempts=settings.model_max_attempts,
                base_ms=settings.model_retry_base_ms,
            ),
            kek=optional_kek(settings.tiny_hermes_kek),
        ),
        notifier=notifier,
        # Absent when no image is approved: a deployment that has not chosen
        # one cannot run a tool, and a Run that binds one fails rather than
        # running the command anywhere else.
        sandbox=_controller(settings),
        workspace=workspace,
        # Skill text is read from the catalog the same database holds, so a
        # deployment configures nothing extra to give an Agent skills.
        skills=SqlSkillLibrary(sessions),
        # The Agent's half of §15.3. It writes proposals and can approve
        # none of them, which is the whole governance story in one field.
        proposals=SqlSkillProposals(sessions),
        # How a picture a person sent becomes something the model can see.
        # One client per workspace, unlike the model call above: a model
        # endpoint is approved platform-wide, but an image is fetched from
        # the channel that received it, and §16.5 measures that against the
        # workspace which owns the binding.
        images=ChannelImageSource(
            sessions,
            lambda workspace_id: SafeOutboundClient(
                # `_workspace_egress`, not `_egress(EgressClaim(...))`: that
                # claim needs an `agent_version_id`, and fetching a picture
                # somebody sent has none to give honestly. The Agent did not
                # ask to call Feishu — the platform is collecting the input it
                # was handed — and naming an Agent version would measure the
                # fetch against a `network.allow` no Agent author wrote for it.
                egress=_workspace_egress(settings, workspace_id),
                connect_timeout=settings.outbound_connect_timeout_seconds,
                read_timeout=settings.outbound_read_timeout_seconds,
                max_redirects=settings.outbound_max_redirects,
                max_response_bytes=settings.outbound_max_response_bytes,
            ),
            optional_kek(settings.tiny_hermes_kek),
        ),
        # An Agent's calls to somebody else's API leave from here rather than
        # from the sandbox, so the credential stays on this side and the
        # request passes the same egress boundary as every other outbound
        # call. Unconfigured egress makes this refuse, not connect.
        # §16.3's gate. Absent, a write that would need a person is refused
        # rather than run: a platform that cannot ask must not decide.
        approvals=SqlApprovalGate(sessions),
        # §16.2's revalidation and calls, out through the same boundary and
        # with the same layers named.
        mcp=OutboundMcpGateway(
            sessions,
            lambda claim: SafeOutboundClient(
                egress=_egress(settings, claim),
                connect_timeout=settings.outbound_connect_timeout_seconds,
                read_timeout=settings.outbound_read_timeout_seconds,
                max_redirects=settings.outbound_max_redirects,
                max_response_bytes=settings.outbound_max_response_bytes,
            ),
            kek=optional_kek(settings.tiny_hermes_kek),
        ),
        http_sender=OutboundHttpToolSender(
            sessions,
            lambda claim: SafeOutboundClient(
                # Unlike a model call, this one names its layers: the Agent's
                # own `network.allow` is one of them, and a call that named
                # nothing would be measured against the platform alone.
                egress=_egress(settings, claim),
                connect_timeout=settings.outbound_connect_timeout_seconds,
                read_timeout=settings.outbound_read_timeout_seconds,
                max_redirects=settings.outbound_max_redirects,
                max_response_bytes=settings.outbound_max_response_bytes,
            ),
            kek=optional_kek(settings.tiny_hermes_kek),
        ),
        # §14.1's write path. Absent, `memory.remember` is refused rather
        # than silently dropped; the same database holds the memories.
        memories=SqlMemoryCandidates(sessions),
        # §14.3's retrieval, scoped to the Run's own subject.
        searches=SqlRunSessionSearches(sessions),
        # §13's delegation. Absent, `agent.delegate` is refused rather than
        # silently answered with nothing; the children live in the same
        # database and share the parent's root budget.
        children=SqlChildRuns(sessions),
        # §13's eighth clause: files reach a Run as authorizations, and
        # this is the only way one opens what it was passed.
        artifacts=SqlArtifactReads(
            sessions, None if workspace is None else workspace.objects
        ),
        settings=WorkerSettings(
            worker_id=worker_id,
            lease_seconds=settings.worker_lease_seconds,
            max_slice_seconds=settings.worker_max_slice_seconds,
            idle_poll_seconds=settings.worker_idle_poll_seconds,
            sandbox_idle_ttl_seconds=settings.sandbox_idle_ttl_seconds,
        ),
    )
    stop = _stop_on_termination()
    logger.info("worker started", extra={"worker_id": worker_id})
    try:
        await runtime.run_forever(stop)
    finally:
        await notifier.close()
    logger.info("worker stopped", extra={"worker_id": worker_id})


def _egress(settings: Settings, claim: EgressClaim | None = None) -> EgressRoute | None:
    """The route out, when this deployment has one.

    `None` when either half is unset, and a client built with `None` refuses
    every call. Failing closed here rather than falling back is the whole of
    the stage's exit check: nothing in the code can reach the network without
    passing the boundary, so nobody has to remember not to.
    """
    if not settings.egress_proxy_url or not settings.egress_proxy_token:
        return None
    return EgressRoute(
        url=settings.egress_proxy_url,
        token=settings.egress_proxy_token,
        # Named only when a caller has layers to be measured against. Naming
        # them can only narrow what the request may reach — the proxy looks
        # each id up itself.
        workspace_id=None if claim is None else claim.workspace_id,
        agent_version_id=None if claim is None else claim.agent_version_id,
        run_id=None if claim is None else claim.run_id,
    )


def _controller(settings: Settings) -> SandboxClient | None:
    """The Controller over its socket, when this deployment has one."""
    if not settings.sandbox_image_digest:
        return None
    return SandboxClient(ControllerClient(settings.sandbox_controller_socket))


async def _ensure_bucket(workspace: WorkspaceRuntime | None) -> None:
    """Idempotent bucket creation at boot, so the first checkpoint is not the
    request that discovers a fresh MinIO. A failure here is logged rather than
    fatal: the store may simply not be up yet, and every later operation
    reports `workspace_storage_unavailable` honestly."""
    if workspace is None:
        return
    ensure = getattr(workspace.objects, "ensure_bucket", None)
    if ensure is None:
        return
    try:
        await ensure()
    except Exception:
        logger.exception("object-store bucket check failed at startup")


def _workspace(settings: Settings) -> WorkspaceRuntime | None:
    """Persistent session files, when this deployment runs sandboxes at all."""
    if not settings.sandbox_image_digest:
        return None
    return WorkspaceRuntime(
        objects=MinioObjectStore(
            endpoint=settings.s3_endpoint,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            bucket=settings.s3_bucket,
        ),
        quota=WorkspaceQuota(
            max_bytes=settings.workspace_max_bytes,
            max_objects=settings.workspace_max_objects,
        ),
        staging_ttl_seconds=settings.workspace_staging_ttl_seconds,
        # Tar framing adds headers and padding on top of the quota's bytes.
        export_limit=settings.workspace_max_bytes + settings.workspace_max_objects * 2048,
        artifact_max_bytes=settings.artifact_max_bytes,
        run_artifact_max_bytes=settings.run_artifact_max_bytes,
        preview_bytes=settings.shell_output_bytes,
    )


def scheduler_main() -> None:
    configure_logging()
    asyncio.run(_scheduler())


async def _scheduler() -> None:
    settings = get_settings()
    notifier = _notifier(settings)
    workspace = _workspace(settings)
    await _ensure_bucket(workspace)
    senders = _FeishuSenders(settings)
    sessions = build_session_factory(settings)
    runtime = SchedulerRuntime(
        session_factory=sessions,
        notifier=notifier,
        sandbox=_controller(settings),
        objects=None if workspace is None else workspace.objects,
        replies=_channel_replies(settings, senders),
        settings=SchedulerSettings(
            max_recovery_attempts=settings.max_recovery_attempts,
            event_retention_hours=settings.event_retention_hours,
        ),
    )
    stop = _stop_on_termination()
    # NOTE: read once, here, at process start. A binding created — or
    # switched to `long_connection` — after this point gets no socket until
    # the scheduler process is restarted; see `_long_connections`'s
    # docstring for why that race is not worth solving.
    connections = await _long_connections(settings, sessions)
    logger.info(
        "scheduler started: long_connection bindings=%d of at most one per"
        " process (a binding created or switched to long_connection after"
        " startup needs a scheduler restart to take effect; a second one is"
        " refused with a channel.long_connection.not_started audit row)",
        len(connections),
    )
    try:
        await asyncio.gather(
            runtime.run_forever(stop, settings.scheduler_interval_seconds),
            # Shares `stop` with the main loop on purpose: a termination
            # signal has to close both the lease/reply loop and every open
            # socket together, rather than the main loop exiting first and
            # leaving connections open until their own timeout.
            *(_supervised_connection(connection, stop) for connection in connections),
        )
    finally:
        await notifier.close()
        await senders.aclose()
    logger.info("scheduler stopped")


async def _supervised_connection(
    connection: FeishuLongConnection,
    stop: asyncio.Event,
    *,
    first_delay: float = 5.0,
    max_delay: float = 300.0,
) -> None:
    """Retries a socket that **fails to establish**, with backoff — instead
    of letting the exception propagate into the `asyncio.gather` in
    `_scheduler`, which would cancel every other connection and the
    lease/reply loop along with it.

    Establishing is all it covers, and the narrowness is not an oversight
    worth papering over with a wider promise. What is missing is *recovery*,
    not a trace: once `connect_until_ready` has returned, a drop is handled
    inside the SDK's own thread (`_receive_message_loop` → `_reconnect` in
    `lark_oapi/ws/client.py`), and `_reconnect` calls `on_reconnecting()`
    before it starts retrying, which reaches `_on_reconnecting` here and
    writes a `disconnected` row — so a drop after a successful connect is
    visible on the audit page. What nobody does is check that the socket
    the SDK is nursing is still alive: `connection.run(stop)` stays parked
    on `stop.wait()` either way, and if the SDK's own retrying never
    succeeds this function never learns of it. **That liveness check is a
    known gap on this branch, not a covered case.** (It does not usually
    end in an exception either: the client's `_reconnect_count` defaults to
    `-1`, whose branch retries forever and never raises. Only a server that
    pushes a non-negative `ReconnectCount` makes it give up, and the
    `ServerUnreachableException` it then raises dies inside a task on the
    ws loop rather than surfacing here.)

    `FeishuLongConnection.run`'s own docstring leaves the retry-or-give-up
    call to whoever hosts it here; retrying with backoff, bounded by `stop`
    so a shutdown mid-backoff exits promptly, is that decision. A bad
    binding (wrong credentials, app not enabled for long connection) still
    cannot send replies, cards, or receipts for anyone else — bringing the
    whole process down over it would be strictly worse than logging and
    trying again.

    **At most two audit rows per outage per process, however long the
    outage lasts**, which is why the outage lives here as a local rather
    than inside `run()`: one attempt cannot tell whether it is the first
    failure or the two-hundredth. The first failure writes `connect_failed`;
    every retry after it only logs, carrying the attempt count — unless that
    write did not reach the table, in which case the next retry tries it
    again, which is one INSERT per backoff and lands at most one row; the
    connect that finally succeeds writes `reconnected` with how long the
    whole run of failures lasted. Per attempt this was ~262 rows a day for one
    wrong app secret, against an `audit_events` table nothing prunes — the
    workspace's audit page (`created_at desc`, no filter by default) would
    show nothing else, and §19.2 needs the closing row anyway: an outage
    window with only a left edge measures nothing.

    **Per process is the honest scope, and the gap is real.** `outage_since`
    and `audited` are locals: a scheduler restarted in the middle of an
    outage begins a *new* one and writes a second `connect_failed`, and the
    first one's window is never closed by anybody. §19.2's reader sees two
    consecutive left edges and one window that stays half-open forever.
    Nothing here can fix that — closing it would mean reading the previous
    process's rows back out of `audit_events` at startup, which is a
    different design from "the retry loop owns the outage" — so it is
    written down rather than papered over.

    The pair is only ever written as a pair: `record_connect_failed` reports
    whether its row actually reached the table (every failure inside it is
    swallowed, deliberately, so an audit hiccup cannot take the socket
    down), and only a `True` from it lets the recovery row be written. "We
    called it" is not "the row is there", and treating them as the same
    produced a `reconnected` with nothing on its left — a green
    "succeeded" in the audit page for an outage no reader can measure.

    The two delays are a judgment call, not a requirement — the plan gives
    no numbers. 5s so a Feishu blip or a scheduler that started before the
    network did recovers within one attempt instead of sitting out a long
    first backoff; 300s as the ceiling because the failures that survive
    several retries are configuration (secret wrong, app not enabled for
    long connection) and those are fixed by a human, at which point the
    scheduler is restarted anyway — polling faster than every five minutes
    buys nothing once the failure is already recorded.
    """
    delay = first_delay
    #: When the current run of failures began, and whether its
    #: `connect_failed` row is really in the table — not whether writing it
    #: was attempted. `None`/`False` means there is no outage open that this
    #: loop is able to close.
    outage_since: float | None = None
    audited = False
    failures = 0
    came_up = False

    async def close_out_the_outage() -> None:
        """Called from inside `run()` the moment the socket is up."""
        nonlocal outage_since, audited, failures, delay, came_up
        came_up = True
        if outage_since is None:
            return
        if not audited:
            # An outage this loop never managed to open. Either it was a
            # round that came up and then died (no `connect_failed` belongs
            # to it — `_on_reconnecting` owns that row and writes it only if
            # the SDK signalled), or the left-edge write itself failed. A
            # `reconnected` here would be a right edge closing onto nothing,
            # which reads on the audit page as an outage that ended well and
            # measures, for §19.2, exactly nothing.
            logger.info(
                "long connection binding=%s is up again after %d failure(s), with"
                " no recorded outage to close",
                connection.binding_id,
                failures,
            )
            outage_since, failures = None, 0
            return
        logger.info(
            "long connection binding=%s is up after %d failed attempt(s)",
            connection.binding_id,
            failures,
        )
        await connection.record_recovered(time.monotonic() - outage_since)
        outage_since, audited, failures = None, False, 0
        # Reset here and nowhere else. The backoff is what keeps a wedged
        # binding from hammering, and the only thing that has earned its
        # release is a run of failures that demonstrably ended.
        #
        # It used to be reset at the top of this function, on *any* connect.
        # No round can reach it today — a round that comes up and then
        # raises has to have had `stop` set for `run()`'s `finally` to run
        # at all, and the loop below returns on `stop.is_set()` — so this is
        # a correctness move, not a fix for a spin anyone has observed. What
        # it removes is the shape: a round that came up and died would
        # otherwise hand the next round `first_delay` again, forever.
        delay = first_delay

    while not stop.is_set():
        came_up = False
        try:
            await connection.run(stop, on_connected=close_out_the_outage)
            return
        except Exception:
            failures += 1
            if outage_since is None:
                outage_since = time.monotonic()
            # A round that came up and then failed is not a *connect*
            # failure; `_on_reconnecting` already wrote its own row if the
            # SDK signalled the drop, and calling this one "connect_failed"
            # would name the wrong thing.
            worth_a_row = not audited and not came_up
            logger.exception(
                "long connection failed binding=%s: failure %d of this outage,"
                " retrying in %.0fs%s",
                connection.binding_id,
                failures,
                delay,
                "" if worth_a_row else " (logged only; the outage is already recorded)",
            )
            if worth_a_row:
                audited = await connection.record_connect_failed()
        if stop.is_set():
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=delay)
        delay = min(delay * 2, max_delay)


def _deliver_via(
    sessions: async_sessionmaker[AsyncSession], kek: bytes | None
) -> DeliverFrame:
    """One `deliver`, shared by every long-connection binding.

    A fresh session per frame — not the session `_long_connections` used to
    read bindings and resolve credentials, which is long closed by the time
    a real frame arrives.

    The whole `FeishuChannelService`, not `accept_verified` alone. Claiming
    is only the half both transports share; the half after it — refusal
    receipts, command receipts, the Run, and `attach_run` — is what the
    outbound scan reads. For one task this function stopped at the claim,
    and the result was the shape this repository keeps producing: rows in
    `channel_events`, no Run, nobody answered. `deliver_verified` is the
    same six steps the webhook route takes, called from here rather than
    copied, because a second copy is a second place to forget one of them.

    Assembled per frame the way `resources.feishu_channel_service`
    assembles it per request, and for that docstring's reason: the claim
    and the Run it leads to commit or roll back together. The claim is what
    stops Feishu retrying, so a claim committed without its Run is a
    message lost with nothing left to say so.
    """

    async def deliver(binding_id: UUID, envelope: dict[str, Any]) -> None:
        async with sessions() as session:
            store = SqlChannelStore(session)
            service = FeishuChannelService(
                bindings=store,
                resolve_secret=CredentialResolver(SqlSecretStore(session), kek).resolve,
                webhooks=FeishuWebhookService(store),
                ingestion=ChannelIngestion(
                    subjects=SqlEndUserStore(session),
                    conversations=store,
                    runs=RunCoordination(SqlRunStore(session)),
                ),
            )
            try:
                await service.deliver_verified(
                    binding_id=binding_id,
                    envelope=envelope,
                    request_id=f"lc-{uuid.uuid4()}",
                )
            except AppError as error:
                # An audited refusal has already written its row in this
                # session; rolling back would erase the only record that
                # the delivery was refused and why. Same branch, same
                # reason, as the request-scoped assembly.
                if error.audited:
                    await session.commit()
                else:
                    await session.rollback()
                raise
            except BaseException:
                await session.rollback()
                raise
            else:
                await session.commit()

    return deliver


class _RecordLongConnectionEvent(Protocol):
    """What this process writes with, which is wider than what the adapter
    asks for.

    `RecordConnectionEvent` (the adapter's own port) covers the three kinds
    a live socket produces. This process writes a fourth — `not_started`,
    for a binding it refuses to connect — whose entire value is the
    explanation, so it needs `context`. Kept as a separate protocol rather
    than widening the adapter's: a port exists to bound what its holder can
    ask for, and the adapter has no business writing free-form context.

    That this is still usable *as* a `RecordConnectionEvent` is not a
    promise made here — it is checked where it is passed, at the
    `FeishuLongConnection(..., record=...)` call in `_long_connections`.
    """

    async def __call__(
        self,
        binding_id: UUID,
        kind: str,
        down_seconds: float | None,
        *,
        context: dict[str, Any] | None = None,
    ) -> None: ...


def _connection_event_recorder(
    sessions: async_sessionmaker[AsyncSession], workspace_id: UUID
) -> _RecordLongConnectionEvent:
    """Writes disconnect/reconnect events into the same `audit_events` table
    the console's audit page already reads and renders — not a new table
    with no reader. §19.2's later redelivery check needs `down_seconds` to
    be something a person can actually go look at, and `context` is already
    served by `/api/v1/audit-events` and rendered as a column in
    `AuditPage.tsx`, so a row landing here reaches someone the moment it is
    written rather than only existing as an INSERT.

    `actor_type="platform"`, following `sandbox/cli.py`'s `_AuditSink`:
    no user asked for this reconnect, the socket did it on its own.

    `down_seconds` is always written, `None` included. Dropping the key
    when there is no number made "the outage's length is not known" and
    "this row carries no context at all" the same row to whoever reads
    `/api/v1/audit-events` — and the second is what a reader assumes.

    `context` widens that same column for a kind whose whole point is the
    explanation (`not_started`: why this process refused to connect a
    binding at all — another binding holds the process's one socket, or the
    binding's own credentials are unset or unresolvable). Keyword-only with
    a default, so this still
    satisfies `RecordConnectionEvent` — the adapter calls it with three
    positional arguments and knows nothing about the extra.
    """

    async def record(
        binding_id: UUID,
        kind: str,
        down_seconds: float | None,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        async with sessions() as session:
            session.add(
                AuditEventRow(
                    workspace_id=workspace_id,
                    actor_type="platform",
                    actor_id=None,
                    action=f"channel.long_connection.{kind}",
                    resource_type="channel_binding",
                    resource_id=binding_id,
                    # The audit page renders `result` next to the action. A
                    # connection that came back up is the only one of these
                    # kinds that ended well; calling any of the others
                    # "succeeded" would put a green word next to an outage,
                    # or next to a binding this process refused to connect.
                    result="succeeded" if kind == "reconnected" else "failed",
                    request_id=f"long-connection-{binding_id}-{kind}-{uuid.uuid4().hex[:8]}",
                    context={"down_seconds": down_seconds, **(context or {})},
                )
            )
            await session.commit()

    return record


async def _long_connections(
    settings: Settings, sessions: async_sessionmaker[AsyncSession]
) -> tuple[FeishuLongConnection, ...]:
    """The long-connection binding this scheduler process holds a socket
    for — read once, at process start, never polled again.

    **At most one, and the ones left out say so out loud.** The SDK's
    WebSocket loop is a module-level global built at import time
    (`lark_oapi/ws/client.py:31-35`) that `Client.start()` drives with
    `run_until_complete`, and `FeishuChannel.stop()` ends by stopping *that*
    loop (`_stop_private_ws_client`, `lark_oapi/channel/channel.py:853-876`)
    — so a second binding in this process has no loop to start on, and the
    first binding's teardown would stop the loop the second one's live
    socket is running on. Returning both and letting them fight is the shape
    of bug this repository keeps producing: the console would show the
    second binding on `long_connection`, the administrator would restart the
    scheduler as told, and no message would ever arrive, with a green
    backend test suite the whole time. So the extras are refused *visibly* —
    an error log and one `channel.long_connection.not_started` audit row
    each, naming the binding that took the socket — and the refusal is
    written where the console already reads.

    Which binding wins is by `id`, only because the choice has to be stable
    across restarts: an arbitrary one would make "why is it this binding
    today" unanswerable. It is not a priority — a deployment that wants a
    different binding connected has to stop giving this process two.

    `run_forever` already owns the process's one recurring loop, and
    teaching this to notice a binding created (or switched to
    `long_connection`) after startup would mean answering what happens to a
    handshake in flight when its binding disappears mid-connect. That race
    is not worth solving for an edit this infrequent, so it is not solved:
    **a binding change needs a scheduler restart** — `_scheduler`'s startup
    log line says so, because a user who edits a binding and sees nothing
    happen needs to know this before assuming it is broken.

    Credentials are resolved inside the same session that just read the
    row, not a resolver built after that session closes — a resolver built
    later answers `CredentialMissing` for every id (the bug
    `fix(channels): resolve the image secret through a real session` fixed
    for the image-fetch path).

    **A binding skipped over its credentials is refused just as visibly**,
    and for the same reason as the crowded-out one: nothing here edits the
    binding, so the console goes on showing it on `long_connection`. A
    secret disabled on the secrets page days after the transport was
    switched would otherwise show up only in this process's log — read by
    whoever runs the container, not by whoever flipped the switch. The two
    causes get two different wordings because they need two different
    fixes: fill the credentials in, or re-enable that Secret.

    These rows are bounded — one per unusable binding, and `_scheduler`
    calls this once before `run_forever` rather than from any loop. That
    bound is the whole reason they are affordable: `audit_events` has no
    retention or cleanup anywhere in this repository, so nothing here may
    move into a retry or polling path.
    """
    kek = optional_kek(settings.tiny_hermes_kek)
    deliver = _deliver_via(sessions, kek)
    #: Bindings whose credentials actually resolve, in `id` order. Ordered by
    #: the database rather than sorted here so the query does the tie-break
    #: too, and filtered *before* the one-per-process cut: a binding skipped
    #: for missing credentials must not be the one that "took" the socket,
    #: or a usable binding would be refused on behalf of one that was never
    #: going to connect either.
    usable: list[tuple[ChannelBindingRow, LongConnectionBinding]] = []
    async with sessions() as session:
        rows = (
            await session.scalars(
                select(ChannelBindingRow)
                .where(
                    ChannelBindingRow.transport == "long_connection",
                    ChannelBindingRow.status == "active",
                )
                .order_by(ChannelBindingRow.id)
            )
        ).all()
        credentials = CredentialResolver(SqlSecretStore(session), kek)
        for row in rows:
            if row.app_id is None or row.app_secret_ref is None:
                logger.warning(
                    "long connection binding %s has no app credentials"
                    " configured, skipping",
                    row.id,
                )
                await _connection_event_recorder(sessions, row.workspace_id)(
                    row.id,
                    "not_started",
                    None,
                    context={
                        "reason": (
                            "no app credentials are configured on this binding"
                            " (app_id or the secret reference is unset), so there"
                            " is nothing to open a connection with"
                        )
                    },
                )
                continue
            try:
                secret = await credentials.resolve(row.app_secret_ref)
            except CredentialMissing:
                logger.exception(
                    "long connection binding %s: app secret unavailable,"
                    " skipping",
                    row.id,
                )
                await _connection_event_recorder(sessions, row.workspace_id)(
                    row.id,
                    "not_started",
                    None,
                    context={
                        # Naming the reference is the difference between "go
                        # look at the secrets page" and "go look at *this*
                        # secret". The two causes need different fixes, so
                        # the wording has to separate them: this one is not
                        # "unconfigured", it is configured and unusable.
                        "reason": (
                            "the app secret this binding references did not"
                            " resolve: there is no Secret with that id, or its"
                            " status is not active"
                        ),
                        "app_secret_ref": row.app_secret_ref,
                    },
                )
                continue
            usable.append(
                (
                    row,
                    LongConnectionBinding(
                        binding_id=row.id, app_id=row.app_id, app_secret=secret
                    ),
                )
            )
    if not usable:
        return ()
    (held, credential), crowded_out = usable[0], usable[1:]
    for row, _ in crowded_out:
        logger.error(
            "long connection binding %s is configured for the long connection but"
            " will not be connected: binding %s already holds this process's only"
            " SDK WebSocket loop. Run it in a scheduler process of its own, or"
            " switch it back to the webhook transport.",
            row.id,
            held.id,
        )
        await _connection_event_recorder(sessions, row.workspace_id)(
            row.id,
            "not_started",
            None,
            context={
                "reason": (
                    "another long_connection binding already holds this scheduler"
                    " process's only SDK WebSocket loop"
                ),
                "holding_binding_id": str(held.id),
            },
        )
    return (
        FeishuLongConnection(
            credential,
            deliver,
            record=_connection_event_recorder(sessions, held.workspace_id),
        ),
    )


class _FeishuSenders:
    """One Feishu sender per workspace, kept for the process's lifetime.

    Per workspace because §16.5's chain is platform ∩ workspace ∩ … and the
    layers a request is measured against are fixed when the proxy connection
    is configured, not per call. A single shared client would name no
    workspace, so every reply would be measured against the platform layer
    alone and a workspace's own outbound scope would mean nothing.

    Kept rather than rebuilt because the tenant-access-token cache lives on
    the sender, and Feishu rate-limits the token endpoint. The number of
    workspaces with a channel binding is small and bounded by the
    installation, so this does not grow without limit.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._clients: dict[UUID, SafeOutboundClient] = {}
        self._senders: dict[UUID, FeishuSender] = {}

    def __call__(self, workspace_id: UUID, /) -> FeishuSender:
        held = self._senders.get(workspace_id)
        if held is None:
            client = SafeOutboundClient(
                egress=_workspace_egress(self._settings, workspace_id)
            )
            self._clients[workspace_id] = client
            held = FeishuSender(client)
            self._senders[workspace_id] = held
        return held

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()


def _workspace_egress(settings: Settings, workspace_id: UUID) -> EgressRoute | None:
    """The route out, named for one workspace and no Agent.

    Not `_egress(settings, EgressClaim(...))`: that claim requires an
    `agent_version_id`, and a channel reply has none to give honestly. The
    Agent did not ask to call Feishu — the platform is delivering its own
    notification — and naming an Agent version would measure the send
    against an `network.allow` no Agent author wrote for this.
    """
    if not settings.egress_proxy_url or not settings.egress_proxy_token:
        return None
    return EgressRoute(
        url=settings.egress_proxy_url,
        token=settings.egress_proxy_token,
        workspace_id=workspace_id,
    )


def _channel_replies(
    settings: Settings, senders: _FeishuSenders
) -> Callable[[AsyncSession], ChannelReplyDispatcher] | None:
    """The reply dispatcher, when this deployment can send anything at all.

    `None` without an egress route, and that is not a silent downgrade: a
    client built without a route refuses every call, so wiring one anyway
    would produce a queue whose every row failed five times and settled
    `refused`. Absent is the honest state — the answers wait in the queue,
    and `reply_note` stays empty rather than blaming Feishu.
    """
    if _egress(settings) is None:
        logger.warning("no egress route: channel replies will not be sent")
        return None
    kek = optional_kek(settings.tiny_hermes_kek)

    def build(session: AsyncSession) -> ChannelReplyDispatcher:
        return ChannelReplyDispatcher(
            store=SqlChannelStore(session),
            resolve_secret=CredentialResolver(SqlSecretStore(session), kek).resolve,
            senders=senders,
            console_url=settings.console_base_url or None,
        )

    return build


def _notifier(settings: Settings) -> WakeUpNotifier:
    url = settings.redis_url
    return RedisWakeUpNotifier(url) if url else NullWakeUpNotifier()


def _stop_on_termination() -> asyncio.Event:
    stop = asyncio.Event()
    running = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        received = getattr(signal, name, None)
        if received is None:
            continue
        with contextlib.suppress(NotImplementedError):
            running.add_signal_handler(received, stop.set)
    return stop

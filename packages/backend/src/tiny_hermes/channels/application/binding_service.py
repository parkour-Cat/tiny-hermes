"""Managing the row that makes a channel exist.

Separate from `feishu_service.py`, which is the inbound path. This is the
outbound-facing half of §20.1's Channels: creating a binding publishes an
Agent into somebody else's messaging tenant, and until it existed the only
thing that ever wrote `channel_bindings` was a test.

§4.6's line is `密钥、安全策略与渠道`:

- workspace and platform administrators **manage metadata and never see
  plaintext**, so nothing here returns a key — only the reference;
- a developer `使用已授权绑定`: they may see what is bound, because using a
  binding means knowing it is there, and may not create one;
- a viewer gets `否`, not `只读`. A binding names an Agent and an app id,
  which together say this workspace publishes that Agent into that tenant.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from tiny_hermes.tenancy.domain.models import Actor, Role

#: Who may see what is bound. A developer is here because §4.6 lets them use
#: an authorized binding, and a viewer is deliberately absent.
LISTERS = frozenset({Role.WORKSPACE_ADMIN, Role.DEVELOPER})
#: Who may open or close a channel.
MANAGERS = frozenset({Role.WORKSPACE_ADMIN})

#: Channels whose deliveries this platform decrypts, and which therefore
#: cannot be bound without a key to decrypt them with. Mirrors migration
#: 0037's CHECK rather than restating a rule the schema already holds.
ENCRYPTED_CHANNELS = frozenset({"feishu"})


class ForbiddenChannelAction(Exception):
    pass


class ChannelKeyRequired(Exception):
    """A Feishu binding with no key reference. The schema refuses it too;
    this refuses it where the person who typed it is still looking."""


class ChannelKeyUnknown(Exception):
    """The reference names no secret in this workspace.

    Worth its own refusal: a binding pointing at a secret that does not
    exist accepts deliveries it can never decrypt, and fails at the far end
    — inside a webhook, hours later, where nobody is watching.
    """


class UnknownChannel(Exception):
    pass


class ChannelAlreadyBound(Exception):
    """One Agent, one channel, once. The unique constraint says so; this
    turns it into an answer rather than an integrity error."""


@dataclass(frozen=True)
class ChannelBindingView:
    """What a console is shown. No key, and no room for one."""

    id: UUID
    workspace_id: UUID
    channel: str
    agent_id: UUID
    status: str
    app_id: str | None
    encrypt_key_ref: str | None
    #: The app secret this binding replies with, by reference. Absent for a
    #: receive-only binding.
    app_secret_ref: str | None
    created_by: UUID
    created_at: datetime


class ChannelBindingStore(Protocol):
    async def user_role(self, workspace_id: UUID, user_id: UUID) -> Role | None: ...

    async def secret_exists(self, workspace_id: UUID, reference: str) -> bool:
        """By the **id** a `CredentialResolver` would resolve, not by name."""
        ...

    async def agent_exists(self, workspace_id: UUID, agent_id: UUID) -> bool: ...

    async def create_binding(
        self,
        *,
        workspace_id: UUID,
        channel: str,
        agent_id: UUID,
        created_by: UUID,
        app_id: str | None,
        encrypt_key_ref: str | None,
        app_secret_ref: str | None,
    ) -> ChannelBindingView | None: ...

    async def list_bindings(
        self, workspace_id: UUID
    ) -> tuple[ChannelBindingView, ...]: ...

    async def binding(
        self, workspace_id: UUID, binding_id: UUID
    ) -> ChannelBindingView | None: ...

    async def update_binding(
        self,
        workspace_id: UUID,
        binding_id: UUID,
        changes: dict[str, str | None],
    ) -> ChannelBindingView | None: ...

    async def disable_binding(
        self, workspace_id: UUID, binding_id: UUID
    ) -> ChannelBindingView | None: ...

    async def append_audit(
        self,
        *,
        workspace_id: UUID,
        actor_id: UUID,
        action: str,
        resource_id: UUID,
        request_id: str,
    ) -> None: ...


@dataclass(frozen=True)
class ChannelBindingService:
    store: ChannelBindingStore

    async def create(
        self,
        actor: Actor,
        workspace_id: UUID,
        *,
        channel: str,
        agent_id: UUID,
        app_id: str | None,
        encrypt_key_ref: str | None,
        app_secret_ref: str | None,
        request_id: str,
    ) -> ChannelBindingView:
        await self._require_role(
            actor, workspace_id, request_id, allowed=MANAGERS,
            audit_as_platform="channel.binding_created_by_platform_admin",
        )
        if channel in ENCRYPTED_CHANNELS and not encrypt_key_ref:
            raise ChannelKeyRequired
        if encrypt_key_ref and not await self.store.secret_exists(
            workspace_id, encrypt_key_ref
        ):
            raise ChannelKeyUnknown
        # Same check for the app secret, and the same reason: a reference to
        # a secret that does not exist fails inside an outbound call nobody
        # is watching, not here where the person who typed it is.
        if app_secret_ref and not await self.store.secret_exists(
            workspace_id, app_secret_ref
        ):
            raise ChannelKeyUnknown
        if not await self.store.agent_exists(workspace_id, agent_id):
            # Checked rather than left to the foreign key: an id from another
            # workspace would otherwise come back as an integrity error, and
            # the difference between "no such Agent" and "not yours" is one
            # this platform does not tell a caller anyway.
            raise UnknownChannel
        created = await self.store.create_binding(
            workspace_id=workspace_id,
            channel=channel,
            agent_id=agent_id,
            created_by=actor.id,
            app_id=app_id,
            encrypt_key_ref=encrypt_key_ref,
            app_secret_ref=app_secret_ref,
        )
        if created is None:
            raise ChannelAlreadyBound
        await self.store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="channel.binding_created",
            resource_id=created.id,
            request_id=request_id,
        )
        return created

    async def list(
        self, actor: Actor, workspace_id: UUID, request_id: str
    ) -> tuple[ChannelBindingView, ...]:
        await self._require_role(
            actor, workspace_id, request_id, allowed=LISTERS,
            audit_as_platform="channel.bindings_read_by_platform_admin",
        )
        return await self.store.list_bindings(workspace_id)

    async def update(
        self,
        actor: Actor,
        workspace_id: UUID,
        binding_id: UUID,
        *,
        changes: dict[str, str | None],
        request_id: str,
    ) -> ChannelBindingView:
        """Rewire an existing binding's credentials.

        Exists because `uq_channel_bindings_target` allows one binding per
        (workspace, channel, agent) and `disable` does not release it. A
        binding created before the reply path existed therefore had no way
        to acquire an app secret and no way to be replaced — permanently
        receive-only, with every reply settling `no_credential`.

        Deliberately narrow: credentials and `app_id` only. Moving a binding
        to a different Agent would silently redirect every existing
        conversation in `channel_conversations`, which is a different
        operation from fixing a credential and should look like one.
        """
        await self._require_role(
            actor, workspace_id, request_id, allowed=MANAGERS,
            audit_as_platform="channel.binding_updated_by_platform_admin",
        )
        existing = await self.store.binding(workspace_id, binding_id)
        if existing is None:
            raise UnknownChannel
        # Validated against the *result* of the change, not against what was
        # sent: an update that clears the encrypt key and one that never
        # mentioned it are the same binding afterwards, and only the first
        # is refused.
        after_key = changes.get("encrypt_key_ref", existing.encrypt_key_ref)
        if existing.channel in ENCRYPTED_CHANNELS and not after_key:
            raise ChannelKeyRequired
        for field in ("encrypt_key_ref", "app_secret_ref"):
            reference = changes.get(field)
            if reference and not await self.store.secret_exists(
                workspace_id, reference
            ):
                raise ChannelKeyUnknown
        updated = await self.store.update_binding(workspace_id, binding_id, changes)
        if updated is None:
            raise UnknownChannel
        await self.store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="channel.binding_updated",
            resource_id=binding_id,
            request_id=request_id,
        )
        return updated

    async def disable(
        self, actor: Actor, workspace_id: UUID, binding_id: UUID, request_id: str
    ) -> ChannelBindingView:
        await self._require_role(
            actor, workspace_id, request_id, allowed=MANAGERS,
            audit_as_platform="channel.binding_disabled_by_platform_admin",
        )
        # Disabled, never deleted: `channel_events` references this row, and
        # those rows are the record of what the channel already delivered.
        updated = await self.store.disable_binding(workspace_id, binding_id)
        if updated is None:
            raise UnknownChannel
        await self.store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="channel.binding_disabled",
            resource_id=binding_id,
            request_id=request_id,
        )
        return updated

    async def _require_role(
        self,
        actor: Actor,
        workspace_id: UUID,
        request_id: str,
        *,
        allowed: frozenset[Role],
        audit_as_platform: str,
    ) -> None:
        """The same shape `secrets/application/service.py` uses, and for the
        same §4.6 line — a service account is refused outright, and a
        platform admin acting outside their own membership leaves a trace."""
        if actor.is_service_account:
            raise ForbiddenChannelAction
        role = await self.store.user_role(workspace_id, actor.id)
        if role is not None:
            if role not in allowed:
                raise ForbiddenChannelAction
            return
        if not actor.is_platform_admin:
            raise ForbiddenChannelAction
        await self.store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action=audit_as_platform,
            resource_id=workspace_id,
            request_id=request_id,
        )

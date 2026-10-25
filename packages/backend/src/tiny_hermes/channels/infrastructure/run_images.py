"""The Worker's image source, resolved through the channel a Run came from.

A reference names a message and a file, not an app. Which credentials fetch
it is a fact about the Session — `channel_conversations` already maps one to
its binding, which is the same lookup the reply path makes.

Here rather than in `runs`, for the reason `feishu_images` gives: the fetch
is authenticated with a binding's app secret, and a module that plans model
requests has no business holding one.
"""

from collections.abc import Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiny_hermes.channels.domain.image_reference import parse_reference
from tiny_hermes.channels.infrastructure.feishu_images import (
    FeishuFileFetcher,
    FeishuImageFetcher,
)
from tiny_hermes.channels.infrastructure.sql_channel_store import SqlChannelStore
from tiny_hermes.model_catalog.infrastructure.credentials import CredentialResolver
from tiny_hermes.outbound.client import SafeOutboundClient
from tiny_hermes.secrets.infrastructure.sql_store import SqlSecretStore

#: One client per workspace, so §16.5's platform ∩ workspace intersection is
#: measured against the workspace that owns the image — the same reason the
#: reply path builds one per workspace rather than sharing a process-wide one.
ClientFactory = Callable[[UUID], SafeOutboundClient]


class ChannelImageSource:
    """Fetches an image for one Session, using that Session's binding.

    The Session is an argument rather than something this is constructed
    with: a Worker is one per process and a Session is one per Run, so
    binding it here would fetch a second workspace's image with the first
    workspace's token.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        client_for: ClientFactory,
        kek: bytes | None,
    ) -> None:
        self._sessions = sessions
        self._client_for = client_for
        self._kek = kek

    async def data_url_for(self, reference: str, session_id: UUID) -> str:
        picture = parse_reference(reference)
        client, app_id, secret = await self._fetching_for(session_id, "images")
        return await FeishuImageFetcher(client).data_url(
            app_id=app_id,
            app_secret=secret,
            message_id=picture.message_id,
            file_key=picture.file_key,
        )

    async def file_bytes_for(self, reference: str, session_id: UUID) -> bytes:
        """An attached file's bytes, for the sandbox's inbox (§1.27, v2.27)."""
        attached = parse_reference(reference)
        client, app_id, secret = await self._fetching_for(session_id, "files")
        return await FeishuFileFetcher(client).file_bytes(
            app_id=app_id,
            app_secret=secret,
            message_id=attached.message_id,
            file_key=attached.file_key,
        )

    async def _fetching_for(
        self, session_id: UUID, what: str
    ) -> tuple[SafeOutboundClient, str, str]:
        """The client, app id and app secret this Session's binding fetches with."""
        async with self._sessions() as session:
            target = await SqlChannelStore(session).delivery_target_for(session_id)
            if target is None or target.app_id is None or target.app_secret_ref is None:
                # No binding, or a receive-only one. Raised rather than
                # returning nothing: the caller turns this into a stated
                # reason, and the alternative is a question about a picture
                # or a file sent without it.
                raise LookupError(f"session {session_id} has no binding that can fetch {what}")
            # Resolved inside the session that read the binding, the way the
            # reply path does it. A `CredentialResolver` built at assembly time
            # has no session to read secrets through and answers
            # `CredentialMissing` for every id — which is what the first
            # version of this did, surfacing as `image_unavailable` on a Run
            # that had everything else right.
            secret = await CredentialResolver(
                SqlSecretStore(session), self._kek
            ).resolve(target.app_secret_ref)
        # Scoped to the binding's workspace, so §16.5's platform ∩ workspace
        # intersection is measured against the workspace that owns the resource
        # — the same reason the reply path builds one client per workspace.
        return self._client_for(target.workspace_id), target.app_id, secret


__all__ = ["ChannelImageSource"]

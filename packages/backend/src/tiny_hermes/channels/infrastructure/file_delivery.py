"""`file.send`'s channel half (§1.27, v2.27): a saved file, sent into the
conversation the Session belongs to."""

import logging
from collections.abc import Callable
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiny_hermes.audit.infrastructure.tables import AuditEventRow
from tiny_hermes.channels.infrastructure.feishu_files import FeishuFileUploader, OutboundUpload
from tiny_hermes.channels.infrastructure.feishu_sender import FeishuSender
from tiny_hermes.channels.infrastructure.sql_channel_store import SqlChannelStore
from tiny_hermes.model_catalog.infrastructure.credentials import CredentialResolver
from tiny_hermes.runs.application.file_sends import MAX_SEND_BYTES, FileSendRefused, FileSent
from tiny_hermes.secrets.infrastructure.sql_store import SqlSecretStore
from tiny_hermes.session_workspace.application.files import (
    WorkspaceFileNotFound,
    WorkspaceFiles,
    WorkspaceFilesUnavailable,
)
from tiny_hermes.session_workspace.infrastructure.sql_files import SqlWorkspaceFiles
from tiny_hermes.session_workspace.ports.objects import ObjectStore

logger = logging.getLogger(__name__)


class ChannelFileSender:
    """Reads the Session's latest saved snapshot, never the live sandbox: what
    is sent is what was checkpointed, the same rule `site.publish` keeps."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        objects: ObjectStore,
        client_for: Callable[[UUID], OutboundUpload],
        kek: bytes | None,
        *,
        max_bytes: int = MAX_SEND_BYTES,
    ) -> None:
        self._sessions = sessions
        self._objects = objects
        self._client_for = client_for
        self._kek = kek
        self._max_bytes = max_bytes

    async def send(
        self,
        *,
        workspace_id: UUID,
        session_id: UUID,
        run_id: UUID,
        path: str,
        delivery_key: str,
    ) -> FileSent:
        name = path.rsplit("/", 1)[-1]
        async with self._sessions() as db:
            files = WorkspaceFiles(SqlWorkspaceFiles(db), self._objects)
            try:
                source = await files.store.session_source(workspace_id, session_id)
                snapshot = await files.list_files(source)
            except (WorkspaceFileNotFound, WorkspaceFilesUnavailable) as error:
                raise FileSendRefused(
                    "unavailable", "the saved workspace cannot be read"
                ) from error
            entry = next((item for item in snapshot.items if item.path == path), None)
            if snapshot.revision_id is None or entry is None:
                raise FileSendRefused(
                    "not_found",
                    f"{path} is not in the saved workspace. A file written in this "
                    "same round is not saved yet: send it in its own round, after "
                    "the round that wrote it",
                )
            if entry.size > self._max_bytes:
                raise FileSendRefused(
                    "too_large",
                    f"{path} is {entry.size} bytes, over the {self._max_bytes} a sent "
                    "file may be; compress it or split it",
                )
            target = await SqlChannelStore(db).delivery_target_for(session_id)
            if (
                target is None
                or target.channel != "feishu"
                or not target.binding_active
                or target.app_id is None
                or target.app_secret_ref is None
            ):
                return FileSent(name=name, size=entry.size, delivered=False)
            secret = await CredentialResolver(SqlSecretStore(db), self._kek).resolve(
                target.app_secret_ref
            )
            try:
                _, stream = await files.content(source, snapshot.revision_id, path)
                body = bytearray()
                async for chunk in stream:
                    body.extend(chunk)
            except (WorkspaceFileNotFound, WorkspaceFilesUnavailable) as error:
                raise FileSendRefused("unavailable", f"{path} could not be read") from error

        client = self._client_for(target.workspace_id)
        try:
            file_key = await FeishuFileUploader(client).upload(
                app_id=target.app_id, app_secret=secret, name=name, body=bytes(body)
            )
            await FeishuSender(client).send_file(
                app_id=target.app_id,
                app_secret=secret,
                open_id=target.external_user_id,
                file_key=file_key,
                delivery_key=delivery_key,
            )
        except Exception as error:
            # Told to the model rather than raised: a round that failed here
            # would lose the work before it, and "Feishu did not take it" is
            # something the model can say to the person.
            logger.warning(
                "file not delivered",
                extra={"run_id": str(run_id), "error": type(error).__name__},
            )
            raise FileSendRefused(
                "not_delivered", f"Feishu did not accept the file ({type(error).__name__})"
            ) from error
        async with self._sessions.begin() as db:
            # Name and size: what left, not what it said.
            db.add(
                AuditEventRow(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    actor_type="run",
                    actor_id=run_id,
                    action="file.sent",
                    resource_type="session",
                    resource_id=session_id,
                    result="succeeded",
                    request_id=f"run:{run_id}"[:80],
                    context={"run_id": str(run_id), "path": path, "size": str(entry.size)},
                )
            )
        return FileSent(name=name, size=entry.size, delivered=True)


__all__ = ["ChannelFileSender"]

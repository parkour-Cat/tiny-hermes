from urllib.parse import quote
from uuid import UUID

from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from tiny_hermes.session_workspace.application.files import (
    WorkspaceFileNotFound,
    WorkspaceFiles,
    WorkspaceFilesChanged,
    WorkspaceFilesUnavailable,
)
from tiny_hermes.session_workspace.ports.files import FileSource
from tiny_hermes.shared.errors import AppError


class ConsoleFileResponse(BaseModel):
    path: str
    size_bytes: int
    sha256: str | None


class ConsoleFilesResponse(BaseModel):
    revision_id: UUID | None
    items: list[ConsoleFileResponse]


class EndUserFileResponse(BaseModel):
    path: str
    size_bytes: int


class EndUserFilesResponse(BaseModel):
    revision_id: UUID | None
    items: list[EndUserFileResponse]


def file_error(error: Exception) -> AppError:
    if isinstance(error, WorkspaceFileNotFound):
        return AppError(
            code="workspace_file_not_found",
            title="File not found",
            status=404,
            detail="This saved file is not available.",
        )
    if isinstance(error, WorkspaceFilesChanged):
        return AppError(
            code="workspace_files_changed",
            title="Files changed",
            status=409,
            detail="Saved files have changed. Refresh the list before downloading.",
        )
    return AppError(
        code="workspace_files_unavailable",
        title="Files unavailable",
        status=503,
        detail="Saved files could not be read. Please try again later.",
    )


FILE_ERRORS = (WorkspaceFileNotFound, WorkspaceFilesChanged, WorkspaceFilesUnavailable)


async def file_download(
    files: WorkspaceFiles,
    source: FileSource | None,
    revision_id: UUID,
    path: str,
) -> StreamingResponse:
    entry, stream = await files.content(source, revision_id, path)
    filename = quote(entry.path.rsplit("/", 1)[-1], safe="")
    return StreamingResponse(
        stream,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            "Content-Length": str(entry.size),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )

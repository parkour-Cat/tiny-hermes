"""What the catalog stores for an HTTP tool, apart from how it stores it.

Product design §16.1. Two records, and the split is the same one the skill
catalog makes: an `HttpTool` is a name and a place in a workspace, and an
`HttpToolVersion` is one immutable document.

**A binding names a version.** Product design's reason for skills applies
unchanged here and is worth restating: the document lives on somebody else's
server, and if a binding named a URL then their edit — a parameter becoming
required, an operation disappearing — would change what a published Agent does
without anyone here publishing anything.

**The credential is a reference, never a value.** It names an environment
variable or a Secret, exactly as a model endpoint does, and is resolved at the
moment of the call. Nothing in this module has ever held one.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from tiny_hermes.tools.domain.openapi import Operation

#: Same shape as a skill name and an Agent alias, and for the same reason: it
#: becomes part of a tool name a model types back, and a name needing quoting
#: is a name that will arrive misquoted.
NAME_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
NAME_MAX_LENGTH = 48


class HttpToolStatus(StrEnum):
    ACTIVE = "active"
    #: Stops new bindings. A published Agent that already binds a version keeps
    #: working — §15.1's rule for skills, applied here because it is the same
    #: rule about immutable versions rather than a coincidence.
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True)
class HttpTool:
    id: UUID
    workspace_id: UUID
    name: str
    #: Where the requests go. Registered by a person rather than read from the
    #: document's `servers`, because that field is the API author's opinion
    #: about which deployment you meant, and this is a decision the workspace
    #: makes and the outbound scope is checked against.
    base_url: str
    #: Names an environment variable or a Secret id; never a value.
    credential_ref: str | None
    current_version_id: UUID | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class HttpToolVersion:
    """One immutable document, without the document.

    `operations` is what the parse produced, which is what a binding is checked
    against and what the model is told about. The original body is kept too —
    an administrator comparing two versions needs the thing they registered,
    not this platform's reading of it.
    """

    id: UUID
    http_tool_id: UUID
    version_number: int
    content_hash: str
    title: str
    document_version: str
    operations: tuple[Operation, ...]
    status: HttpToolStatus
    created_by: UUID
    created_at: datetime

    @property
    def bindable(self) -> bool:
        return self.status is HttpToolStatus.ACTIVE

    def operation(self, operation_id: str) -> Operation | None:
        return next(
            (item for item in self.operations if item.operation_id == operation_id),
            None,
        )

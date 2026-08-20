"""What publishing needs to know about a bound HTTP tool version.

The same shape and the same reasoning as `skills.py`: a port narrow enough to
implement in a test in five lines, answering about visibility and existence with
the same nothing so a draft naming another workspace's version cannot learn that
it exists.

One field here has no counterpart there. `host` is what the version's tool will
actually be called at, and the publish check measures it against the Agent's own
`network.allow`. Binding a tool whose host the Agent may not reach would be a
version that lists a capability and refuses it on first use.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class HttpToolBindingView:
    """One HTTP tool version, as the publish check needs to see it."""

    http_tool_id: UUID
    version_id: UUID
    #: The tool's name, which is the middle segment of every call name this
    #: version generates. Carried so a refusal can say `orders` rather than a
    #: uuid.
    tool_name: str
    #: The host of the tool's registered base URL.
    host: str
    #: Every operation this version declares. The check is a subset test, so it
    #: needs the whole set rather than an "exists" question per id.
    operation_ids: tuple[str, ...]
    #: Which of those change something at the far end. Carried separately
    #: because §16.3's publish-time choice is only required where a bound
    #: operation could write, and the names alone do not say.
    write_operation_ids: tuple[str, ...]
    active: bool


class HttpToolBindingReader(Protocol):
    async def visible_versions(
        self, workspace_id: UUID, version_ids: Sequence[UUID]
    ) -> Sequence[HttpToolBindingView]:
        """The named versions this workspace may bind.

        Ids that do not exist and ids belonging elsewhere are both absent from
        the answer, and the caller may not tell them apart.
        """
        ...

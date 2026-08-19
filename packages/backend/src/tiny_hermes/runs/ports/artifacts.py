"""A Run's one way to open a file somebody passed it.

One method, and what it takes is an id rather than a path. §13's eighth clause
moves files between a parent and a child as **authorizations**: there is no
shared directory, so there is nothing to browse and no name to resolve. A port
that accepted a path would be a port somebody could point at a directory.

The answer distinguishes "you may not read this" from "there is nothing here",
because a model told only "no" will keep asking with different ids. Both come
back as sentences rather than exceptions, the same as every other tool answer.
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class ArtifactContent:
    """One file's bytes, as text, with what it is.

    `text` is `None` when the read was refused; `detail` then says why in
    words the model can act on.
    """

    filename: str = ""
    media_type: str = ""
    size_bytes: int = 0
    text: str | None = None
    detail: str = ""


class ArtifactReads(Protocol):
    async def read(self, *, run_id: UUID, artifact_id: str) -> ArtifactContent:
        """Open one Artifact this Run was granted, or say why not.

        The Run is named rather than its Agent or its Session: a grant is made
        to one piece of work, and asking on behalf of anything wider would let
        a later Run read what nobody passed it.
        """
        ...

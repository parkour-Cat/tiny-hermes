"""Where a Run gets skill text from, and nothing else about skills.

One method, because one is all a Run does with the catalog: it holds version
ids its Version fixed at publication, and it reads files out of them. Whether
that version may still be bound, who may see it, and what the scan said were
all answered before this Run existed.
"""

from typing import Protocol
from uuid import UUID


class SkillLibrary(Protocol):
    async def read_file(self, version_id: UUID, path: str) -> str | None:
        """One file's text, or ``None`` when that version has no such file.

        ``None`` rather than an exception: a model naming a file that is not in
        the package has made an ordinary mistake, and the answer it needs is a
        tool result saying so.
        """
        ...

"""What publishing needs to know about a bound skill version.

Much narrower than `SkillStore`, on purpose. The Agent Catalog decides whether a
draft may be published; it does not decide what a skill is, who may write one,
or which workspace can see it. A port small enough to implement in a test in
five lines is what keeps that division true a year from now.

The reader answers about visibility as well as existence, and deliberately
returns the same nothing for both: a draft naming a version in somebody else's
workspace must not learn that it exists. That is the same rule the catalog's own
404-rather-than-403 follows.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class SkillBindingView:
    """One version, as the publish check needs to see it.

    `active` and `blocked_by_scan` are separate rather than one `bindable`
    because an author who is told only "not bindable" has to guess which of the
    two they are looking at, and the two have different fixes.
    """

    skill_id: UUID
    version_id: UUID
    name: str
    #: The manifest summary. This is what §6 puts in the model's context, and
    #: what the summary budget is measured against here.
    description: str
    active: bool
    blocked_by_scan: bool


class SkillBindingReader(Protocol):
    async def visible_versions(
        self, workspace_id: UUID, version_ids: Sequence[UUID]
    ) -> Sequence[SkillBindingView]:
        """The named versions this workspace may bind — its own and platform.

        Ids that do not exist and ids belonging elsewhere are both absent from
        the answer, and the caller may not tell them apart.
        """
        ...

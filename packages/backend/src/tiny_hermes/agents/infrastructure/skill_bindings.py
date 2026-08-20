"""The Agent Catalog's view of the skill catalog.

One adapter, so the visibility rule lives in one place. `SkillCatalog._visible`
answers "may this workspace see this skill" for its own reads; this answers the
same question for a publish check, and answers it by asking the same store the
same way rather than by restating the rule.
"""

from collections.abc import Sequence
from uuid import UUID

from tiny_hermes.agents.ports.skills import SkillBindingView
from tiny_hermes.skills.domain.models import Skill, SkillScope, SkillVersionStatus
from tiny_hermes.skills.domain.scan import blocking
from tiny_hermes.skills.ports.store import SkillStore


class CatalogSkillBindings:
    def __init__(self, store: SkillStore) -> None:
        self._store = store

    async def visible_versions(
        self, workspace_id: UUID, version_ids: Sequence[UUID]
    ) -> Sequence[SkillBindingView]:
        views: list[SkillBindingView] = []
        for version_id in dict.fromkeys(version_ids):
            version = await self._store.get_version(version_id)
            if version is None:
                continue
            skill = await self._store.get_skill(version.skill_id)
            if skill is None or not _visible(skill, workspace_id):
                continue
            views.append(
                SkillBindingView(
                    skill_id=skill.id,
                    version_id=version.id,
                    name=skill.name,
                    description=version.manifest.description,
                    active=version.status is SkillVersionStatus.ACTIVE,
                    blocked_by_scan=bool(blocking(version.findings)),
                )
            )
        return views


def _visible(skill: Skill, workspace_id: UUID) -> bool:
    """A workspace sees its own skills and every platform skill."""
    return skill.scope is SkillScope.PLATFORM or skill.workspace_id == workspace_id

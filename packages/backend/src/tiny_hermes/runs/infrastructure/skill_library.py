"""Skill text for a running Run, read one file at a time.

Its own session per read, opened from the factory the Worker already holds. A
skill file is read in the middle of answering a tool call, nowhere near the
transaction that records the slice, and joining that transaction would mean a
read holding a write open across a model round.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiny_hermes.skills.infrastructure.tables import SkillFileRow


class SqlSkillLibrary:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def read_file(self, version_id: UUID, path: str) -> str | None:
        async with self._sessions() as session:
            return await session.scalar(
                select(SkillFileRow.content).where(
                    SkillFileRow.skill_version_id == version_id,
                    SkillFileRow.path == path,
                )
            )

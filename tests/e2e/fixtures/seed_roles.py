"""Provision non-admin identities for an explicitly opted-in isolated UX stack.

The product has no local-user registration API. Only fixture provisioning uses
the ORM; the role acceptance tests log in and verify public HTTP/UI behavior.
Existing identities are never changed or given a new password.
"""

import asyncio
import os

from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tiny_hermes.identity.infrastructure.tables import AuthIdentityRow, UserRow


async def main() -> None:
    if os.environ.get("TINY_HERMES_E2E_ROLE_FIXTURES") != "isolated-ux-only":
        raise SystemExit("Explicit isolated UX fixture opt-in is required")
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with async_sessionmaker(engine)() as session, session.begin():
            for role in ("workspace_admin", "developer", "viewer"):
                subject = f"ux-{role}@example.com"
                existing = await session.scalar(select(AuthIdentityRow).where(
                    AuthIdentityRow.provider == "local", AuthIdentityRow.subject == subject,
                ))
                if existing is not None:
                    continue
                user = UserRow(display_name=f"UX {role}", is_platform_admin=False)
                session.add(user)
                await session.flush()
                session.add(AuthIdentityRow(
                    user_id=user.id, provider="local", subject=subject,
                    password_hash=PasswordHash.recommended().hash("ux-role-check-only-123"),
                ))
        print("Isolated role identities ready; existing identities unchanged")
    finally:
        await engine.dispose()


asyncio.run(main())

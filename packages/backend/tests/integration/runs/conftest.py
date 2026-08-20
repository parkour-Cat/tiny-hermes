"""Fixtures the HTTP tool and approval suites share.

Here rather than in one of them because pytest binds a fixture to the module
that defines it, and two suites describing two halves of one path should be
looking at one stand-in and one boundary.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.egress.infrastructure.sql_directory import SqlScopeDirectory

from ..egress_support import ProxyHandle, running_proxy
from .http_tool_support import StandIn, serving


@pytest.fixture
async def api() -> AsyncIterator[tuple[StandIn, str]]:
    app = StandIn()
    async with serving(app) as url:
        yield app, url


@pytest.fixture
async def proxy(engine: AsyncEngine) -> AsyncIterator[ProxyHandle]:
    """The real boundary, reading the real layers.

    The SQL directory rather than the in-memory one, because what these suites
    claim is precisely that the chain — platform, workspace, and the Agent's
    own `network` out of its published version — is what decides. A directory
    the test filled in by hand would prove none of it.
    """
    async with running_proxy(
        directory=SqlScopeDirectory(async_sessionmaker(engine, expire_on_commit=False))
    ) as handle:
        yield handle

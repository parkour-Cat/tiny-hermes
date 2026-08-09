# tiny-hermes M1 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付 M1 第一阶段“可启动骨架”：平台可从空环境启动，首位管理员可安全初始化、登录、创建工作空间，并为管理写操作留下审计记录。

**Architecture:** 后端先建立一个 Python 发布单元，FastAPI 路由只调用 application 用例，身份与租户规则不依赖 Web 框架；SQLAlchemy/Alembic 负责 PostgreSQL 持久化。React Web 只消费真实 API，Compose 先运行 PostgreSQL、Redis、MinIO、API 与 Web，Worker、Scheduler 和 Sandbox Controller 在后续阶段接入。

**Tech Stack:** Python 3.12、uv、FastAPI、Pydantic Settings、SQLAlchemy 2、Alembic、PostgreSQL、pwdlib/Argon2、React、TypeScript、Vite、Ant Design、TanStack Query、Vitest、Playwright、Docker Compose

---

## 1. 开始前的固定条件

- 当前目录还不是 Git 仓库。执行任务前先运行 `git rev-parse --is-inside-work-tree`；如果不是 `true`，停止并取得用户对初始化仓库或连接已有仓库的明确授权。本文不会把“写提交”当成初始化 Git 的授权。
- 本机已检测到 Python 3.12.6 与 uv 0.11.26，符合后端基线。
- 本机已检测到 Node 22.12.0，但设计要求 Node 24 LTS。执行前必须先激活 Node 24；`node --version` 不是 `v24.*` 时不生成前端锁文件。

## 2. 文件结构与职责

```text
tiny-hermes/
├─ .github/workflows/ci.yml                 # 后端、前端、迁移检查
├─ apps/
│  ├─ api/Dockerfile                        # API 生产镜像
│  └─ web/
│     ├─ src/api/client.ts                  # 统一浏览器 API 访问
│     ├─ src/auth/AuthProvider.tsx           # 当前登录状态
│     ├─ src/pages/BootstrapPage.tsx         # 首次初始化
│     ├─ src/pages/LoginPage.tsx             # 本地登录
│     ├─ src/pages/WorkspacesPage.tsx        # 工作空间列表与创建
│     ├─ src/App.tsx                         # 页面路由
│     └─ ...                                 # Vite、测试和样式配置
├─ deploy/compose/
│  ├─ compose.yaml                           # 本地完整启动
│  └─ postgres/init/01-create-test-db.sql    # 独立测试库
├─ packages/backend/src/tiny_hermes/
│  ├─ api/app.py                             # FastAPI 组装
│  ├─ api/health.py                          # 存活与就绪路由
│  ├─ identity/application/auth_service.py   # Bootstrap 与登录用例
│  ├─ identity/domain/models.py              # 调用主体与角色值
│  ├─ identity/infrastructure/tables.py      # 身份表映射
│  ├─ identity/presentation/routes.py        # 身份 HTTP 路由
│  ├─ tenancy/application/workspace_service.py
│  ├─ tenancy/infrastructure/tables.py
│  ├─ tenancy/presentation/routes.py
│  ├─ audit/application/recorder.py          # 审计写入端口
│  ├─ audit/infrastructure/tables.py
│  └─ shared/{config,database,errors,logging}.py
├─ packages/backend/tests/
│  ├─ unit/                                  # 不启动数据库的规则测试
│  └─ integration/                           # 真实 PostgreSQL/API 测试
├─ alembic.ini
├─ migrations/                               # 只增不改的数据库迁移
├─ pyproject.toml
├─ uv.lock
├─ package.json
├─ pnpm-workspace.yaml
└─ pnpm-lock.yaml
```

阶段一不创建空的 `worker`、`scheduler` 或 `sandbox-controller` 假进程。后续阶段增加进程时复用本阶段的配置、数据库、日志和健康检查边界。

### Task 1: 固定工具链和仓库骨架

**Files:**
- Create: `.python-version`
- Create: `.nvmrc`
- Create: `.gitignore`
- Create: `pyproject.toml`
- Create: `package.json`
- Create: `pnpm-workspace.yaml`
- Create: `packages/backend/src/tiny_hermes/__init__.py`
- Create: `packages/backend/tests/unit/test_package.py`

- [ ] **Step 1: 验证执行环境和 Git 前置条件**

Run:

```powershell
python --version
uv --version
node --version
pnpm --version
git rev-parse --is-inside-work-tree
```

Expected: Python 为 `3.12.*`，uv 可运行，Node 为 `v24.*`，最后一行是 `true`。当前已知 Node 是 v22，因此执行者需先通过用户选定的 Node 版本管理方式切换到 24；不得用 Node 22 生成锁文件。

- [ ] **Step 2: 写入版本和忽略规则**

```text
# .python-version
3.12
```

```text
# .nvmrc
24
```

```gitignore
# .gitignore
.env
.env.*
!.env.example
.venv/
__pycache__/
.pytest_cache/
.ruff_cache/
.mypy_cache/
.pyright/
*.py[cod]
node_modules/
dist/
coverage/
playwright-report/
test-results/
.vite/
data/
*.log
```

- [ ] **Step 3: 写入 Python 发布单元配置**

```toml
# pyproject.toml
[project]
name = "tiny-hermes"
version = "0.0.0"
description = "A multi-tenant runtime for lightweight Hermes-style agents"
requires-python = ">=3.12,<3.13"
dependencies = []

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["packages/backend/src/tiny_hermes"]

[tool.pytest.ini_options]
testpaths = ["packages/backend/tests"]
addopts = "-ra"
asyncio_mode = "auto"

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "ASYNC", "S"]

[tool.pyright]
pythonVersion = "3.12"
typeCheckingMode = "strict"
include = ["packages/backend/src", "packages/backend/tests"]
```

Run:

```powershell
uv add fastapi pydantic-settings sqlalchemy asyncpg alembic "pwdlib[argon2]" structlog python-multipart
uv add --dev pytest pytest-asyncio httpx ruff pyright
```

Expected: `pyproject.toml` 的依赖列表被 uv 更新并生成 `uv.lock`；解析到正式发行版，不包含预发布版本。

- [ ] **Step 4: 写入前端工作区配置**

```json
// package.json
{
  "name": "tiny-hermes-workspace",
  "private": true,
  "packageManager": "pnpm@10.15.0",
  "scripts": {
    "web:dev": "pnpm --filter @tiny-hermes/web dev",
    "web:test": "pnpm --filter @tiny-hermes/web test",
    "web:build": "pnpm --filter @tiny-hermes/web build",
    "web:lint": "pnpm --filter @tiny-hermes/web lint"
  }
}
```

```yaml
# pnpm-workspace.yaml
packages:
  - apps/web
```

Run: `corepack prepare pnpm@10.15.0 --activate`

Expected: `pnpm --version` 输出 `10.15.0`。如果该版本无法从官方包源取得，不临时换号；先核对 pnpm 官方发布记录并通过文档变更更新版本。

- [ ] **Step 5: 先写包导入测试并确认失败**

```python
# packages/backend/tests/unit/test_package.py
def test_package_exposes_version() -> None:
    import tiny_hermes

    assert tiny_hermes.__version__ == "0.0.0"
```

Run: `uv run pytest packages/backend/tests/unit/test_package.py -v`

Expected: FAIL，提示 `tiny_hermes` 尚不可导入或没有 `__version__`。

- [ ] **Step 6: 写最小包入口并确认通过**

```python
# packages/backend/src/tiny_hermes/__init__.py
__version__ = "0.0.0"
```

Run: `uv run pytest packages/backend/tests/unit/test_package.py -v`

Expected: `1 passed`。

- [ ] **Step 7: 运行基础静态检查**

Run:

```powershell
uv run ruff check packages/backend
uv run pyright
```

Expected: 两条命令均退出 0。

- [ ] **Step 8: 提交仓库骨架**

```powershell
git add .python-version .nvmrc .gitignore pyproject.toml uv.lock package.json pnpm-workspace.yaml packages/backend
git commit -m "chore: establish project toolchain"
```

Expected: 产生一个只包含工具链和包导入测试的提交。

### Task 2: 配置加载、日志和健康检查

**Files:**
- Create: `packages/backend/src/tiny_hermes/shared/config.py`
- Create: `packages/backend/src/tiny_hermes/shared/logging.py`
- Create: `packages/backend/src/tiny_hermes/api/health.py`
- Create: `packages/backend/src/tiny_hermes/api/app.py`
- Create: `packages/backend/tests/unit/shared/test_config.py`
- Create: `packages/backend/tests/unit/api/test_health.py`
- Create: `.env.example`

- [ ] **Step 1: 写配置失败测试**

```python
# packages/backend/tests/unit/shared/test_config.py
import pytest
from pydantic import ValidationError

from tiny_hermes.shared.config import Settings


def test_settings_reject_placeholder_cookie_secret() -> None:
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql+asyncpg://app:app@db/app",
            redis_url="redis://redis:6379/0",
            s3_endpoint="http://minio:9000",
            s3_bucket="tiny-hermes",
            session_cookie_secret="change-me",
            bootstrap_token="bootstrap-token-with-at-least-32-characters",
        )
```

Run: `uv run pytest packages/backend/tests/unit/shared/test_config.py -v`

Expected: FAIL，提示 `tiny_hermes.shared.config` 不存在。

- [ ] **Step 2: 实现严格配置对象**

```python
# packages/backend/src/tiny_hermes/shared/config.py
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    database_url: str
    redis_url: str
    s3_endpoint: str
    s3_bucket: str
    session_cookie_secret: str = Field(min_length=32)
    bootstrap_token: str = Field(min_length=32)
    session_ttl_seconds: int = Field(default=28_800, ge=300, le=604_800)

    @field_validator("session_cookie_secret", "bootstrap_token")
    @classmethod
    def reject_example_secrets(cls, value: str) -> str:
        if value in {"change-me", "example", "secret"}:
            raise ValueError("example secrets are not valid runtime secrets")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]
```

```dotenv
# .env.example
ENVIRONMENT=development
DATABASE_URL=postgresql+asyncpg://tiny_hermes:local-only@localhost:5432/tiny_hermes
REDIS_URL=redis://localhost:6379/0
S3_ENDPOINT=http://localhost:9000
S3_BUCKET=tiny-hermes
SESSION_COOKIE_SECRET=replace-with-at-least-32-random-characters
BOOTSTRAP_TOKEN=replace-with-at-least-32-random-characters
```

Run: `uv run pytest packages/backend/tests/unit/shared/test_config.py -v`

Expected: PASS。

- [ ] **Step 3: 写健康检查失败测试**

```python
# packages/backend/tests/unit/api/test_health.py
from fastapi.testclient import TestClient

from tiny_hermes.api.app import create_app


def test_liveness_does_not_depend_on_external_services() -> None:
    response = TestClient(create_app(readiness=lambda: False)).get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_readiness_reports_dependency_failure() -> None:
    response = TestClient(create_app(readiness=lambda: False)).get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
```

Run: `uv run pytest packages/backend/tests/unit/api/test_health.py -v`

Expected: FAIL，提示 API 模块不存在。

- [ ] **Step 4: 实现应用工厂与健康路由**

```python
# packages/backend/src/tiny_hermes/api/health.py
from collections.abc import Callable

from fastapi import APIRouter, Response, status


def health_router(readiness: Callable[[], bool]) -> APIRouter:
    router = APIRouter(tags=["health"])

    @router.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "alive"}

    @router.get("/health/ready")
    def ready(response: Response) -> dict[str, str]:
        if not readiness():
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "not_ready"}
        return {"status": "ready"}

    return router
```

```python
# packages/backend/src/tiny_hermes/api/app.py
from collections.abc import Callable

from fastapi import FastAPI

from tiny_hermes.api.health import health_router


def create_app(readiness: Callable[[], bool] = lambda: True) -> FastAPI:
    app = FastAPI(title="tiny-hermes API", version="0.0.0")
    app.include_router(health_router(readiness))
    return app


app = create_app()
```

```python
# packages/backend/src/tiny_hermes/shared/logging.py
import logging

import structlog


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ]
    )
```

Run: `uv run pytest packages/backend/tests/unit/api/test_health.py -v`

Expected: `2 passed`。

- [ ] **Step 5: 增加 API 启动入口并验证 OpenAPI**

Run: `uv add uvicorn`

Add to `pyproject.toml`:

```toml
[project.scripts]
tiny-hermes-api = "tiny_hermes.api.cli:main"
```

Create `packages/backend/src/tiny_hermes/api/cli.py`:

```python
import uvicorn


def main() -> None:
    uvicorn.run("tiny_hermes.api.app:app", host="0.0.0.0", port=8000)
```

Run: `uv run tiny-hermes-api`

Expected: API 监听 `http://127.0.0.1:8000`；访问 `/openapi.json` 返回 200，并包含两个健康路由。验证后停止进程。

- [ ] **Step 6: 运行本任务检查并提交**

Run:

```powershell
uv run pytest packages/backend/tests/unit -v
uv run ruff check packages/backend
uv run pyright
git add .env.example pyproject.toml uv.lock packages/backend
git commit -m "feat: add configuration and health endpoints"
```

Expected: 测试和静态检查均通过，提交成功。

### Task 3: 建立 PostgreSQL 数据层和首份迁移

**Files:**
- Create: `packages/backend/src/tiny_hermes/shared/database.py`
- Create: `packages/backend/src/tiny_hermes/identity/infrastructure/tables.py`
- Create: `packages/backend/src/tiny_hermes/tenancy/infrastructure/tables.py`
- Create: `packages/backend/src/tiny_hermes/audit/infrastructure/tables.py`
- Create: `packages/backend/tests/integration/conftest.py`
- Create: `packages/backend/tests/integration/test_initial_migration.py`
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/versions/20260810_0001_identity_tenancy.py`

- [ ] **Step 1: 写迁移失败测试**

```python
# packages/backend/tests/integration/test_initial_migration.py
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine


async def test_initial_migration_creates_foundation_tables(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        tables = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))

    assert {
        "users",
        "auth_identities",
        "auth_sessions",
        "workspaces",
        "memberships",
        "audit_events",
        "alembic_version",
    } <= tables
```

```python
# packages/backend/tests/integration/conftest.py
import os

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


@pytest.fixture(scope="session")
def database_url() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://tiny_hermes:local-only@localhost:5432/tiny_hermes_test",
    )


@pytest.fixture(scope="session")
async def engine(database_url: str) -> AsyncEngine:
    value = create_async_engine(database_url)
    yield value
    await value.dispose()
```

Run: `$env:TEST_DATABASE_URL='postgresql+asyncpg://tiny_hermes:local-only@localhost:5432/tiny_hermes_test'; uv run pytest packages/backend/tests/integration/test_initial_migration.py -v`

Expected: FAIL，因为数据库或迁移尚不存在；如果端口未监听，错误必须明确是连接失败。

- [ ] **Step 2: 定义共享数据库基类和会话工厂**

```python
# packages/backend/src/tiny_hermes/shared/database.py
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import DateTime
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from tiny_hermes.shared.config import Settings


class Base(DeclarativeBase):
    pass


class IdMixin:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


def build_session_factory(settings: Settings) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    return async_sessionmaker(engine, expire_on_commit=False)


async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        yield session
```

- [ ] **Step 3: 定义身份、租户与审计表映射**

```python
# packages/backend/src/tiny_hermes/identity/infrastructure/tables.py
from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from tiny_hermes.shared.database import Base, CreatedAtMixin, IdMixin


class UserRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "users"
    status: Mapped[str] = mapped_column(String(32), default="active")
    display_name: Mapped[str] = mapped_column(String(120))
    is_platform_admin: Mapped[bool] = mapped_column(default=False)


class AuthIdentityRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "auth_identities"
    __table_args__ = (UniqueConstraint("provider", "subject"),)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(32))
    subject: Mapped[str] = mapped_column(String(320))
    password_hash: Mapped[str] = mapped_column(String(512))


class AuthSessionRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "auth_sessions"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_digest: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

```python
# packages/backend/src/tiny_hermes/tenancy/infrastructure/tables.py
from uuid import UUID

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from tiny_hermes.shared.database import Base, CreatedAtMixin, IdMixin


class WorkspaceRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "workspaces"
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(32), default="active")


class MembershipRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id"),)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(32))
```

```python
# packages/backend/src/tiny_hermes/audit/infrastructure/tables.py
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from tiny_hermes.shared.database import Base, CreatedAtMixin, IdMixin


class AuditEventRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "audit_events"
    workspace_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    actor_type: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[UUID | None] = mapped_column(nullable=True)
    action: Mapped[str] = mapped_column(String(120))
    resource_type: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[UUID | None] = mapped_column(nullable=True)
    result: Mapped[str] = mapped_column(String(32))
    request_id: Mapped[str] = mapped_column(String(80), index=True)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
```

- [ ] **Step 4: 配置 Alembic 并写明确的首份迁移**

```ini
# alembic.ini
[alembic]
script_location = migrations
prepend_sys_path = .
```

```python
# migrations/env.py
import asyncio
import os

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from tiny_hermes.audit.infrastructure import tables as audit_tables  # noqa: F401
from tiny_hermes.identity.infrastructure import tables as identity_tables  # noqa: F401
from tiny_hermes.shared.database import Base
from tiny_hermes.tenancy.infrastructure import tables as tenancy_tables  # noqa: F401

config = context.config
config.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])
target_metadata = Base.metadata


def run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with engine.connect() as connection:
        await connection.run_sync(run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline migrations are not supported")
asyncio.run(run_online())
```

The URL comes only from `DATABASE_URL`; neither file contains a credential.

Create `migrations/versions/20260810_0001_identity_tenancy.py` with:

```python
"""create identity, tenancy and audit tables"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260810_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("is_platform_admin", sa.Boolean(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "workspaces",
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "auth_identities",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("subject", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(512), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "subject"),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("csrf_digest", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest"),
    )
    op.create_table(
        "memberships",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "user_id"),
    )
    op.create_table(
        "audit_events",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(120), nullable=False),
        sa.Column("resource_type", sa.String(80), nullable=False),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("request_id", sa.String(80), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_workspace_id", "audit_events", ["workspace_id"])
    op.create_index("ix_audit_events_request_id", "audit_events", ["request_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_request_id", table_name="audit_events")
    op.drop_index("ix_audit_events_workspace_id", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_table("memberships")
    op.drop_table("auth_sessions")
    op.drop_table("auth_identities")
    op.drop_table("workspaces")
    op.drop_table("users")
```

- [ ] **Step 5: 对真实 PostgreSQL 执行迁移并确认测试通过**

Run:

```powershell
$env:DATABASE_URL='postgresql+asyncpg://tiny_hermes:local-only@localhost:5432/tiny_hermes_test'
uv run alembic upgrade head
uv run pytest packages/backend/tests/integration/test_initial_migration.py -v
uv run alembic downgrade base
uv run alembic upgrade head
```

Expected: 测试通过，降级与重新升级均退出 0。

- [ ] **Step 6: 检查模型和迁移没有漂移**

Run: `uv run alembic check`

Expected: 输出 `No new upgrade operations detected.`。

- [ ] **Step 7: 提交数据基础**

```powershell
git add alembic.ini migrations packages/backend/src/tiny_hermes/shared/database.py packages/backend/src/tiny_hermes/identity packages/backend/src/tiny_hermes/tenancy packages/backend/src/tiny_hermes/audit packages/backend/tests/integration
git commit -m "feat: add foundation database schema"
```

Expected: 提交成功，迁移文件与对应模型在同一提交中。

### Task 4: 实现一次性初始化和本地认证规则

**Files:**
- Create: `packages/backend/src/tiny_hermes/identity/domain/models.py`
- Create: `packages/backend/src/tiny_hermes/identity/ports/store.py`
- Create: `packages/backend/src/tiny_hermes/identity/application/auth_service.py`
- Create: `packages/backend/src/tiny_hermes/identity/infrastructure/sql_store.py`
- Create: `packages/backend/src/tiny_hermes/identity/infrastructure/memory_store.py`
- Create: `packages/backend/src/tiny_hermes/audit/application/recorder.py`
- Create: `packages/backend/tests/unit/identity/test_auth_service.py`
- Create: `packages/backend/tests/integration/identity/test_bootstrap_concurrency.py`

- [ ] **Step 1: 写初始化只能成功一次的规则测试**

```python
# packages/backend/tests/unit/identity/test_auth_service.py
from dataclasses import replace

import pytest

from tiny_hermes.identity.application.auth_service import AuthService, BootstrapClosed
from tiny_hermes.identity.domain.models import NewLocalUser
from tiny_hermes.identity.infrastructure.memory_store import MemoryAuthStore


@pytest.mark.asyncio
async def test_bootstrap_creates_first_platform_admin_and_then_closes() -> None:
    store = MemoryAuthStore()
    service = AuthService(store, bootstrap_token="a" * 32, session_ttl_seconds=28_800)
    command = NewLocalUser(subject="admin@example.com", display_name="Admin", password="long-pass-123")

    first = await service.bootstrap("a" * 32, command, request_id="req-1")

    assert first.is_platform_admin is True
    assert store.audit_actions == ["identity.bootstrap_succeeded"]
    with pytest.raises(BootstrapClosed):
        await service.bootstrap("a" * 32, replace(command, subject="other@example.com"), "req-2")
```

Run: `uv run pytest packages/backend/tests/unit/identity/test_auth_service.py -v`

Expected: FAIL，因为认证服务尚不存在。

- [ ] **Step 2: 定义不依赖数据库框架的身份类型和存储端口**

```python
# packages/backend/src/tiny_hermes/identity/domain/models.py
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class NewLocalUser:
    subject: str
    display_name: str
    password: str


@dataclass(frozen=True)
class AuthenticatedUser:
    id: UUID
    subject: str
    display_name: str
    status: str
    is_platform_admin: bool


@dataclass(frozen=True)
class StoredIdentity:
    user: AuthenticatedUser
    password_hash: str


@dataclass(frozen=True)
class StoredSession:
    user: AuthenticatedUser
    csrf_digest: str
    expires_at: datetime
    revoked_at: datetime | None
```

```python
# packages/backend/src/tiny_hermes/identity/ports/store.py
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from tiny_hermes.identity.domain.models import AuthenticatedUser, StoredIdentity, StoredSession


class AuthStore(Protocol):
    def bootstrap_lock(self) -> AbstractAsyncContextManager[None]: ...
    async def has_platform_admin(self) -> bool: ...
    async def create_platform_admin(
        self, subject: str, display_name: str, password_hash: str
    ) -> AuthenticatedUser: ...
    async def find_local_identity(self, subject: str) -> StoredIdentity | None: ...
    async def create_session(
        self, user_id: UUID, token_digest: str, csrf_digest: str, expires_at: datetime
    ) -> None: ...
    async def find_session(self, token_digest: str, now: datetime) -> StoredSession | None: ...
    async def revoke_session(self, token_digest: str, now: datetime) -> bool: ...
    async def append_audit(
        self,
        *,
        actor_id: UUID | None,
        action: str,
        result: str,
        request_id: str,
        context: dict[str, str],
    ) -> None: ...
```

`bootstrap_lock()` 的 PostgreSQL 实现必须在同一事务中执行固定键的 `pg_advisory_xact_lock`；内存实现使用 `asyncio.Lock`。这样两个并发初始化请求不能都通过“还没有管理员”的检查。

- [ ] **Step 3: 实现密码、令牌和认证用例**

```python
# packages/backend/src/tiny_hermes/identity/application/auth_service.py
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from pwdlib import PasswordHash

from tiny_hermes.identity.domain.models import AuthenticatedUser, NewLocalUser
from tiny_hermes.identity.ports.store import AuthStore


class BootstrapClosed(Exception):
    pass


class InvalidBootstrapToken(Exception):
    pass


class InvalidCredentials(Exception):
    pass


class AuthService:
    def __init__(self, store: AuthStore, bootstrap_token: str, session_ttl_seconds: int) -> None:
        self._store = store
        self._bootstrap_token = bootstrap_token
        self._session_ttl = timedelta(seconds=session_ttl_seconds)
        self._passwords = PasswordHash.recommended()

    async def bootstrap(
        self, presented_token: str, command: NewLocalUser, request_id: str
    ) -> AuthenticatedUser:
        if not hmac.compare_digest(presented_token, self._bootstrap_token):
            await self._store.append_audit(
                actor_id=None,
                action="identity.bootstrap_failed",
                result="denied",
                request_id=request_id,
                context={"reason": "invalid_token"},
            )
            raise InvalidBootstrapToken
        async with self._store.bootstrap_lock():
            if await self._store.has_platform_admin():
                raise BootstrapClosed
            user = await self._store.create_platform_admin(
                command.subject.strip().lower(),
                command.display_name.strip(),
                self._passwords.hash(command.password),
            )
            await self._store.append_audit(
                actor_id=user.id,
                action="identity.bootstrap_succeeded",
                result="succeeded",
                request_id=request_id,
                context={},
            )
            return user

    async def login(
        self, subject: str, password: str, request_id: str
    ) -> tuple[str, str, AuthenticatedUser]:
        identity = await self._store.find_local_identity(subject.strip().lower())
        if identity is None or not self._passwords.verify(password, identity.password_hash):
            await self._store.append_audit(
                actor_id=None,
                action="identity.login_failed",
                result="denied",
                request_id=request_id,
                context={},
            )
            raise InvalidCredentials
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        await self._store.create_session(
            identity.user.id,
            self.digest_token(token),
            self.digest_token(csrf_token),
            datetime.now(timezone.utc) + self._session_ttl,
        )
        await self._store.append_audit(
            actor_id=identity.user.id,
            action="identity.login_succeeded",
            result="succeeded",
            request_id=request_id,
            context={},
        )
        return token, csrf_token, identity.user

    @staticmethod
    def digest_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    async def authenticate(self, token: str) -> AuthenticatedUser:
        stored = await self._store.find_session(
            self.digest_token(token), datetime.now(timezone.utc)
        )
        if stored is None or stored.revoked_at is not None or stored.user.status != "active":
            raise InvalidCredentials
        return stored.user

    async def verify_csrf(self, token: str, csrf_token: str) -> AuthenticatedUser:
        stored = await self._store.find_session(
            self.digest_token(token), datetime.now(timezone.utc)
        )
        if stored is None or not hmac.compare_digest(
            stored.csrf_digest, self.digest_token(csrf_token)
        ):
            raise InvalidCredentials
        return stored.user

    async def logout(self, token: str, request_id: str) -> None:
        digest = self.digest_token(token)
        stored = await self._store.find_session(digest, datetime.now(timezone.utc))
        if stored is None:
            return
        await self._store.revoke_session(digest, datetime.now(timezone.utc))
        await self._store.append_audit(
            actor_id=stored.user.id,
            action="identity.logout_succeeded",
            result="succeeded",
            request_id=request_id,
            context={},
        )
```

Both methods compare only the SHA-256 digest with the database; the raw cookie token is never written to logs, AuditEvent or a table.

- [ ] **Step 4: 实现 PostgreSQL Store 与内存测试 Store**

Create `identity/infrastructure/sql_store.py` implementing every `AuthStore` method with the injected `AsyncSession`. `bootstrap_lock` executes:

```python
await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 847_263_991})
```

`has_platform_admin` checks `users.is_platform_admin=true`. `create_platform_admin` writes that field as `true`; ordinary Users keep the non-null default `false`. Do not encode a platform role as a fake Workspace membership.

Create `identity/infrastructure/memory_store.py` as a test-only-compatible in-process implementation with an `asyncio.Lock`, user/identity/session dictionaries and `audit_actions: list[str]`. Its behavior must match the Protocol, including session expiry and revocation.

- [ ] **Step 5: 确认规则测试通过**

Run: `uv run pytest packages/backend/tests/unit/identity/test_auth_service.py -v`

Expected: PASS；第二次初始化抛出 `BootstrapClosed`，存储中只有一个平台管理员。

- [ ] **Step 6: 写并发集成测试并先确认失败**

```python
# packages/backend/tests/integration/identity/test_bootstrap_concurrency.py
import asyncio

from tiny_hermes.identity.domain.models import NewLocalUser


async def test_only_one_concurrent_bootstrap_commits(auth_service_factory) -> None:
    command = NewLocalUser("admin@example.com", "Admin", "long-pass-123")
    results = await asyncio.gather(
        auth_service_factory().bootstrap("a" * 32, command, "req-a"),
        auth_service_factory().bootstrap("a" * 32, command, "req-b"),
        return_exceptions=True,
    )

    assert sum(not isinstance(value, Exception) for value in results) == 1
```

Run: `uv run pytest packages/backend/tests/integration/identity/test_bootstrap_concurrency.py -v`

Expected: 在 SQL Store fixture 接好之前 FAIL；接好后 PASS，而且数据库中只有一个 `is_platform_admin=true` 的 User。

- [ ] **Step 7: 运行身份规则和迁移检查**

Run:

```powershell
uv run pytest packages/backend/tests/unit/identity packages/backend/tests/integration/identity -v
uv run alembic check
uv run ruff check packages/backend
uv run pyright
```

Expected: 全部退出 0，日志和失败输出不包含密码、Bootstrap Token 或原始会话令牌。

- [ ] **Step 8: 提交身份核心**

```powershell
git add migrations packages/backend/src/tiny_hermes/identity packages/backend/src/tiny_hermes/audit packages/backend/tests
git commit -m "feat: add one-time bootstrap and local auth core"
```

Expected: 提交成功。

### Task 5: 暴露 Bootstrap、登录、当前用户和退出 API

**Files:**
- Create: `packages/backend/src/tiny_hermes/shared/errors.py`
- Create: `packages/backend/src/tiny_hermes/api/request_context.py`
- Create: `packages/backend/src/tiny_hermes/identity/presentation/routes.py`
- Modify: `packages/backend/src/tiny_hermes/api/app.py`
- Test: `packages/backend/tests/integration/identity/test_auth_api.py`

- [ ] **Step 1: 写完整认证 API 合约测试**

```python
# packages/backend/tests/integration/identity/test_auth_api.py
def test_bootstrap_login_me_and_logout(api_client) -> None:
    bootstrap = api_client.post(
        "/api/v1/bootstrap",
        headers={"X-Bootstrap-Token": "a" * 32},
        json={
            "subject": "admin@example.com",
            "display_name": "Admin",
            "password": "long-pass-123",
        },
    )
    assert bootstrap.status_code == 201
    assert bootstrap.json()["is_platform_admin"] is True

    closed = api_client.post(
        "/api/v1/bootstrap",
        headers={"X-Bootstrap-Token": "a" * 32},
        json={
            "subject": "second@example.com",
            "display_name": "Second",
            "password": "long-pass-456",
        },
    )
    assert closed.status_code == 409
    assert closed.json()["code"] == "bootstrap_closed"

    login = api_client.post(
        "/api/v1/auth/sessions",
        json={"subject": "admin@example.com", "password": "long-pass-123"},
    )
    assert login.status_code == 201
    assert login.cookies.get("tiny_hermes_session")
    assert login.cookies.get("tiny_hermes_csrf")
    csrf = login.cookies["tiny_hermes_csrf"]

    me = api_client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["subject"] == "admin@example.com"

    logout = api_client.delete(
        "/api/v1/auth/sessions/current", headers={"X-CSRF-Token": csrf}
    )
    assert logout.status_code == 204
    assert api_client.get("/api/v1/auth/me").status_code == 401
```

Run: `uv run pytest packages/backend/tests/integration/identity/test_auth_api.py -v`

Expected: FAIL，返回 404。

- [ ] **Step 2: 定义统一错误和请求 ID**

```python
# packages/backend/src/tiny_hermes/shared/errors.py
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AppError(Exception):
    code: str
    title: str
    status: int
    detail: str
    context: dict[str, Any] = field(default_factory=dict)
```

```python
# packages/backend/src/tiny_hermes/api/request_context.py
from uuid import uuid4

from fastapi import Request


def request_id(request: Request) -> str:
    value = request.headers.get("X-Request-Id")
    return value if value and len(value) <= 80 else f"req_{uuid4().hex}"
```

Modify `create_app` to register one `AppError` handler returning `application/problem+json` with `type`, `code`, `title`, `status`, `detail`, `request_id` and `context`. Add an HTTP middleware that sets the same request ID on `request.state.request_id` and the `X-Request-Id` response header.

- [ ] **Step 3: 实现认证请求/响应和 Cookie 边界**

`identity/presentation/routes.py` must contain these exact public schemas and routes:

```python
class BootstrapRequest(BaseModel):
    subject: EmailStr
    display_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=12, max_length=256)


class LoginRequest(BaseModel):
    subject: EmailStr
    password: str = Field(min_length=1, max_length=256)


class UserResponse(BaseModel):
    id: UUID
    subject: str
    display_name: str
    status: str
    is_platform_admin: bool
```

Route behavior:

```text
POST   /api/v1/bootstrap                 -> 201 UserResponse
POST   /api/v1/auth/sessions             -> 201 UserResponse + HttpOnly cookie
GET    /api/v1/auth/me                   -> 200 UserResponse or 401
DELETE /api/v1/auth/sessions/current     -> 204 and expired cookie
```

The session cookie is named `tiny_hermes_session`, uses `HttpOnly`, `SameSite=Lax`, path `/`, `Max-Age=settings.session_ttl_seconds`, and `Secure=true` outside development. Login also returns a readable `tiny_hermes_csrf` cookie whose random value is stored only as `csrf_digest`. Every cookie-authenticated `POST`, `PATCH` and `DELETE` other than login requires the same raw value in `X-CSRF-Token` and verifies its digest against the current AuthSession; missing or mismatched values return 403 `csrf_failed`. Logout expires both cookies. Bootstrap reads `X-Bootstrap-Token`; it never accepts the token in query strings or JSON. Add `email-validator` through `uv add email-validator` for `EmailStr`.

- [ ] **Step 4: 组装依赖而不使用模块级数据库会话**

Modify `create_app` to create one session factory at startup and one `AsyncSession` per request. Build `SqlAuthStore` and `AuthService` from that request session. Commit on success and roll back on exceptions. Override this dependency in tests; do not monkey-patch global settings or keep an `AsyncSession` on `app.state`.

- [ ] **Step 5: 运行合约测试并核对安全属性**

Run:

```powershell
uv run pytest packages/backend/tests/integration/identity/test_auth_api.py -v
uv run pytest packages/backend/tests/unit/identity -v
```

Expected: 全部通过。额外检查 `auth_sessions.token_digest` 是 64 位十六进制摘要，数据库中找不到响应 Cookie 原文。

- [ ] **Step 6: 运行静态检查并提交**

```powershell
uv run ruff check packages/backend
uv run pyright
git add pyproject.toml uv.lock packages/backend
git commit -m "feat: expose bootstrap and browser authentication"
```

Expected: 全部检查和提交成功。

### Task 6: 实现 Workspace、Membership 和工作空间边界

**Files:**
- Create: `packages/backend/src/tiny_hermes/tenancy/domain/models.py`
- Create: `packages/backend/src/tiny_hermes/tenancy/ports/store.py`
- Create: `packages/backend/src/tiny_hermes/tenancy/application/workspace_service.py`
- Create: `packages/backend/src/tiny_hermes/tenancy/infrastructure/sql_store.py`
- Create: `packages/backend/src/tiny_hermes/tenancy/presentation/routes.py`
- Modify: `packages/backend/src/tiny_hermes/api/app.py`
- Test: `packages/backend/tests/unit/tenancy/test_workspace_service.py`
- Test: `packages/backend/tests/integration/tenancy/test_workspace_api.py`

- [ ] **Step 1: 写角色规则失败测试**

```python
# packages/backend/tests/unit/tenancy/test_workspace_service.py
import pytest

from tiny_hermes.tenancy.application.workspace_service import Forbidden, WorkspaceService
from tiny_hermes.tenancy.domain.models import Actor, Role
from tiny_hermes.tenancy.infrastructure.memory_store import MemoryWorkspaceStore


async def test_only_platform_admin_can_create_workspace() -> None:
    service = WorkspaceService(MemoryWorkspaceStore())
    ordinary = Actor.new(is_platform_admin=False)

    with pytest.raises(Forbidden):
        await service.create_workspace(ordinary, "Acme", "req-1")


async def test_creator_becomes_workspace_admin() -> None:
    store = MemoryWorkspaceStore()
    service = WorkspaceService(store)
    admin = Actor.new(is_platform_admin=True)

    workspace = await service.create_workspace(admin, "Acme", "req-2")

    assert store.role_for(workspace.id, admin.id) is Role.WORKSPACE_ADMIN
    assert store.audit_actions == ["workspace.created"]
```

Run: `uv run pytest packages/backend/tests/unit/tenancy/test_workspace_service.py -v`

Expected: FAIL，租户模块尚不存在。

- [ ] **Step 2: 定义固定角色和服务端授权入口**

```python
# packages/backend/src/tiny_hermes/tenancy/domain/models.py
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID, uuid4


class Role(StrEnum):
    WORKSPACE_ADMIN = "workspace_admin"
    DEVELOPER = "developer"
    VIEWER = "viewer"


@dataclass(frozen=True)
class Actor:
    id: UUID
    is_platform_admin: bool

    @classmethod
    def new(cls, is_platform_admin: bool) -> "Actor":
        return cls(id=uuid4(), is_platform_admin=is_platform_admin)


@dataclass(frozen=True)
class Workspace:
    id: UUID
    name: str
    status: str
```

`WorkspaceStore` Protocol must expose these methods with the exact names used by `WorkspaceService`: `create_workspace`, `add_membership`, `list_visible_workspaces`, `get_membership`, `list_members`, and `append_audit`. All methods receive an already-normalized UUID; SQL queries include `workspace_id` in the predicate rather than fetching globally and filtering afterward.

- [ ] **Step 3: 实现创建、列表和成员查看用例**

`WorkspaceService` must enforce:

```python
async def create_workspace(self, actor: Actor, name: str, request_id: str) -> Workspace:
    if not actor.is_platform_admin:
        raise Forbidden
    normalized = name.strip()
    if not normalized or len(normalized) > 120:
        raise InvalidWorkspaceName
    workspace = await self._store.create_workspace(normalized)
    await self._store.add_membership(workspace.id, actor.id, Role.WORKSPACE_ADMIN)
    await self._store.append_audit(
        workspace_id=workspace.id,
        actor_id=actor.id,
        action="workspace.created",
        resource_id=workspace.id,
        request_id=request_id,
    )
    return workspace
```

`list_workspaces(actor)` returns every Workspace for a platform administrator and only joined Workspaces for other Users. `list_members(actor, selected_workspace_id)` permits platform administrators, workspace administrators and viewers; viewer access is read-only. The selected ID must come from `X-Workspace-Id` and pass membership validation even when the User has only one membership.

- [ ] **Step 4: 写跨空间 API 失败测试**

```python
# packages/backend/tests/integration/tenancy/test_workspace_api.py
def test_workspace_header_is_required_and_cross_tenant_lookup_is_denied(admin_client) -> None:
    first = admin_client.post("/api/v1/workspaces", json={"name": "A"}).json()
    second = admin_client.post("/api/v1/workspaces", json={"name": "B"}).json()

    missing = admin_client.get(f"/api/v1/workspaces/{first['id']}/members")
    assert missing.status_code == 400
    assert missing.json()["code"] == "workspace_required"

    mismatch = admin_client.get(
        f"/api/v1/workspaces/{first['id']}/members",
        headers={"X-Workspace-Id": second["id"]},
    )
    assert mismatch.status_code == 403
    assert mismatch.json()["code"] == "workspace_scope_mismatch"
    assert first["name"] not in str(mismatch.json())


def test_cookie_authenticated_write_requires_csrf(admin_client_without_csrf_header) -> None:
    response = admin_client_without_csrf_header.post(
        "/api/v1/workspaces", json={"name": "Denied"}
    )
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"
```

Run: `uv run pytest packages/backend/tests/integration/tenancy/test_workspace_api.py -v`

Expected: FAIL，路由尚不存在。

- [ ] **Step 5: 实现 SQL Store 和三个 HTTP 路由**

Implement:

```text
GET  /api/v1/workspaces                         -> 可见 Workspace 列表
POST /api/v1/workspaces                         -> 201，新 Workspace
GET  /api/v1/workspaces/{workspace_id}/members  -> 成员列表
```

`POST` requires a platform administrator. The member route requires `X-Workspace-Id`, checks that header UUID equals the path UUID, then validates membership. Platform administrator cross-workspace reads remain allowed only when header and path match and must append `workspace.members_read_by_platform_admin` AuditEvent.

The SQL Store and audit insert participate in the same request transaction. A failed membership or audit write rolls back Workspace creation; no successful management response may be returned before the transaction commits.

- [ ] **Step 6: 确认租户和审计测试通过**

Run:

```powershell
uv run pytest packages/backend/tests/unit/tenancy -v
uv run pytest packages/backend/tests/integration/tenancy -v
```

Expected: 全部通过。创建两个 Workspace 后有两条 `workspace.created` AuditEvent；错误响应不泄露另一 Workspace 的名称或字段。

- [ ] **Step 7: 运行阶段内回归并提交**

```powershell
uv run pytest packages/backend/tests -v
uv run alembic check
uv run ruff check packages/backend
uv run pyright
git add packages/backend
git commit -m "feat: add workspace tenancy boundary"
```

Expected: 全部退出 0，提交成功。

### Task 7: 用 Compose 启动真实依赖、迁移和 API

**Files:**
- Create: `deploy/compose/compose.yaml`
- Create: `deploy/compose/postgres/init/01-create-test-db.sql`
- Create: `apps/api/Dockerfile`
- Modify: `packages/backend/src/tiny_hermes/api/health.py`
- Modify: `packages/backend/src/tiny_hermes/api/app.py`
- Test: `packages/backend/tests/integration/api/test_readiness.py`

- [ ] **Step 1: 核对镜像标签确实存在**

Run:

```powershell
docker manifest inspect postgres:17.6-alpine
docker manifest inspect redis:8.2.1-alpine
docker manifest inspect minio/minio:RELEASE.2025-07-23T15-54-02Z
docker manifest inspect python:3.12.11-slim
docker manifest inspect ghcr.io/astral-sh/uv:0.11.26
```

Expected: 五条命令都返回包含 `schemaVersion` 的镜像清单。任何标签不存在时，先查对应项目官方发布记录，修订本计划中的具体版本后再写 Compose；不得换成 `latest`。

- [ ] **Step 2: 写独立测试库初始化脚本**

```sql
-- deploy/compose/postgres/init/01-create-test-db.sql
CREATE DATABASE tiny_hermes_test OWNER tiny_hermes;
```

- [ ] **Step 3: 写 API 多阶段镜像**

```dockerfile
# apps/api/Dockerfile
FROM ghcr.io/astral-sh/uv:0.11.26 AS uv
FROM python:3.12.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

RUN groupadd --system --gid 10001 tiny-hermes \
    && useradd --system --uid 10001 --gid tiny-hermes --create-home tiny-hermes

COPY --from=uv /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages/backend/src ./packages/backend/src
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
RUN uv sync --frozen --no-dev

USER 10001:10001
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "tiny-hermes-api"]
```

- [ ] **Step 4: 写 Compose 服务和健康探针**

`deploy/compose/compose.yaml` must define:

```yaml
name: tiny-hermes
services:
  postgres:
    image: postgres:17.6-alpine
    environment:
      POSTGRES_DB: tiny_hermes
      POSTGRES_USER: tiny_hermes
      POSTGRES_PASSWORD: local-only
    volumes:
      - postgres-data:/var/lib/postgresql/data
      - ./postgres/init:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U tiny_hermes -d tiny_hermes"]
      interval: 5s
      timeout: 3s
      retries: 20
    ports: ["5432:5432"]

  redis:
    image: redis:8.2.1-alpine
    command: ["redis-server", "--save", "", "--appendonly", "no"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 20
    ports: ["6379:6379"]

  minio:
    image: minio/minio:RELEASE.2025-07-23T15-54-02Z
    command: ["server", "/data", "--console-address", ":9001"]
    environment:
      MINIO_ROOT_USER: tiny-hermes-local
      MINIO_ROOT_PASSWORD: tiny-hermes-local-password
    volumes: ["minio-data:/data"]
    healthcheck:
      test: ["CMD", "mc", "ready", "local"]
      interval: 5s
      timeout: 3s
      retries: 20
    ports: ["9000:9000", "9001:9001"]

  migrate:
    build:
      context: ../..
      dockerfile: apps/api/Dockerfile
    command: ["uv", "run", "--no-sync", "alembic", "upgrade", "head"]
    environment: &app-env
      DATABASE_URL: postgresql+asyncpg://tiny_hermes:local-only@postgres:5432/tiny_hermes
      REDIS_URL: redis://redis:6379/0
      S3_ENDPOINT: http://minio:9000
      S3_BUCKET: tiny-hermes
      SESSION_COOKIE_SECRET: local-cookie-secret-with-32-characters
      BOOTSTRAP_TOKEN: local-bootstrap-token-with-32-characters
    depends_on:
      postgres: {condition: service_healthy}

  api:
    build:
      context: ../..
      dockerfile: apps/api/Dockerfile
    environment: *app-env
    depends_on:
      migrate: {condition: service_completed_successfully}
      redis: {condition: service_healthy}
      minio: {condition: service_healthy}
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/ready')"]
      interval: 5s
      timeout: 3s
      retries: 20
    ports: ["8000:8000"]

volumes:
  postgres-data:
  minio-data:
```

The values above are local-only examples and are not used by production manifests. `.env.example` explains that production must provide random values and use secret file mounting once KEK is introduced.

- [ ] **Step 5: 写就绪检查失败测试**

```python
# packages/backend/tests/integration/api/test_readiness.py
def test_readiness_is_503_when_database_probe_fails(api_client_with_failed_database) -> None:
    response = api_client_with_failed_database.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "checks": {"database": "failed"}}


def test_readiness_is_503_when_schema_is_behind(api_client_with_old_schema) -> None:
    response = api_client_with_old_schema.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["checks"]["migration"] == "behind"
```

Run: `uv run pytest packages/backend/tests/integration/api/test_readiness.py -v`

Expected: FAIL；当前 readiness 只接收布尔值，尚未检查数据库和 migration。

- [ ] **Step 6: 实现数据库与迁移版本就绪探针**

Replace the boolean readiness callback with an async `ReadinessProbe` that:

1. runs `SELECT 1` through a fresh SQLAlchemy connection;
2. reads `alembic_version.version_num`;
3. compares it with the script directory head returned by Alembic;
4. returns named check results without exposing connection URLs or exceptions.

`/health/live` remains independent from PostgreSQL. `/health/ready` returns 200 only when both `database=ok` and `migration=current`; otherwise it returns 503 with the two named results.

- [ ] **Step 7: 启动全新 Compose 并验证迁移顺序**

Run:

```powershell
docker compose -f deploy/compose/compose.yaml config
docker compose -f deploy/compose/compose.yaml up -d --build
docker compose -f deploy/compose/compose.yaml ps
Invoke-RestMethod http://localhost:8000/health/live
Invoke-RestMethod http://localhost:8000/health/ready
```

Expected: `migrate` 以退出码 0 完成，PostgreSQL、Redis、MinIO、API 均 healthy；两个 HTTP 响应分别为 `alive` 和 `ready`。

- [ ] **Step 8: 运行集成测试并提交**

```powershell
$env:TEST_DATABASE_URL='postgresql+asyncpg://tiny_hermes:local-only@localhost:5432/tiny_hermes_test'
uv run pytest packages/backend/tests/integration -v
git add apps/api deploy/compose packages/backend
git commit -m "feat: add compose runtime and dependency readiness"
```

Expected: 全部测试和提交成功。Compose 保持运行供下一任务使用。

### Task 8: 创建最小初始化、登录和 Workspace 页面

**Files:**
- Create: `apps/web/package.json`
- Create: `apps/web/vite.config.ts`
- Create: `apps/web/src/api/client.ts`
- Create: `apps/web/src/auth/AuthProvider.tsx`
- Create: `apps/web/src/pages/BootstrapPage.tsx`
- Create: `apps/web/src/pages/LoginPage.tsx`
- Create: `apps/web/src/pages/WorkspacesPage.tsx`
- Create: `apps/web/src/i18n/zh-CN.ts`
- Create: `apps/web/src/i18n/en-US.ts`
- Create: `apps/web/src/test/setup.ts`
- Create: `apps/web/src/App.tsx`
- Create: `apps/web/src/main.tsx`
- Create: `apps/web/src/App.test.tsx`
- Create: `apps/web/index.html`
- Create: `apps/web/tsconfig.json`

- [ ] **Step 1: 初始化前端包并安装明确依赖**

Create `apps/web/package.json`:

```json
{
  "name": "@tiny-hermes/web",
  "private": true,
  "version": "0.0.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build",
    "test": "vitest run",
    "lint": "eslint . --max-warnings 0"
  }
}
```

Run:

```powershell
pnpm --filter @tiny-hermes/web add react react-dom react-router-dom @tanstack/react-query antd
pnpm --filter @tiny-hermes/web add -D typescript vite @vitejs/plugin-react vitest jsdom @testing-library/react @testing-library/jest-dom @testing-library/user-event eslint @eslint/js typescript-eslint @types/react @types/react-dom
pnpm install --lockfile-only
```

Expected: `pnpm-lock.yaml` 生成，所有解析版本为正式发行版。

- [ ] **Step 2: 先写真实路由流程测试**

```tsx
// apps/web/src/App.test.tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "./App";

afterEach(() => vi.restoreAllMocks());

test("logs in and creates a workspace through the API", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch");
  fetchMock
    .mockResolvedValueOnce(new Response(JSON.stringify({ code: "unauthenticated" }), { status: 401 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ id: "u1", subject: "admin@example.com", display_name: "Admin", status: "active", is_platform_admin: true }), { status: 201 }))
    .mockResolvedValueOnce(new Response(JSON.stringify([]), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ id: "w1", name: "Acme", status: "active" }), { status: 201 }));

  render(<App />);
  await userEvent.type(await screen.findByLabelText("邮箱"), "admin@example.com");
  await userEvent.type(screen.getByLabelText("密码"), "long-pass-123");
  await userEvent.click(screen.getByRole("button", { name: "登录" }));
  await userEvent.click(await screen.findByRole("button", { name: "新建工作空间" }));
  await userEvent.type(screen.getByLabelText("名称"), "Acme");
  await userEvent.click(screen.getByRole("button", { name: "创建" }));

  expect(await screen.findByText("Acme")).toBeInTheDocument();
  expect(fetchMock).toHaveBeenLastCalledWith("/api/v1/workspaces", expect.objectContaining({ method: "POST" }));
});
```

Run: `pnpm web:test`

Expected: FAIL，因为 `App` 和测试环境配置尚不存在。

- [ ] **Step 3: 实现只访问真实 API 的客户端**

```ts
// apps/web/src/api/client.ts
export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const csrf = document.cookie
    .split("; ")
    .find((part) => part.startsWith("tiny_hermes_csrf="))
    ?.split("=")[1];
  const method = (init.method ?? "GET").toUpperCase();
  const csrfHeader = ["POST", "PATCH", "DELETE"].includes(method) && csrf
    ? { "X-CSRF-Token": decodeURIComponent(csrf) }
    : {};
  const response = await fetch(path, {
    ...init,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...csrfHeader, ...init.headers },
  });
  if (!response.ok) {
    const body = await response.json();
    throw new ApiError(response.status, body.code ?? "request_failed", body.detail ?? "请求失败");
  }
  return response.status === 204 ? (undefined as T) : response.json();
}
```

No page may provide a fallback fake User or Workspace after a request fails. A network failure stays visible and offers retry.

- [ ] **Step 4: 实现三个页面和登录状态容器**

`AuthProvider` calls `GET /api/v1/auth/me` on load and exposes `user`, `loading`, `login` and `logout`. `LoginPage` posts to `/api/v1/auth/sessions`; `BootstrapPage` posts the token only through `X-Bootstrap-Token`; `WorkspacesPage` lists and creates through `/api/v1/workspaces`. Use Ant Design form validation, but keep the server response authoritative.

`App.tsx` routes unauthenticated users to login, provides a visible link to initialization, and routes authenticated users to `/workspaces`. Keep all Chinese strings in `src/i18n/zh-CN.ts` and matching English strings in `src/i18n/en-US.ts`; components reference translation keys rather than inline production text.

- [ ] **Step 5: 配置 Vite 代理和测试环境**

`vite.config.ts` must use React, `jsdom` for Vitest, load `@testing-library/jest-dom/vitest` from `src/test/setup.ts`, and proxy `/api` plus `/health` to `http://localhost:8000` during development. `tsconfig.json` enables `strict`, `noUncheckedIndexedAccess` and `exactOptionalPropertyTypes`.

Run: `pnpm web:test`

Expected: PASS。

- [ ] **Step 6: 构建并人工验证最小链路**

Run:

```powershell
pnpm web:lint
pnpm web:test
pnpm web:build
pnpm web:dev
```

Expected: 前三条退出 0；浏览器访问 Vite 地址后能使用真实 API 登录、创建两个 Workspace，刷新后会话仍有效。验证后停止开发服务器。

- [ ] **Step 7: 提交最小控制台**

```powershell
git add package.json pnpm-workspace.yaml pnpm-lock.yaml apps/web
git commit -m "feat: add bootstrap login and workspace console"
```

Expected: 提交成功，仓库中不包含浏览器状态或本地凭据。

### Task 9: 加入 Web 容器、端到端验收和 CI

**Files:**
- Create: `apps/web/Dockerfile`
- Create: `apps/web/nginx.conf`
- Modify: `deploy/compose/compose.yaml`
- Create: `tests/e2e/playwright.config.ts`
- Create: `tests/e2e/foundation.spec.ts`
- Create: `.github/workflows/ci.yml`
- Create: `docs/development.md`

- [ ] **Step 1: 核对 Web 构建镜像并安装 Playwright**

Run:

```powershell
docker manifest inspect node:24.6.0-alpine
docker manifest inspect nginx:1.29.1-alpine
pnpm add -Dw @playwright/test
pnpm exec playwright install chromium
```

Expected: 两个镜像标签存在，Chromium 安装成功。CI 后面使用 Playwright 官方安装命令，不把浏览器二进制提交进仓库。

- [ ] **Step 2: 写失败的端到端场景**

```ts
// tests/e2e/foundation.spec.ts
import { expect, test } from "@playwright/test";

test("bootstrap, login, create two workspaces and logout", async ({ page, request }) => {
  const bootstrap = await request.post("/api/v1/bootstrap", {
    headers: { "X-Bootstrap-Token": "local-bootstrap-token-with-32-characters" },
    data: { subject: "admin@example.com", display_name: "Admin", password: "long-pass-123" },
  });
  expect(bootstrap.status()).toBe(201);

  await page.goto("/login");
  await page.getByLabel("邮箱").fill("admin@example.com");
  await page.getByLabel("密码").fill("long-pass-123");
  await page.getByRole("button", { name: "登录" }).click();
  await page.getByRole("button", { name: "新建工作空间" }).click();
  await page.getByLabel("名称").fill(`Workspace-${Date.now()}-A`);
  await page.getByRole("button", { name: "创建" }).click();
  await page.getByRole("button", { name: "新建工作空间" }).click();
  await page.getByLabel("名称").fill(`Workspace-${Date.now()}-B`);
  await page.getByRole("button", { name: "创建" }).click();

  await expect(page.getByRole("listitem")).toHaveCount(2);
  await page.getByRole("button", { name: "退出" }).click();
  await expect(page).toHaveURL(/\/login$/);
});
```

```ts
// tests/e2e/playwright.config.ts
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: ".",
  use: { baseURL: "http://127.0.0.1:3000", trace: "retain-on-failure" },
  retries: 0,
  workers: 1,
});
```

Run: `pnpm exec playwright test --config tests/e2e/playwright.config.ts`

Expected: FAIL，因为 Web 尚未加入 Compose 的 3000 端口。

- [ ] **Step 3: 构建静态 Web 镜像并反向代理 API**

```dockerfile
# apps/web/Dockerfile
FROM node:24.6.0-alpine AS build
WORKDIR /app
RUN corepack enable && corepack prepare pnpm@10.15.0 --activate
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/web/package.json ./apps/web/package.json
RUN pnpm install --frozen-lockfile
COPY apps/web ./apps/web
RUN pnpm --filter @tiny-hermes/web build

FROM nginx:1.29.1-alpine
COPY apps/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/apps/web/dist /usr/share/nginx/html
EXPOSE 3000
```

```nginx
# apps/web/nginx.conf
server {
    listen 3000;
    server_name _;
    root /usr/share/nginx/html;
    index index.html;

    location /api/ {
        proxy_pass http://api:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Request-Id $request_id;
    }

    location /health/ {
        proxy_pass http://api:8000;
    }

    location / {
        try_files $uri /index.html;
    }
}
```

Add this service to Compose:

```yaml
  web:
    build:
      context: ../..
      dockerfile: apps/web/Dockerfile
    depends_on:
      api: {condition: service_healthy}
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://localhost:3000/"]
      interval: 5s
      timeout: 3s
      retries: 20
    ports: ["3000:3000"]
```

- [ ] **Step 4: 让端到端测试从空卷运行**

Run:

```powershell
docker compose -f deploy/compose/compose.yaml down -v
docker compose -f deploy/compose/compose.yaml up -d --build
pnpm exec playwright test --config tests/e2e/playwright.config.ts
```

Expected: `1 passed`。此处删除的只是在明确命名的 Compose 项目 `tiny-hermes` 下由本计划创建的开发卷；执行前用 `docker compose ... config --volumes` 确认仅为 `postgres-data` 与 `minio-data`。

- [ ] **Step 5: 写 CI 的四个独立检查作业**

Write `.github/workflows/ci.yml` with this job structure; the implementation may only add cache keys or artifact retention, not remove commands:

```yaml
name: ci
on:
  push:
  pull_request:
permissions:
  contents: read

jobs:
  backend-unit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.12"}
      - uses: astral-sh/setup-uv@v6
        with: {version: "0.11.26", enable-cache: true}
      - run: uv sync --frozen
      - run: uv run ruff check packages/backend
      - run: uv run pyright
      - run: uv run pytest packages/backend/tests/unit -v

  backend-integration:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:17.6-alpine
        env:
          POSTGRES_DB: tiny_hermes_test
          POSTGRES_USER: tiny_hermes
          POSTGRES_PASSWORD: local-only
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U tiny_hermes -d tiny_hermes_test"
          --health-interval 5s --health-timeout 3s --health-retries 20
    env:
      DATABASE_URL: postgresql+asyncpg://tiny_hermes:local-only@localhost:5432/tiny_hermes_test
      TEST_DATABASE_URL: postgresql+asyncpg://tiny_hermes:local-only@localhost:5432/tiny_hermes_test
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.12"}
      - uses: astral-sh/setup-uv@v6
        with: {version: "0.11.26", enable-cache: true}
      - run: uv sync --frozen
      - run: uv run alembic upgrade head
      - run: uv run pytest packages/backend/tests/integration -v
      - run: uv run alembic check
      - run: uv run alembic downgrade base
      - run: uv run alembic upgrade head

  web:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: {node-version: "24.6.0"}
      - run: corepack enable
      - run: corepack prepare pnpm@10.15.0 --activate
      - run: pnpm install --frozen-lockfile
      - run: pnpm web:lint
      - run: pnpm web:test
      - run: pnpm web:build

  compose-e2e:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: {node-version: "24.6.0"}
      - run: corepack enable
      - run: corepack prepare pnpm@10.15.0 --activate
      - run: pnpm install --frozen-lockfile
      - run: pnpm exec playwright install --with-deps chromium
      - run: docker compose -f deploy/compose/compose.yaml up -d --build --wait
      - run: pnpm exec playwright test --config tests/e2e/playwright.config.ts
      - if: failure()
        run: docker compose -f deploy/compose/compose.yaml logs --no-color
      - if: failure()
        uses: actions/upload-artifact@v4
        with: {name: playwright-report, path: "playwright-report/", retention-days: 7}
      - if: always()
        run: docker compose -f deploy/compose/compose.yaml down -v
```

CI secrets use non-production generated values. No workflow logs `.env` or prints environment variables.

- [ ] **Step 6: 写开发说明并实际从头照做一次**

`docs/development.md` must include these exact sections:

```text
Requirements
  Python 3.12, uv 0.11.26, Node 24 LTS, pnpm 10.15.0, Docker with Compose
First start
  copy .env.example to .env and replace both example secrets
  docker compose -f deploy/compose/compose.yaml up -d --build
  POST /api/v1/bootstrap once with X-Bootstrap-Token
Local tests
  backend unit, integration, web and Playwright commands
Database migrations
  create, review, upgrade, downgrade smoke test and alembic check
Reset local development data
  inspect named volumes, then down -v only for the tiny-hermes Compose project
Security notes
  no real secrets in Git, Bootstrap closes permanently, local passwords are development-only
```

Run every command copied from the document in a fresh clone or clean temporary directory. Expected: no missing step, hidden local file or manual database edit is required.

- [ ] **Step 7: 运行第一阶段总检查**

Run:

```powershell
uv run ruff check packages/backend
uv run pyright
uv run pytest packages/backend/tests/unit -v
$env:TEST_DATABASE_URL='postgresql+asyncpg://tiny_hermes:local-only@localhost:5432/tiny_hermes_test'
uv run pytest packages/backend/tests/integration -v
uv run alembic check
pnpm web:lint
pnpm web:test
pnpm web:build
pnpm exec playwright test --config tests/e2e/playwright.config.ts
```

Expected: 所有命令退出 0。

- [ ] **Step 8: 核对发布边界和敏感内容**

Run:

```powershell
rg -n "BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|sk-[A-Za-z0-9]|AKIA[0-9A-Z]" . --glob '!pnpm-lock.yaml' --glob '!uv.lock'
git status --short
```

Expected: 敏感内容搜索没有命中；`git status` 只显示本任务预期文件。

- [ ] **Step 9: 提交第一阶段闭环**

```powershell
git add .github apps/web deploy/compose docs/development.md package.json pnpm-lock.yaml tests/e2e
git commit -m "ci: verify foundation from a clean environment"
```

Expected: 提交成功，随后 `git status --short` 无输出。

## 3. 第一阶段验收记录

执行完九个任务后，在 `docs/superpowers/verification/2026-08-10-m1-foundation.md` 保存以下事实：

- 测试命令、Git 提交 ID、Docker 与依赖版本。
- 空库 migration、降级/升级、Bootstrap 并发测试结果。
- 登录、创建两个 Workspace、退出的 Playwright 结果。
- 跨 Workspace 拒绝和错误响应无字段泄露的测试结果。
- `/health/live` 与 `/health/ready` 在数据库正常、断开和 migration 落后三种情况下的结果。
- 已知限制：尚无 Agent、Run、Worker、Scheduler、沙箱、ServiceAccount、API Key 或 Secret 管理。

只记录命令、版本、通过/失败和必要的脱敏错误摘要，不复制 Cookie、密码、Bootstrap Token 或数据库连接口令。

## 4. 进入阶段二的条件

只有以下条件同时成立，才根据实际代码编写阶段二 Run 主链路逐文件计划：

- 九个任务及第一阶段总检查全部通过。
- 全新 Compose 环境不依赖开发机残留数据。
- 身份、Workspace 和 AuditEvent 的表与事务边界没有未解决的设计冲突。
- `X-Workspace-Id`、请求 ID 和统一错误格式已经稳定，可供 Agent 与 Run API 复用。
- 用户确认阶段一的实际操作体验可以继续作为 M1 基础。

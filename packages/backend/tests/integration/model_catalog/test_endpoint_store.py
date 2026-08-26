from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.model_catalog.domain.models import (
    EndpointStatus,
    ModelEndpointSpec,
    UsageQuality,
)
from tiny_hermes.model_catalog.infrastructure.sql_store import SqlModelEndpointStore
from tiny_hermes.model_catalog.ports.store import EndpointNameTaken

pytestmark = pytest.mark.usefixtures("empty_database")


def spec(name: str = "acme-gpt", **overrides: object) -> ModelEndpointSpec:
    fields: dict[str, object] = {
        "name": name,
        "kind": "openai_compatible",
        "base_url": "https://models.example.com/v1",
        "model": "acme-large",
        "context_window": 128_000,
        "max_output_tokens": 4_096,
        "usage_quality": "provider",
        "credential_ref": "TINY_HERMES_MODEL_KEY_ACME",
    }
    fields.update(overrides)
    return ModelEndpointSpec.model_validate(fields)


async def test_an_endpoint_is_written_and_read_back(engine: AsyncEngine) -> None:
    author = uuid4()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        store = SqlModelEndpointStore(session)
        created = await store.register(spec(), created_by=author)
    async with factory() as session:
        found = await SqlModelEndpointStore(session).read(created.id)

    assert found is not None
    assert found.spec == spec()
    assert found.status is EndpointStatus.ACTIVE
    assert found.created_by == author


async def test_a_repeated_name_is_refused(engine: AsyncEngine) -> None:
    """The name is how an administrator refers to an endpoint, so it has to mean one."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        store = SqlModelEndpointStore(session)
        await store.register(spec(), created_by=uuid4())
    async with factory() as session:
        with pytest.raises(EndpointNameTaken):
            await SqlModelEndpointStore(session).register(
                spec(model="other-model"), created_by=uuid4()
            )


async def test_disabling_removes_it_from_the_selectable_list(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        store = SqlModelEndpointStore(session)
        first = await store.register(spec("acme-gpt"), created_by=uuid4())
        await store.register(spec("beta-gpt"), created_by=uuid4())
    async with factory() as session:
        await SqlModelEndpointStore(session).set_status(first.id, EndpointStatus.DISABLED)
    async with factory() as session:
        store = SqlModelEndpointStore(session)
        assert [entry.spec.name for entry in await store.list_active()] == ["beta-gpt"]
        # Still readable by id: an Agent Version published against it keeps
        # naming it, and a disabled endpoint has to be describable to say so.
        assert await store.read(first.id) is not None


async def test_an_unknown_id_is_absent_rather_than_an_error(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        assert await SqlModelEndpointStore(session).read(uuid4()) is None


async def test_usage_quality_survives_the_round_trip(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        created = await SqlModelEndpointStore(session).register(
            spec(usage_quality="unavailable"), created_by=uuid4()
        )
    async with factory() as session:
        found = await SqlModelEndpointStore(session).read(created.id)
    assert found is not None
    assert found.spec.usage_quality is UsageQuality.UNAVAILABLE


async def test_the_table_is_platform_scoped(engine: AsyncEngine) -> None:
    """No workspace column, asserted rather than assumed.

    Approving an endpoint belongs to the platform administrator; a workspace
    only chooses among approved ones. A later change that scopes endpoints per
    workspace is a design decision, and it should have to argue with this test
    rather than arrive as an extra column.
    """
    async with engine.connect() as connection:
        columns = await connection.run_sync(
            lambda sync: {entry["name"] for entry in inspect(sync).get_columns("model_endpoints")}
        )
    assert "workspace_id" not in columns


async def test_no_column_holds_a_credential(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        columns = await connection.run_sync(
            lambda sync: {entry["name"] for entry in inspect(sync).get_columns("model_endpoints")}
        )
    assert "credential_ref" in columns
    assert not {name for name in columns if name in {"credential", "api_key", "secret"}}


async def test_the_migration_created_the_table_not_the_metadata(engine: AsyncEngine) -> None:
    """The test database is migrated, so a table only SQLAlchemy knows about would fail here.

    Compared against Alembic's own head rather than a literal revision. The
    literal was correct and had to be edited by every slice that added a
    migration, which makes it a chore that eventually gets bumped without being
    read. Asking Alembic what head is keeps the check — an unmigrated database
    still fails — and removes the maintenance.
    """
    head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    async with engine.connect() as connection:
        version = await connection.execute(text("SELECT version_num FROM alembic_version"))
        assert version.scalar_one() == head


async def test_a_declared_vision_endpoint_stores_and_reads_back_as_one(
    engine: AsyncEngine,
) -> None:
    """The declaration has to survive the store, and it did not.

    `ModelEndpointRow` is built field by field, so a spec field with no line
    here is silently dropped: the console sent `accepts_images: true`, the
    API accepted it, the row was written `false`, and every image was
    replaced by "could not be retrieved" with nothing anywhere saying why.

    A frontend test asserted the flag left the browser and a domain test
    asserted the default — neither could see the gap between them.
    """
    from tiny_hermes.model_catalog.infrastructure.sql_store import SqlModelEndpointStore

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        store = SqlModelEndpointStore(session)
        created = await store.register(
            ModelEndpointSpec(
                name="vision",
                base_url="https://api.example.com/v1",
                model="deepseek-v4-flash-vision-exp",
                context_window=128_000,
                max_output_tokens=4_096,
                usage_quality=UsageQuality.PROVIDER,
                credential_ref="MODEL_KEY",
                accepts_images=True,
            ),
            created_by=await _some_user(engine),
        )
        await session.commit()

    async with sessions() as session:
        read = await SqlModelEndpointStore(session).read(created.id)

    assert read is not None
    assert read.spec.accepts_images is True


async def test_an_endpoint_that_said_nothing_reads_back_text_only(
    engine: AsyncEngine,
) -> None:
    from tiny_hermes.model_catalog.infrastructure.sql_store import SqlModelEndpointStore

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        store = SqlModelEndpointStore(session)
        created = await store.register(
            ModelEndpointSpec(
                name="plain",
                base_url="https://api.example.com/v1",
                model="deepseek-v4-flash",
                context_window=128_000,
                max_output_tokens=4_096,
                usage_quality=UsageQuality.PROVIDER,
                credential_ref="MODEL_KEY",
            ),
            created_by=await _some_user(engine),
        )
        await session.commit()

    async with sessions() as session:
        read = await SqlModelEndpointStore(session).read(created.id)

    assert read is not None
    assert read.spec.accepts_images is False


async def _some_user(engine: AsyncEngine) -> UUID:
    """Any user, created if the fixtures left none — `created_by` is a
    foreign key and this test is about the spec, not about authorship."""
    async with engine.begin() as connection:
        found = await connection.execute(text("SELECT id FROM users LIMIT 1"))
        existing = found.scalar_one_or_none()
        if existing is not None:
            return existing
        made = uuid4()
        await connection.execute(
            text(
                "INSERT INTO users (id, display_name, status, is_platform_admin,"
                " created_at) VALUES (:i, 'fixture', 'active', false, now())"
            ),
            {"i": made},
        )
        return made

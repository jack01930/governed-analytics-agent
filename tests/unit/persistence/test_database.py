import pytest
from alembic.config import Config

from governed_analytics.config import DatabaseSettings
from governed_analytics.persistence.database import (
    create_async_database_engine,
    set_alembic_database_url,
)


def test_engine_uses_pool_pre_ping_and_bounded_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = DatabaseSettings(
        database_url=(
            "postgresql+asyncpg://analytics_readonly:pw@localhost:5432/app"
        ),
    )
    expected_engine = object()
    recorded: dict[str, object] = {}

    def fake_create_async_engine(url: str, **kwargs: object) -> object:
        recorded["url"] = url
        recorded.update(kwargs)
        return expected_engine

    monkeypatch.setattr(
        "governed_analytics.persistence.database.create_async_engine",
        fake_create_async_engine,
    )

    engine = create_async_database_engine(settings)

    assert engine is expected_engine
    assert recorded == {
        "url": settings.database_url,
        "pool_pre_ping": True,
        "pool_size": 5,
        "max_overflow": 5,
        "pool_timeout": 5,
    }


def test_alembic_database_url_round_trips_percent_encoded_credentials() -> None:
    config = Config()
    migration_url = (
        "postgresql+psycopg://governed_admin:p%40ss%25word@localhost:5432/app"
    )

    set_alembic_database_url(config, migration_url)

    assert config.get_main_option("sqlalchemy.url") == migration_url

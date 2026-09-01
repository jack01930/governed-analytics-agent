import pytest

from governed_analytics.config import DatabaseSettings
from governed_analytics.persistence.database import create_async_database_engine


def test_engine_uses_pool_pre_ping_and_bounded_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = DatabaseSettings(
        database_url="postgresql+asyncpg://readonly:pw@localhost:5432/app",
        migration_database_url="postgresql+psycopg://admin:pw@localhost:5432/app",
        loader_database_url="postgresql+psycopg://loader:pw@localhost:5432/app",
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

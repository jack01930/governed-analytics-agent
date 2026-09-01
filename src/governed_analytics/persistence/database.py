from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from governed_analytics.config import DatabaseSettings


def create_async_database_engine(settings: DatabaseSettings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_timeout=5,
    )


def set_alembic_database_url(config: Config, database_url: str) -> None:
    """Persist a URL through Alembic's interpolating ConfigParser boundary."""
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

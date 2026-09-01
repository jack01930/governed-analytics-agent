import pytest
from pydantic import ValidationError

from governed_analytics.config import (
    DatabaseSettings,
    LoaderDatabaseSettings,
    MigrationDatabaseSettings,
)


def test_role_scoped_settings_load_only_their_own_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://analytics_readonly:readonly_pw@db:5432/app",
    )
    monkeypatch.setenv(
        "LOADER_DATABASE_URL",
        "postgresql+psycopg://analytics_loader:loader_pw@db:5432/app",
    )
    monkeypatch.setenv(
        "MIGRATION_DATABASE_URL",
        "postgresql+psycopg://governed_admin:admin_pw@db:5432/app",
    )

    readonly = DatabaseSettings(_env_file=None)  # type: ignore[call-arg]
    loader = LoaderDatabaseSettings(_env_file=None)  # type: ignore[call-arg]
    migration = MigrationDatabaseSettings(_env_file=None)  # type: ignore[call-arg]

    assert readonly.model_dump() == {"database_url": readonly.database_url}
    assert loader.model_dump() == {"loader_database_url": loader.loader_database_url}
    assert migration.model_dump() == {
        "migration_database_url": migration.migration_database_url
    }


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+psycopg://analytics_readonly:pw@db:5432/app",
        "postgresql+asyncpg://analytics_loader:pw@db:5432/app",
        "postgresql+asyncpg://governed_admin:pw@db:5432/app",
        "postgresql+asyncpg://%61nalytics_readonly:pw@db:5432/app",
        "postgresql+asyncpg://:pw@db:5432/app",
        "postgresql+asyncpg://analytics_readonly@db:5432/app",
        "postgresql+asyncpg://analytics_readonly:@db:5432/app",
    ],
)
def test_readonly_settings_reject_invalid_driver_role_or_password(database_url: str) -> None:
    with pytest.raises(ValidationError):
        DatabaseSettings(database_url=database_url)


@pytest.mark.parametrize(
    "loader_database_url",
    [
        "postgresql+asyncpg://analytics_loader:pw@db:5432/app",
        "postgresql+psycopg://analytics_readonly:pw@db:5432/app",
        "postgresql+psycopg://governed_admin:pw@db:5432/app",
        "postgresql+psycopg://analytics%5Floader:pw@db:5432/app",
        "postgresql+psycopg://analytics_loader@db:5432/app",
        "postgresql+psycopg://analytics_loader:@db:5432/app",
    ],
)
def test_loader_settings_reject_invalid_driver_role_or_password(
    loader_database_url: str,
) -> None:
    with pytest.raises(ValidationError):
        LoaderDatabaseSettings(loader_database_url=loader_database_url)


@pytest.mark.parametrize(
    "migration_database_url",
    [
        "postgresql+asyncpg://governed_admin:pw@db:5432/app",
        "postgresql+psycopg://analytics_readonly:pw@db:5432/app",
        "postgresql+psycopg://analytics_loader:pw@db:5432/app",
        "postgresql+psycopg://governed%5Fadmin:pw@db:5432/app",
        "postgresql+psycopg://governed_admin@db:5432/app",
        "postgresql+psycopg://governed_admin:@db:5432/app",
    ],
)
def test_migration_settings_reject_invalid_driver_role_or_password(
    migration_database_url: str,
) -> None:
    with pytest.raises(ValidationError):
        MigrationDatabaseSettings(migration_database_url=migration_database_url)


def test_percent_encoded_nonempty_password_is_valid() -> None:
    settings = MigrationDatabaseSettings(
        migration_database_url=(
            "postgresql+psycopg://governed_admin:p%40ss%25word@db:5432/app"
        )
    )

    assert "%40" in settings.migration_database_url
    assert "%25" in settings.migration_database_url

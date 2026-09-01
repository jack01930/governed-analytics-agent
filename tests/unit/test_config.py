import pytest
from pydantic import ValidationError

from governed_analytics.config import DatabaseSettings


def test_database_settings_accept_three_separate_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://readonly:pw@db:5432/app")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql+psycopg://admin:pw@db:5432/app")
    monkeypatch.setenv("LOADER_DATABASE_URL", "postgresql+psycopg://loader:pw@db:5432/app")

    settings = DatabaseSettings(_env_file=None)

    assert settings.database_url.startswith("postgresql+asyncpg://readonly:")
    assert settings.migration_database_url.startswith("postgresql+psycopg://admin:")
    assert settings.loader_database_url.startswith("postgresql+psycopg://loader:")


def test_readonly_url_cannot_equal_migration_url(monkeypatch: pytest.MonkeyPatch) -> None:
    shared = "postgresql+asyncpg://admin:pw@db:5432/app"
    monkeypatch.setenv("DATABASE_URL", shared)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", shared)
    monkeypatch.setenv("LOADER_DATABASE_URL", "postgresql+psycopg://loader:pw@db:5432/app")

    with pytest.raises(ValidationError, match="must use different credentials"):
        DatabaseSettings(_env_file=None)


@pytest.mark.parametrize(
    ("database_url", "migration_database_url"),
    [
        (
            "postgresql+asyncpg://admin:pw@readonly-db:5432/readonly_app",
            "postgresql+psycopg://admin:pw@migration-db:6543/migration_app",
        ),
    ],
)
def test_readonly_and_migration_urls_require_distinct_credentials(
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
    migration_database_url: str,
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", migration_database_url)
    monkeypatch.setenv("LOADER_DATABASE_URL", "postgresql+psycopg://loader:pw@db:5432/app")

    with pytest.raises(ValidationError, match="must use different credentials"):
        DatabaseSettings(_env_file=None)


def test_readonly_and_migration_urls_reject_percent_encoded_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://admi%6e:p%77@readonly-db:5432/readonly_app",
    )
    monkeypatch.setenv(
        "MIGRATION_DATABASE_URL",
        "postgresql+psycopg://admin:pw@migration-db:6543/migration_app",
    )
    monkeypatch.setenv("LOADER_DATABASE_URL", "postgresql+psycopg://loader:pw@db:5432/app")

    with pytest.raises(ValidationError, match="must use different credentials"):
        DatabaseSettings(_env_file=None)

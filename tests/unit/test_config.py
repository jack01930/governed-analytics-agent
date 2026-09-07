from decimal import Decimal

import pytest
from pydantic import ValidationError

from governed_analytics.config import (
    AgentRuntimeSettings,
    DatabaseSettings,
    LoaderDatabaseSettings,
    MigrationDatabaseSettings,
)


def test_agent_runtime_defaults_are_the_approved_week3_limits() -> None:
    settings = AgentRuntimeSettings(_env_file=None)  # type: ignore[call-arg]

    assert settings.runtime_mode == "fixture"
    assert not settings.live_enabled
    assert (
        settings.max_action_loops,
        settings.max_llm_calls,
        settings.max_tool_calls,
        settings.max_execute_calls,
        settings.max_profile_calls,
        settings.max_repairs,
    ) == (4, 8, 12, 5, 2, 1)
    assert settings.timeout_seconds == 60
    assert settings.soft_cost_cny == Decimal("0.20")
    assert settings.hard_cost_cny == Decimal("0.30")
    assert (
        settings.max_concurrent_runs,
        settings.max_runs,
        settings.run_retention_seconds,
        settings.sse_heartbeat_seconds,
    ) == (2, 100, 3600, 15)


def test_agent_live_mode_requires_server_side_live_enablement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_MODE", "live")
    monkeypatch.setenv("AGENT_LIVE_ENABLED", "false")

    with pytest.raises(ValidationError, match="live mode requires live_enabled"):
        AgentRuntimeSettings(_env_file=None)  # type: ignore[call-arg]


def test_agent_cost_caps_and_subbudgets_are_ordered() -> None:
    with pytest.raises(ValidationError):
        AgentRuntimeSettings(
            _env_file=None,  # type: ignore[call-arg]
            soft_cost_cny=Decimal("0.30"),
            hard_cost_cny=Decimal("0.30"),
        )
    with pytest.raises(ValidationError):
        AgentRuntimeSettings(  # type: ignore[call-arg]
            _env_file=None,
            max_execute_calls=13,
            max_tool_calls=12,
        )
    with pytest.raises(ValidationError):
        AgentRuntimeSettings(
            _env_file=None,  # type: ignore[call-arg]
            max_action_loops=5,
            max_repairs=1,
            max_execute_calls=5,
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
        DatabaseSettings(_env_file=None, database_url=database_url)  # type: ignore[call-arg]


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
        LoaderDatabaseSettings(  # type: ignore[call-arg]
            _env_file=None,
            loader_database_url=loader_database_url,
        )


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
        MigrationDatabaseSettings(  # type: ignore[call-arg]
            _env_file=None,
            migration_database_url=migration_database_url,
        )


def test_percent_encoded_nonempty_password_is_valid() -> None:
    settings = MigrationDatabaseSettings(
        _env_file=None,  # type: ignore[call-arg]
        migration_database_url=(
            "postgresql+psycopg://governed_admin:p%40ss%25word@db:5432/app"
        )
    )

    assert "%40" in settings.migration_database_url
    assert "%25" in settings.migration_database_url

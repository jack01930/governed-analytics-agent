from urllib.parse import unquote, urlsplit

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class _DatabaseSettingsBase(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )


def _validate_database_url(
    database_url: str,
    *,
    expected_driver: str,
    expected_username: str,
) -> str:
    parsed_url = urlsplit(database_url)
    if parsed_url.scheme != expected_driver:
        raise ValueError(f"database URL must use the {expected_driver} driver")

    encoded_username = parsed_url.username
    decoded_username = unquote(encoded_username) if encoded_username is not None else None
    if decoded_username != expected_username or encoded_username != decoded_username:
        raise ValueError(f"database URL must use the {expected_username} role")

    encoded_password = parsed_url.password
    decoded_password = unquote(encoded_password) if encoded_password is not None else None
    if not decoded_password:
        raise ValueError("database URL must include an explicit nonempty password")

    return database_url


class DatabaseSettings(_DatabaseSettingsBase):
    """Read-only analytics connection settings."""

    database_url: str

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, database_url: str) -> str:
        return _validate_database_url(
            database_url,
            expected_driver="postgresql+asyncpg",
            expected_username="analytics_readonly",
        )


class LoaderDatabaseSettings(_DatabaseSettingsBase):
    """Synthetic-data loader connection settings."""

    loader_database_url: str

    @field_validator("loader_database_url")
    @classmethod
    def validate_loader_database_url(cls, database_url: str) -> str:
        return _validate_database_url(
            database_url,
            expected_driver="postgresql+psycopg",
            expected_username="analytics_loader",
        )


class MigrationDatabaseSettings(_DatabaseSettingsBase):
    """Alembic migration connection settings."""

    migration_database_url: str

    @field_validator("migration_database_url")
    @classmethod
    def validate_migration_database_url(cls, database_url: str) -> str:
        return _validate_database_url(
            database_url,
            expected_driver="postgresql+psycopg",
            expected_username="governed_admin",
        )


class ModelSettings(BaseSettings):
    """Configuration metadata for an injected OpenAI-compatible model client."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    model_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model_api_key: SecretStr | None = None
    model_name: str = "qwen3.7-plus"
    eval_model_name: str = "qwen3.7-plus-2026-05-26"

    @field_validator("model_base_url")
    @classmethod
    def validate_model_base_url(cls, model_base_url: str) -> str:
        try:
            parsed_url = urlsplit(model_base_url)
            hostname = parsed_url.hostname
            _ = parsed_url.port
        except ValueError as error:
            raise ValueError("model base URL must be valid") from error
        if (
            not hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
            or "?" in model_base_url
            or "#" in model_base_url
        ):
            raise ValueError("model base URL must not include credentials, query, or fragment")
        if parsed_url.scheme == "https":
            return model_base_url
        if parsed_url.scheme == "http" and hostname.lower() in {"localhost", "127.0.0.1", "::1"}:
            return model_base_url
        raise ValueError("model base URL must use HTTPS except for loopback HTTP")

    @model_validator(mode="after")
    def validate_model_names(self) -> "ModelSettings":
        if not self.model_name.strip() or not self.eval_model_name.strip():
            raise ValueError("model names must not be blank")
        return self

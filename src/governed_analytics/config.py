from typing import Self
from urllib.parse import unquote, urlsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    database_url: str
    migration_database_url: str
    loader_database_url: str

    @model_validator(mode="after")
    def require_separate_readonly_credentials(self) -> Self:
        readonly_credentials = self._credentials(self.database_url)
        migration_credentials = self._credentials(self.migration_database_url)
        if readonly_credentials == migration_credentials:
            raise ValueError(
                "database_url and migration_database_url must use different credentials"
            )
        return self

    @staticmethod
    def _credentials(database_url: str) -> tuple[str | None, str | None]:
        parsed_url = urlsplit(database_url)
        username = unquote(parsed_url.username) if parsed_url.username is not None else None
        password = unquote(parsed_url.password) if parsed_url.password is not None else None
        return username, password

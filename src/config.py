from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_environment: str = "local"
    database_url: str
    test_database_url: str | None = None
    database_pool_size: int = 10
    database_max_overflow: int = 5
    jwt_secret: str
    test_seed_key: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
import os

from src.config import Settings

settings = Settings()
os.environ["DATABASE_URL"] = os.getenv(
    "TEST_DATABASE_URL",
    settings.test_database_url or settings.database_url.rsplit("/", maxsplit=1)[0] + "/wallet_test",
)
os.environ["APP_ENVIRONMENT"] = "test"

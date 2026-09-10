from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOADS_DIR = BASE_DIR / "uploads"
DATA_DIR = BASE_DIR / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    secret_key: str = "truckgps-dev-secret-change-in-production"
    access_token_expire_days: int = 7
    database_url: str = f"sqlite:///{(DATA_DIR / 'truckgps.db').as_posix()}"
    cors_origins: str = "http://localhost:4200,http://127.0.0.1:4200"


settings = Settings()
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

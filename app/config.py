import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
# En Vercel solo /tmp es escribible (y es temporal por instancia).
RUNTIME_DIR = Path("/tmp/truckgps") if os.environ.get("VERCEL") else BASE_DIR
UPLOADS_DIR = RUNTIME_DIR / "uploads"
DATA_DIR = RUNTIME_DIR / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    secret_key: str = "truckgps-dev-secret-change-in-production"
    access_token_expire_days: int = 7
    database_url: str = f"sqlite:///{(DATA_DIR / 'truckgps.db').as_posix()}"
    cors_origins: str = "http://localhost:4200,http://127.0.0.1:4200"


settings = Settings()

# Permite usar una URL de Postgres (Supabase/Neon) tal como la entregan.
if settings.database_url.startswith(("postgres://", "postgresql://")):
    settings.database_url = "postgresql+psycopg://" + settings.database_url.split("://", 1)[1]

UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

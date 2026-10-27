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

    # Avisos al supervisor. Se configuran como variables de entorno en Vercel.
    app_url: str = "https://truck-gps-umber.vercel.app"
    alert_emails: str = ""  # correos separados por coma
    alert_phones: str = ""  # celulares con código de país, separados por coma (+51...)
    alerts_from_email: str = "TruckGPS <onboarding@resend.dev>"
    resend_api_key: str = ""
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from: str = ""  # número de Twilio que envía el SMS
    zone_alert_cooldown_min: int = 30
    # Las zonas oficiales (Lima/Callao) siempre se registran y se muestran al
    # conductor; el correo/SMS al supervisor solo se envía si esto es true.
    official_zone_alerts: bool = False


settings = Settings()

# Permite usar una URL de Postgres (Supabase/Neon) tal como la entregan.
if settings.database_url.startswith(("postgres://", "postgresql://")):
    settings.database_url = "postgresql+psycopg://" + settings.database_url.split("://", 1)[1]

UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

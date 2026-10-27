"""Punto de entrada para Vercel (Python serverless)."""
from app.database import Base, SessionLocal, engine
from app.main import app  # noqa: F401  (Vercel sirve este objeto ASGI)
from app.migrate import ensure_columns
from app.official_zones import load_official_zones
from app.seed import seed_if_empty

# Vercel no garantiza el evento lifespan: crea tablas y datos demo al importar.
Base.metadata.create_all(bind=engine)
ensure_columns(engine)
_db = SessionLocal()
try:
    seed_if_empty(_db)
    _db.commit()
    load_official_zones(_db)
except Exception:
    _db.rollback()
    raise
finally:
    _db.close()

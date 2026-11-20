from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import UPLOADS_DIR, settings
from app.database import Base, SessionLocal, engine
from app.migrate import ensure_columns
from app.routers import alerts, auth, dashboard, fuel, incidents, mapas, places, profiles, reportes, routes, settings as settings_router, simulator, traffic, vehicles, zones
from app.official_zones import load_official_zones
from app.seed import seed_if_empty


STARTUP_LOCK = 727001


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Con Postgres varias instancias pueden arrancar a la vez: un candado evita
    # que dos creen las tablas o siembren los datos al mismo tiempo.
    from sqlalchemy import text

    from app.database import IS_SQLITE

    lock_conn = None if IS_SQLITE else engine.connect()
    if lock_conn is not None:
        lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": STARTUP_LOCK})
    try:
        _startup()
    finally:
        if lock_conn is not None:
            lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": STARTUP_LOCK})
            lock_conn.close()
    yield


def _startup() -> None:
    Base.metadata.create_all(bind=engine)
    ensure_columns(engine)
    db = SessionLocal()
    try:
        seed_if_empty(db)
        load_official_zones(db)
        from app.history import seed_route_history

        seed_route_history(db)
        from app.demo_rutas import seed_rutas_ejemplo

        seed_rutas_ejemplo(db)
        from app.simulation import ensure_simulation

        ensure_simulation(db)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


app = FastAPI(title="TruckGPS API", version="1.0.0", lifespan=lifespan)

origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")
app.include_router(auth.router, prefix="/api")
app.include_router(profiles.router, prefix="/api")
app.include_router(vehicles.router, prefix="/api")
app.include_router(incidents.router, prefix="/api")
app.include_router(routes.router, prefix="/api")
app.include_router(alerts.router, prefix="/api")
app.include_router(dashboard.router, prefix="/api")
app.include_router(fuel.router, prefix="/api")
app.include_router(zones.router, prefix="/api")
app.include_router(simulator.router, prefix="/api")
app.include_router(traffic.router, prefix="/api")
app.include_router(settings_router.router, prefix="/api")
app.include_router(places.router, prefix="/api")
app.include_router(mapas.router, prefix="/api")
app.include_router(reportes.router, prefix="/api")


@app.get("/api/health")
def health():
    return {"ok": True, "service": "truckgps-api"}

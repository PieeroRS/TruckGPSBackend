from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


IS_SQLITE = settings.database_url.startswith("sqlite")
connect_args = {"check_same_thread": False} if IS_SQLITE else {}
# En Postgres (Neon) las funciones serverless abren y reusan pocas conexiones;
# pre_ping evita usar una conexión que el servidor ya cerró por inactividad.
pool_kwargs = {} if IS_SQLITE else {"pool_pre_ping": True, "pool_size": 3, "max_overflow": 2, "pool_recycle": 300}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True, **pool_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@event.listens_for(Engine, "connect")
def _sqlite_fk(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
    if settings.database_url.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

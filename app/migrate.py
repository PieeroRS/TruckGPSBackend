"""Migraciones ligeras: agrega columnas nuevas a tablas existentes.

`Base.metadata.create_all` crea tablas nuevas pero no agrega columnas a las que
ya existen, así que una base creada antes del módulo de consumo necesita esto.
"""
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

# tabla -> {columna: tipo SQL}
NEW_COLUMNS: dict[str, dict[str, str]] = {
    "vehicles": {
        "rendimiento_km_gal": "FLOAT",
        "tanque_gal": "FLOAT",
    },
}


def ensure_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in NEW_COLUMNS.items():
            if table not in tables:
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            for name, sql_type in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))

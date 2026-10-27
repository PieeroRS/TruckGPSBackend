"""Carga las zonas peligrosas oficiales de Lima y Callao desde app/data/zonas_lima_callao.json.

Se ejecuta en cada arranque: agrega las zonas nuevas y actualiza los datos de
las existentes (nombre, coordenadas, fuentes), pero respeta si el supervisor
desactivó alguna.
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import RiskZone

DATA_FILE = Path(__file__).resolve().parent / "data" / "zonas_lima_callao.json"
FIELDS = ("nombre", "tipo", "nivel", "lat", "lng", "radio_m", "notas", "fuentes", "periodo")


def load_official_zones(db: Session) -> int:
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    existing = {z.codigo: z for z in db.scalars(select(RiskZone).where(RiskZone.origen == "oficial")).all()}
    count = 0
    for item in data["zonas"]:
        zone = existing.get(item["codigo"])
        if zone is None:
            zone = RiskZone(codigo=item["codigo"], origen="oficial", activa=True)
            db.add(zone)
        for f in FIELDS:
            setattr(zone, f, item.get(f))
        count += 1
    db.commit()
    return count

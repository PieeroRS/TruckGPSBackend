"""Viajes completados de ejemplo para el informe de desempeño de rutas (CP025).

Se cargan una sola vez, solo si todavía no hay ninguna ruta completada, para que el
informe no salga vacío mientras la flota no termine rutas reales desde la app.
Quedan marcados en «notas» como viaje de ejemplo.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import NavRoute, Vehicle, VehiclePosition, utcnow

LIMA = timezone(timedelta(hours=-5))
NOTA = "Viaje de ejemplo para el informe de desempeño de rutas."

# placa, origen, (lat, lng), destino, (lat, lng), km planificados, min estimados, min reales, días atrás, hora de salida
VIAJES = [
    ("ABC-123", "Callao, Perú", (-12.0566, -77.1181), "Puente Piedra, Lima", (-11.8667, -77.0750), 31.4, 52, 49, 13, 7),
    ("PER-789", "Lurín, Lima", (-12.2747, -76.8708), "Ate, Lima", (-12.0262, -76.9180), 38.2, 61, 74, 11, 15),
    ("DGR-456", "Callao, Perú", (-12.0566, -77.1181), "Chorrillos, Lima", (-12.1683, -77.0240), 24.9, 44, 47, 9, 10),
    ("ABC-123", "Lima, Perú", (-12.0464, -77.0428), "Huaral, Lima", (-11.4950, -77.2078), 79.6, 98, 104, 6, 6),
    ("PER-789", "Ate, Lima", (-12.0262, -76.9180), "Callao, Perú", (-12.0566, -77.1181), 29.7, 55, 58, 4, 16),
    ("DGR-456", "Lima, Perú", (-12.0464, -77.0428), "Cañete, Lima", (-13.0770, -76.3870), 145.0, 150, 171, 2, 8),
    ("ABC-123", "Puente Piedra, Lima", (-11.8667, -77.0750), "Callao, Perú", (-12.0566, -77.1181), 31.0, 50, 46, 1, 14),
]


def seed_rutas_ejemplo(db: Session, ahora: datetime | None = None) -> int:
    if db.scalar(select(func.count()).select_from(NavRoute).where(NavRoute.status == "completada")):
        return 0
    vehiculos = {v.placa: v for v in db.scalars(select(Vehicle)).all()}
    ahora = (ahora or utcnow()).astimezone(LIMA)
    hoy = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
    n = 0
    for placa, on, o, dn, d, km, est, real, dias, hora in VIAJES:
        v = vehiculos.get(placa)
        if v is None:
            continue
        inicio = (hoy - timedelta(days=dias) + timedelta(hours=hora)).astimezone(timezone.utc)
        db.add(NavRoute(
            vehicle_id=v.id, origin_name=on, origin_lat=o[0], origin_lng=o[1], dest_name=dn, dest_lat=d[0], dest_lng=d[1],
            route_label=f"{on.split(',')[0]} → {dn.split(',')[0]}", distance_km=km, duration_min=est,
            status="completada", progress_pct=100, notes=NOTA, created_at=inicio, started_at=inicio,
            completed_at=inicio + timedelta(minutes=real),
        ))
        # Recorrido GPS del viaje (para los km medidos con GPS).
        pasos = 16
        for i in range(pasos + 1):
            f = i / pasos
            db.add(VehiclePosition(
                vehicle_id=v.id, lat=o[0] + (d[0] - o[0]) * f + 0.002 * (i % 3 - 1), lng=o[1] + (d[1] - o[1]) * f,
                velocidad=45, recorded_at=inicio + timedelta(minutes=real * f),
            ))
        n += 1
    db.flush()
    return n

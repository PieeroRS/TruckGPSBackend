"""Recorrido de demostración de la flota de ejemplo (ABC-123, PER-789).

Estas unidades no tienen un GPS real conectado. Para que el Mapa en Vivo muestre
movimiento, avanzan solas por su corredor (Panamericana Norte, Carretera Central)
a su velocidad de referencia. La posición se calcula con la hora, así que es la
misma en todas las instancias del servidor. Se marcan como fuente "demo" y la app
las muestra como «Recorrido simulado», nunca como GPS en vivo.

Si un conductor activa el GPS de su celular en una de estas unidades, manda su
posición real y el recorrido de demostración se detiene.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.geo import haversine_m, path_length_m, point_at
from app.models import AppSetting, Vehicle, VehiclePosition, utcnow

log = logging.getLogger("truckgps.demo")

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
RECORD_EVERY_S = 10
REAL_GPS_GRACE_S = 600

# Puntos de paso por la vía (se usan para pedir la ruta a OSRM y como respaldo si no responde).
CORREDORES: dict[str, dict] = {
    "ABC-123": {
        "nombre": "Lima → Trujillo",
        "kmh": 78,
        "inicio_pct": 0.18,
        "puntos": [
            [-12.0464, -77.0428], [-11.9447, -77.0640], [-11.8660, -77.0745], [-11.7730, -77.1510],
            [-11.6600, -77.2200], [-11.5650, -77.2700], [-11.4930, -77.2290], [-11.1070, -77.6050],
            [-10.7530, -77.7600], [-10.6720, -77.8200], [-10.0700, -78.1500], [-9.4700, -78.3000],
            [-9.0800, -78.5800], [-8.9800, -78.6200], [-8.5400, -78.6800], [-8.4100, -78.7500],
            [-8.1116, -79.0288],
        ],
    },
    "PER-789": {
        "nombre": "Lima → Huancayo",
        "kmh": 40,
        "inicio_pct": 0.32,
        "puntos": [
            [-12.0464, -77.0428], [-12.0300, -76.9200], [-11.9400, -76.7000], [-11.8450, -76.3850],
            [-11.7600, -76.3000], [-11.5900, -76.1900], [-11.5200, -75.9000], [-11.7800, -75.5000],
            [-11.9200, -75.3100], [-12.0651, -75.2049],
        ],
    },
}

_geoms: dict[str, list[list[float]]] = {}
_osrm_failed_at: dict[str, float] = {}
_last_tick = 0.0
_last_prune = 0.0


def _densify(points: list[list[float]], step_m: float = 2000) -> list[list[float]]:
    out = [points[0]]
    for a, b in zip(points, points[1:]):
        d = haversine_m(a[0], a[1], b[0], b[1])
        n = max(1, int(d // step_m))
        for i in range(1, n + 1):
            f = i / n
            out.append([a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f])
    return out


def geometry(db: Session, placa: str) -> list[list[float]]:
    """Trazado del corredor: OSRM (por la vía real) guardado en la base, o los puntos de paso."""
    if placa in _geoms:
        return _geoms[placa]
    clave = f"demo_geom:{placa}"
    row = db.get(AppSetting, clave)
    if row and row.valor and len(row.valor.get("geometry", [])) > 10:
        _geoms[placa] = row.valor["geometry"]
        return _geoms[placa]
    conf = CORREDORES[placa]
    if time.monotonic() - _osrm_failed_at.get(placa, -1e9) > 600:
        from app.routing import RoutingError, osrm_routes

        pts = conf["puntos"]
        vias = [pts[0], *pts[2:-1:3], pts[-1]]
        try:
            geom = osrm_routes([tuple(p) for p in vias], timeout=4)[0]["geometry"]
            # Una de cada dos coordenadas basta para el mapa y ahorra espacio.
            geom = geom[::2] + ([geom[-1]] if len(geom) % 2 == 0 else [])
            if row is None:
                row = AppSetting(clave=clave)
                db.add(row)
            row.valor = {"geometry": geom, "fuente": "osrm"}
            db.flush()
            _geoms[placa] = geom
            return geom
        except RoutingError:
            _osrm_failed_at[placa] = time.monotonic()
    return _densify(conf["puntos"])


def position_at(db: Session, placa: str, when: datetime) -> dict:
    conf = CORREDORES[placa]
    geom = geometry(db, placa)
    total = path_length_m(geom)
    secs = (when - EPOCH).total_seconds()
    along = (conf["inicio_pct"] * total + conf["kmh"] / 3.6 * secs) % total
    lat, lng, rumbo = point_at(geom, along)
    vel = conf["kmh"] + (3 if int(secs / 30) % 3 == 0 else -2 if int(secs / 30) % 3 == 1 else 0)
    return {"lat": round(lat, 6), "lng": round(lng, 6), "vel": vel, "rumbo": rumbo, "along": along, "total": total}


def track(db: Session, placa: str, minutos: int, now: datetime | None = None) -> list[dict]:
    """Recorrido de los últimos minutos (calculado con la misma fórmula; se corta al reiniciar el corredor)."""
    now = now or utcnow()
    pts: list[dict] = []
    prev_along = None
    step = max(10, int(minutos * 60 / 240))
    t = now - timedelta(minutes=minutos)
    while t <= now:
        p = position_at(db, placa, t)
        if prev_along is not None and p["along"] < prev_along:
            pts = []  # empezó una nueva vuelta: solo el tramo actual
        pts.append({"lat": p["lat"], "lng": p["lng"], "velocidad": p["vel"], "t": t.isoformat()})
        prev_along = p["along"]
        t += timedelta(seconds=step)
    return pts


def is_demo_active(vehicle: Vehicle, now: datetime) -> bool:
    if vehicle.placa not in CORREDORES or vehicle.simulado:
        return False
    if vehicle.fuente_posicion == "gps" and vehicle.ultima_posicion_at is not None:
        from app.detection import aware

        if (now - aware(vehicle.ultima_posicion_at)).total_seconds() < REAL_GPS_GRACE_S:
            return False
    return True


def tick(db: Session, now: datetime | None = None, force: bool = False) -> None:
    """Mueve las unidades de demostración y guarda su posición (cada 10 s como máximo)."""
    global _last_tick, _last_prune
    mono = time.monotonic()
    if not force and mono - _last_tick < RECORD_EVERY_S:
        return
    _last_tick = mono
    now = now or utcnow()
    from app.detection import check_speeding

    try:
        with db.begin_nested():
            for v in db.scalars(select(Vehicle).where(Vehicle.placa.in_(list(CORREDORES)))).all():
                if not is_demo_active(v, now):
                    continue
                p = position_at(db, v.placa, now)
                db.add(VehiclePosition(vehicle_id=v.id, lat=p["lat"], lng=p["lng"], velocidad=int(p["vel"]), precision_m=None, recorded_at=now))
                v.lat, v.lng, v.velocidad = p["lat"], p["lng"], int(p["vel"])
                v.precision_m = None
                v.ultima_posicion_at = now
                v.fuente_posicion = "demo"
                v.ruta = CORREDORES[v.placa]["nombre"]
                db.flush()
                check_speeding(db, v, p["vel"], p["lat"], p["lng"])
                if mono - _last_prune > 1800:
                    from sqlalchemy import delete

                    db.execute(
                        delete(VehiclePosition).where(
                            VehiclePosition.vehicle_id == v.id, VehiclePosition.recorded_at < now - timedelta(hours=6)
                        )
                    )
        if mono - _last_prune > 1800:
            _last_prune = mono
    except Exception:  # noqa: BLE001 - nunca debe romper la consulta principal
        log.exception("Error en el recorrido de demostración")

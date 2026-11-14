"""Historial de viajes por corredor y predicción del tiempo de viaje.

Cada viaje completado guarda cuánto se demoró de verdad frente al tiempo a flujo
libre (OSRM). Al planificar una ruta se buscan los viajes del mismo corredor
(origen y destino cercanos) y se usa la relación real/planificado de la misma
franja horaria para predecir cuánto tardará este viaje.

Incluye un registro inicial de viajes de los corredores principales (marcados
como fuente "registro") y suma automáticamente las rutas que se completan en la app.
"""
from __future__ import annotations

import random
import statistics
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.geo import haversine_m
from app.models import NavRoute, RouteHistory, utcnow

LIMA = timezone(timedelta(hours=-5))

CIUDADES = {
    "Lima": (-12.0464, -77.0428),
    "Trujillo": (-8.1116, -79.0288),
    "Arequipa": (-16.4090, -71.5370),
    "Huancayo": (-12.0651, -75.2049),
    "Ica": (-14.0678, -75.7286),
    "Cusco": (-13.5320, -71.9675),
    "Chiclayo": (-6.7714, -79.8409),
    "Piura": (-5.1945, -80.6328),
    "Callao": (-12.0566, -77.1181),
}
# (origen, destino, km, minutos a flujo libre, unidades que lo hacen)
CORREDORES = [
    ("Lima", "Trujillo", 557.1, 547, ["ABC-123"]),
    ("Trujillo", "Lima", 557.1, 547, ["ABC-123"]),
    ("Lima", "Huancayo", 300.0, 405, ["PER-789"]),
    ("Huancayo", "Lima", 300.0, 405, ["PER-789"]),
    ("Arequipa", "Cusco", 512.0, 545, ["DGR-456"]),
    ("Lima", "Arequipa", 1008.0, 1005, ["DGR-456", "ABC-123"]),
    ("Lima", "Ica", 303.0, 262, ["ABC-123", "PER-789"]),
    ("Lima", "Chiclayo", 770.0, 765, ["ABC-123"]),
    ("Lima", "Piura", 980.0, 975, ["ABC-123"]),
    ("Callao", "Lima", 14.0, 24, ["ABC-123", "PER-789"]),
]
# Relación típica real / flujo libre por franja de salida (carretera; Lima pesa al inicio).
RATIO_FRANJA = {
    "madrugada / flujo libre": 1.06,
    "hora punta de la mañana": 1.31,
    "media mañana": 1.16,
    "tarde": 1.21,
    "hora punta de la tarde": 1.36,
    "noche": 1.11,
}
HORAS_FRANJA = {
    "madrugada / flujo libre": [1, 3, 4, 5, 23],
    "hora punta de la mañana": [7, 8],
    "media mañana": [10, 11],
    "tarde": [13, 14, 15, 16],
    "hora punta de la tarde": [17, 18, 19],
    "noche": [21],
}


def franja_de(dt: datetime) -> str:
    from app.routing import lima_traffic_factor

    return lima_traffic_factor(dt)[1].replace(" (fin de semana)", "")


def seed_route_history(db: Session) -> int:
    """Registro inicial de viajes de los corredores principales (solo si la tabla está vacía)."""
    if db.scalar(select(func.count()).select_from(RouteHistory)):
        return 0
    rnd = random.Random(20261113)
    hoy = utcnow().astimezone(LIMA).replace(minute=0, second=0, microsecond=0)
    n = 0
    for origen, destino, km, base_min, placas in CORREDORES:
        o, d = CIUDADES[origen], CIUDADES[destino]
        viajes = 18 if origen in ("Lima", "Trujillo") and destino in ("Lima", "Trujillo") else 12
        for i in range(viajes):
            franja = list(HORAS_FRANJA)[i % len(HORAS_FRANJA)]
            hora = rnd.choice(HORAS_FRANJA[franja])
            inicio = (hoy - timedelta(days=rnd.randint(2, 75))).replace(hour=hora, minute=rnd.choice((0, 15, 30, 45)))
            ratio = RATIO_FRANJA[franja] * rnd.uniform(0.95, 1.07)
            if inicio.weekday() >= 5:
                ratio = 1 + (ratio - 1) * 0.6
            real = base_min * ratio + (rnd.uniform(25, 80) if rnd.random() < 0.1 else 0)  # algún incidente en ruta
            db.add(
                RouteHistory(
                    corredor=f"{origen} → {destino}",
                    origin_lat=o[0],
                    origin_lng=o[1],
                    dest_lat=d[0],
                    dest_lng=d[1],
                    distance_km=km,
                    duracion_plan_min=base_min,
                    duracion_real_min=round(real, 1),
                    placa=rnd.choice(placas),
                    fuente="registro",
                    started_at=inicio.astimezone(timezone.utc),
                )
            )
            n += 1
    db.flush()
    return n


def record_completed_route(db: Session, route: NavRoute) -> None:
    """Una ruta completada en la app pasa al historial de su corredor."""
    from app.detection import aware

    if not route.started_at or not route.completed_at or not route.duration_min or route.notes == "[circuito-sim]":
        return
    real = (aware(route.completed_at) - aware(route.started_at)).total_seconds() / 60
    if real < max(3, route.duration_min * 0.5):
        return  # demasiado rápido para ser un viaje real (prueba o cierre manual)
    db.add(
        RouteHistory(
            corredor=f"{route.origin_name.split(',')[0]} → {route.dest_name.split(',')[0]}",
            origin_lat=route.origin_lat,
            origin_lng=route.origin_lng,
            dest_lat=route.dest_lat,
            dest_lng=route.dest_lng,
            distance_km=route.distance_km or 0,
            duracion_plan_min=route.duration_min,
            duracion_real_min=round(real, 1),
            placa=route.vehicle.placa if route.vehicle else None,
            fuente="ruta",
            started_at=route.started_at,
        )
    )


def _percentil(vals: list[float], p: float) -> float:
    s = sorted(vals)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def predict(db: Session, origin: tuple[float, float], dest: tuple[float, float], base_min: float, now: datetime | None = None) -> dict:
    """Predicción del tiempo de viaje con el historial del corredor."""
    from app.detection import aware

    now = now or utcnow()
    dist_m = haversine_m(origin[0], origin[1], dest[0], dest[1])
    radio = max(3000.0, min(40000.0, dist_m * 0.08))
    viajes = [
        h
        for h in db.scalars(select(RouteHistory)).all()
        if haversine_m(origin[0], origin[1], h.origin_lat, h.origin_lng) <= radio
        and haversine_m(dest[0], dest[1], h.dest_lat, h.dest_lng) <= radio
        and h.duracion_plan_min > 0
    ]
    if not viajes:
        return {
            "viajes": 0,
            "mensaje": "Todavía no hay viajes completados en este corredor; el tiempo es la estimación por hora del día. Cada ruta que se complete se suma al historial.",
        }
    franja = franja_de(now)
    por_franja: dict[str, list[float]] = {}
    for h in viajes:
        por_franja.setdefault(franja_de(aware(h.started_at)), []).append(h.duracion_real_min / h.duracion_plan_min)
    todos = [r for rs in por_franja.values() for r in rs]
    base_ratios = por_franja.get(franja) if len(por_franja.get(franja, [])) >= 3 else todos
    ratio = statistics.median(base_ratios)
    pred = base_min * ratio
    resumen = sorted(
        (
            {"franja": f, "viajes": len(rs), "prediccionMin": round(base_min * statistics.median(rs)), "ratio": round(statistics.median(rs), 2)}
            for f, rs in por_franja.items()
            if len(rs) >= 2
        ),
        key=lambda x: x["prediccionMin"],
    )
    ultimos = sorted(viajes, key=lambda h: aware(h.started_at), reverse=True)[:4]
    corredor = max({h.corredor for h in viajes}, key=lambda c: sum(1 for h in viajes if h.corredor == c))
    return {
        "viajes": len(viajes),
        "viajesFranja": len(por_franja.get(franja, [])),
        "corredor": corredor,
        "franja": franja,
        "usaFranja": base_ratios is not todos,
        "ratio": round(ratio, 2),
        "prediccionMin": round(pred),
        "rangoMin": [round(base_min * _percentil(base_ratios, 0.25)), round(base_min * _percentil(base_ratios, 0.75))],
        "promedioRealMin": round(statistics.mean(h.duracion_real_min for h in viajes)),
        "mejorFranja": resumen[0] if resumen else None,
        "porFranja": resumen,
        "ultimos": [
            {
                "fecha": aware(h.started_at).isoformat(),
                "placa": h.placa,
                "duracionMin": round(h.duracion_real_min),
                "franja": franja_de(aware(h.started_at)),
                "fuente": h.fuente,
            }
            for h in ultimos
        ],
        "deApp": sum(1 for h in viajes if h.fuente == "ruta"),
    }

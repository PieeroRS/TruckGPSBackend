"""Cálculo de rutas (OSRM) y estimación de tráfico.

OSRM (router.project-osrm.org, datos de OpenStreetMap) da la geometría y el
tiempo a flujo libre. No tiene tráfico en vivo, así que el tiempo con tráfico es
una ESTIMACIÓN: factor por hora del día en Lima + demoras por eventos de tráfico
activos (los que registra el supervisor o el simulador). Para tráfico real se
puede conectar un proveedor (TomTom, HERE, Google) más adelante.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from app.geo import destination, haversine_m, path_length_m, project

OSRM_URL = "https://router.project-osrm.org/route/v1/driving/"
UA = "TruckGPS/1.0 (+https://truck-gps-umber.vercel.app)"
LIMA = timezone(timedelta(hours=-5))

# Demora agregada cuando una ruta pasa por un evento de tráfico.
EVENT_DELAY_MIN = {"moderado": 6, "congestionado": 15, "bloqueo": 35}
EVENT_LABEL = {"moderado": "Tráfico moderado", "congestionado": "Congestión", "bloqueo": "Vía bloqueada"}


class RoutingError(RuntimeError):
    pass


def osrm_routes(points: list[tuple[float, float]], alternatives: int = 0) -> list[dict]:
    """Rutas entre puntos [(lat, lng), ...]. Cada una: distance_km, duration_min, geometry [[lat,lng]]."""
    coords = ";".join(f"{lng:.6f},{lat:.6f}" for lat, lng in points)
    params = {"overview": "full", "geometries": "geojson"}
    if alternatives and len(points) == 2:
        params["alternatives"] = str(alternatives)
    url = OSRM_URL + coords + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        raise RoutingError("No se pudo conectar con el servicio de rutas (OSRM)") from exc
    if data.get("code") != "Ok" or not data.get("routes"):
        raise RoutingError("No hay ruta disponible entre esos puntos")
    out = []
    for r in data["routes"]:
        out.append(
            {
                "distance_km": round(r["distance"] / 1000, 2),
                "duration_min": round(r["duration"] / 60, 1),
                "geometry": [[round(lat, 5), round(lng, 5)] for lng, lat in r["geometry"]["coordinates"]],
            }
        )
    return out


def detour_alternative(origin: tuple[float, float], dest: tuple[float, float], avoid: tuple[float, float] | None = None) -> list[dict]:
    """Rutas alternativas forzadas por un punto intermedio desplazado a cada lado del trayecto."""
    mid_lat = (origin[0] + dest[0]) / 2
    mid_lng = (origin[1] + dest[1]) / 2
    if avoid:
        mid_lat, mid_lng = avoid
    straight = haversine_m(origin[0], origin[1], dest[0], dest[1])
    offset = max(1200.0, min(30000.0, straight * 0.12))
    from app.geo import bearing

    b = bearing(origin[0], origin[1], dest[0], dest[1])
    found = []
    for side in (90, -90):
        via = destination(mid_lat, mid_lng, b + side, offset)
        try:
            r = osrm_routes([origin, via, dest])[0]
        except RoutingError:
            continue
        found.append(r)
    return found


def lima_traffic_factor(when: datetime | None = None) -> tuple[float, str]:
    """Factor típico de congestión en Lima según la hora (1.0 = flujo libre)."""
    now = (when or datetime.now(timezone.utc)).astimezone(LIMA)
    h = now.hour + now.minute / 60
    if 6.5 <= h < 9.5:
        f, label = 1.6, "hora punta de la mañana"
    elif 9.5 <= h < 12:
        f, label = 1.25, "media mañana"
    elif 12 <= h < 17:
        f, label = 1.3, "tarde"
    elif 17 <= h < 20.5:
        f, label = 1.8, "hora punta de la tarde"
    elif 20.5 <= h < 22:
        f, label = 1.2, "noche"
    else:
        f, label = 1.0, "madrugada / flujo libre"
    if now.weekday() >= 5:
        f = 1 + (f - 1) * 0.5
        label += " (fin de semana)"
    return round(f, 2), label


def traffic_level(factor: float) -> str:
    if factor < 1.15:
        return "fluido"
    if factor < 1.5:
        return "moderado"
    return "congestionado"


def events_on_path(path: list[list[float]], events: list) -> list:
    """Eventos de tráfico que la ruta atraviesa (a menos de su radio)."""
    hits = []
    for ev in events:
        pr = project(path, ev.lat, ev.lng)
        if pr and pr.distance_m <= ev.radio_m:
            hits.append(ev)
    return hits


def estimate(route: dict, events: list, when: datetime | None = None) -> dict:
    """Agrega a una ruta OSRM el tiempo estimado con tráfico."""
    factor, franja = lima_traffic_factor(when)
    base = route["duration_min"]
    hits = events_on_path(route["geometry"], events)
    delay = sum(EVENT_DELAY_MIN.get(e.nivel, 10) for e in hits)
    total = round(base * factor + delay, 1)
    eff = total / base if base else factor
    return {
        **route,
        "duration_traffic_min": total,
        "traffic_factor": round(eff, 2),
        "traffic_level": "congestionado" if hits else traffic_level(eff),
        "franja": franja,
        "eventos": [
            {"id": e.id, "descripcion": e.descripcion, "nivel": e.nivel, "demora_min": EVENT_DELAY_MIN.get(e.nivel, 10)}
            for e in hits
        ],
    }


def plan_with_alternatives(origin: tuple[float, float], dest: tuple[float, float], events: list) -> list[dict]:
    """Hasta 3 rutas distintas, con tráfico estimado, ordenadas por tiempo estimado."""
    routes = osrm_routes([origin, dest], alternatives=3)
    if len(routes) < 2:
        avoid = None
        if events:
            hits = events_on_path(routes[0]["geometry"], events)
            if hits:
                avoid = (hits[0].lat, hits[0].lng)
        routes += detour_alternative(origin, dest, avoid)
    # Quitar duplicados casi iguales y desvíos absurdos (> 1.6 veces la más corta).
    shortest = min(r["distance_km"] for r in routes)
    uniq: list[dict] = []
    for r in routes:
        if r["distance_km"] > shortest * 1.6:
            continue
        if any(abs(r["distance_km"] - u["distance_km"]) < 0.05 and abs(r["duration_min"] - u["duration_min"]) < 0.5 for u in uniq):
            continue
        uniq.append(r)
    est = sorted((estimate(r, events) for r in uniq[:3]), key=lambda r: r["duration_traffic_min"])
    for i, r in enumerate(est):
        r["recomendada"] = i == 0
    return est


def geometry_length_km(geometry: list[list[float]]) -> float:
    return round(path_length_m(geometry) / 1000, 2)

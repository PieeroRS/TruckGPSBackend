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
EVENT_DELAY_MIN = {"moderado": 6, "congestionado": 15, "bloqueo": 35, "cierre": 45}
EVENT_LABEL = {"moderado": "Tráfico moderado", "congestionado": "Congestión", "bloqueo": "Vía bloqueada", "cierre": "Calle cerrada"}
# Una calle cerrada no se atraviesa: las rutas la rodean. Radio del cierre reportado.
CLOSURE_RADIUS_M = 25


class RoutingError(RuntimeError):
    pass


def osrm_routes(points: list[tuple[float, float]], alternatives: int = 0, timeout: float = 8, steps: bool = False) -> list[dict]:
    """Rutas entre puntos [(lat, lng), ...]. Cada una: distance_km, duration_min, geometry [[lat,lng]].

    Con steps=True agrega "steps": las maniobras giro a giro en español (para la guía por voz).
    """
    coords = ";".join(f"{lng:.6f},{lat:.6f}" for lat, lng in points)
    params = {"overview": "full", "geometries": "geojson"}
    if steps:
        params["steps"] = "true"
    if alternatives and len(points) == 2:
        params["alternatives"] = str(alternatives)
    url = OSRM_URL + coords + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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
        if steps:
            out[-1]["steps"] = build_steps(r, out[-1]["geometry"])
    return out


# ─────────────── indicaciones giro a giro (español) ───────────────

_GIRO = {
    "left": "gira a la izquierda",
    "right": "gira a la derecha",
    "slight left": "gira ligeramente a la izquierda",
    "slight right": "gira ligeramente a la derecha",
    "sharp left": "gira cerrado a la izquierda",
    "sharp right": "gira cerrado a la derecha",
    "uturn": "da la vuelta en U",
    "straight": "sigue recto",
}
_LADO = {"left": "izquierda", "slight left": "izquierda", "sharp left": "izquierda", "right": "derecha", "slight right": "derecha", "sharp right": "derecha"}
_ORDINAL = {1: "primera", 2: "segunda", 3: "tercera", 4: "cuarta", 5: "quinta", 6: "sexta"}


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def step_text(tipo: str, mod: str, calle: str, salida: int | None = None, destinos: str = "") -> str | None:
    """Texto de una maniobra de OSRM. None = no se anuncia."""
    en = f" en {calle}" if calle else ""
    por = f" por {calle}" if calle else ""
    hacia = f" hacia {calle}" if calle else (f" hacia {destinos.split(',')[0].split(':')[-1].strip()}" if destinos else "")
    lado = _LADO.get(mod)
    if tipo == "depart":
        return f"Inicia el recorrido{por}"
    if tipo == "arrive":
        return "Has llegado a tu destino" + (f", a la {lado}" if lado else "")
    if tipo in ("turn", "continue"):
        if mod == "straight" or not mod:
            return f"Continúa{por}" if calle else "Sigue recto"
        if mod == "uturn":
            return f"Da la vuelta en U{en}"
        return _cap(_GIRO.get(mod, "gira")) + en
    if tipo == "new name":
        return f"Continúa{por}" if calle else None
    if tipo == "merge":
        return f"Incorpórate{' a ' + calle if calle else ''}" + (f" por la {lado}" if lado and not calle else "")
    if tipo == "on ramp":
        return f"Toma el acceso{' a la ' + lado if lado else ''}{hacia}"
    if tipo == "off ramp":
        return f"Toma la salida{' a la ' + lado if lado else ''}{hacia}"
    if tipo == "fork":
        return f"Mantente a la {lado}{hacia}" if lado else f"Continúa{por}"
    if tipo == "end of road":
        return f"Al final de la vía, {_GIRO.get(mod, 'gira')}{en}"
    if tipo in ("roundabout", "rotary"):
        n = _ORDINAL.get(salida or 0)
        return (f"En el óvalo, toma la {n} salida" if n else "Entra al óvalo") + hacia
    if tipo == "roundabout turn":
        return f"En el óvalo, {_GIRO.get(mod, 'continúa')}{en}"
    if tipo in ("exit roundabout", "exit rotary"):
        return None
    if tipo in ("notification", "use lane"):
        return None
    return f"Continúa{por}" if calle else None


def build_steps(route: dict, geometry: list[list[float]]) -> list[dict]:
    """Maniobras de una ruta OSRM: texto, punto y metros desde el inicio (along_m)."""
    legs = route.get("legs") or []
    total_geom = path_length_m(geometry)
    total_osrm = sum(st.get("distance", 0) for leg in legs for st in leg.get("steps", [])) or 1.0
    escala = total_geom / total_osrm if total_osrm else 1.0
    out: list[dict] = []
    acumulado = 0.0
    for li, leg in enumerate(legs):
        for st in leg.get("steps", []):
            man = st.get("maneuver", {})
            tipo, mod = man.get("type", ""), man.get("modifier", "")
            dist = float(st.get("distance", 0))
            # Los puntos de paso de un desvío no son llegadas ni salidas reales.
            intermedio = (tipo == "arrive" and li < len(legs) - 1) or (tipo == "depart" and li > 0)
            calle = (st.get("name") or "").strip() or (st.get("ref") or "").split(";")[0].strip()
            texto = None if intermedio else step_text(tipo, mod, calle, man.get("exit"), st.get("destinations") or "")
            if texto:
                lng, lat = man.get("location", [None, None])
                out.append(
                    {
                        "texto": texto,
                        "tipo": tipo,
                        "mod": mod,
                        "calle": calle,
                        "lat": round(lat, 6) if lat is not None else None,
                        "lng": round(lng, 6) if lng is not None else None,
                        "along_m": round(acumulado * escala),
                        "distancia_m": round(dist),
                    }
                )
            acumulado += dist
    # Une «Continúa por X» seguidos de la misma calle y descarta pasos pegados al anterior.
    limpio: list[dict] = []
    for s in out:
        if limpio and s["tipo"] in ("new name", "continue") and s["mod"] in ("straight", "") and s["calle"] and s["calle"] == limpio[-1]["calle"]:
            limpio[-1]["distancia_m"] += s["distancia_m"]
            continue
        limpio.append(s)
    return limpio


# ─────────────── calles cerradas ───────────────

def closures_of(events: list) -> list:
    return [e for e in events if e.nivel == "cierre"]


def route_avoiding(origin: tuple[float, float], dest: tuple[float, float], closures: list, steps: bool = False) -> dict:
    """Mejor ruta que no pasa por ninguna calle cerrada.

    Primero prueba las alternativas de OSRM; si todas pasan por un cierre, fuerza desvíos por
    puntos de paso a cada lado de la vía cerrada. Devuelve la ruta con:
    - cierres_evitados: cierres por los que pasaba la ruta directa y la devuelta ya no.
    - cierres_en_ruta: cierres que no se pudieron rodear (la ruta pasa por ellos).
    """
    from concurrent.futures import ThreadPoolExecutor

    directas = osrm_routes([origin, dest], alternatives=3, steps=steps)
    base = directas[0]
    if not closures:
        return {**base, "cierres_evitados": [], "cierres_en_ruta": []}
    hits_base = events_on_path(base["geometry"], closures)
    if not hits_base:
        return {**base, "cierres_evitados": [], "cierres_en_ruta": []}

    def libre(r: dict) -> bool:
        return not events_on_path(r["geometry"], closures)

    libres = [r for r in directas if libre(r)]
    if not libres:
        # Desvíos forzados alrededor del primer cierre que encuentra la ruta.
        ev = min(hits_base, key=lambda e: project(base["geometry"], e.lat, e.lng).along_m)
        pr = project(base["geometry"], ev.lat, ev.lng)
        from app.geo import point_at

        _, _, rumbo = point_at(base["geometry"], pr.along_m)
        vias = [destination(ev.lat, ev.lng, rumbo + lado, d) for d in (180, 350, 650, 1100) for lado in (90, -90)]

        def intento(via: tuple[float, float]) -> dict | None:
            try:
                return osrm_routes([origin, via, dest], steps=steps)[0]
            except RoutingError:
                return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            cands = [c for c in pool.map(intento, vias) if c]
        limite = base["distance_km"] * 2.5 + 3
        libres = [c for c in cands if libre(c) and c["distance_km"] <= limite]
    if libres:
        mejor = min(libres, key=lambda r: r["duration_min"])
        return {**mejor, "cierres_evitados": [closure_brief(e) for e in hits_base], "cierres_en_ruta": []}
    return {**base, "cierres_evitados": [], "cierres_en_ruta": [closure_brief(e) for e in hits_base]}


def closure_brief(e) -> dict:
    return {"id": e.id, "descripcion": e.descripcion, "lat": e.lat, "lng": e.lng}


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
    """Hasta 3 rutas distintas, con tráfico estimado, ordenadas por tiempo estimado.

    Las rutas que pasan por una calle cerrada se descartan si existe otra que la rodea.
    """
    routes = osrm_routes([origin, dest], alternatives=3)
    cierres = closures_of(events)
    evitados: list[dict] = []
    if cierres and any(events_on_path(r["geometry"], cierres) for r in routes):
        try:
            alterna = route_avoiding(origin, dest, cierres)
        except RoutingError:
            alterna = None
        if alterna and not alterna["cierres_en_ruta"]:
            evitados = alterna["cierres_evitados"]
            limpias = [r for r in routes if not events_on_path(r["geometry"], cierres)]
            routes = [{k: alterna[k] for k in ("distance_km", "duration_min", "geometry")}] + limpias
    if len(routes) < 2:
        avoid = None
        if events:
            hits = events_on_path(routes[0]["geometry"], events)
            if hits:
                avoid = (hits[0].lat, hits[0].lng)
        routes += detour_alternative(origin, dest, avoid)
    if evitados:
        routes = [r for r in routes if not events_on_path(r["geometry"], cierres)]
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
        r["cierres_evitados"] = evitados if not any(e["nivel"] == "cierre" for e in r["eventos"]) else []
        r["pasa_por_cierre"] = any(e["nivel"] == "cierre" for e in r["eventos"])
    return est


def geometry_length_km(geometry: list[list[float]]) -> float:
    return round(path_length_m(geometry) / 1000, 2)

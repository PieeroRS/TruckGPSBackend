"""TomTom: tráfico en vivo (calles verdes, amarillas y rojas) y búsqueda de lugares.

La clave se guarda solo en el servidor (variable de entorno TOMTOM_API_KEY en Vercel).
El navegador nunca la ve: pide los cuadros de tráfico y las búsquedas a este servidor,
que los trae de TomTom y guarda una copia corta para no gastar el cupo gratuito.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from app.config import settings

log = logging.getLogger("truckgps.tomtom")

BASE = "https://api.tomtom.com"
UA = "TruckGPS/1.0"
LIMA_TZ = timezone(timedelta(hours=-5))

# Perú con un margen: fuera de aquí no se piden cuadros (protege el cupo).
PERU = (-18.6, -81.6, 0.3, -68.4)  # sur, oeste, norte, este
ZOOM_MIN, ZOOM_MAX = 6, 18

# Cupo gratuito de TomTom: la búsqueda tiene bastante menos que los cuadros de tráfico.
SEARCH_DAILY_CAP = 80
TILE_TTL_S = 120
SEARCH_TTL_S = 24 * 3600
SIGN_WINDOW_S = 6 * 3600


class TomTomError(Exception):
    def __init__(self, msg: str, status: int = 502):
        super().__init__(msg)
        self.status = status


def configured() -> bool:
    return bool(settings.tomtom_api_key.strip())


# ─────────────── copia en memoria ───────────────


class _TTLCache:
    def __init__(self, max_items: int):
        self.max = max_items
        self.data: OrderedDict[str, tuple[float, object]] = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key: str):
        with self.lock:
            hit = self.data.get(key)
            if not hit:
                return None
            exp, val = hit
            if exp < time.time():
                self.data.pop(key, None)
                return None
            self.data.move_to_end(key)
            return val

    def put(self, key: str, val: object, ttl: float) -> None:
        with self.lock:
            self.data[key] = (time.time() + ttl, val)
            self.data.move_to_end(key)
            while len(self.data) > self.max:
                self.data.popitem(last=False)


_tiles = _TTLCache(1500)
_searches = _TTLCache(800)
_search_day = {"dia": "", "n": 0}
_last_error: dict[str, str | float | None] = {"msg": None, "at": 0.0}


def _remember_error(msg: str) -> None:
    _last_error["msg"] = msg
    _last_error["at"] = time.time()


def _fetch(path: str, params: dict[str, str | int | float], timeout: float = 6) -> tuple[bytes, str]:
    if not configured():
        raise TomTomError("El tráfico en vivo no está configurado", 503)
    q = {"key": settings.tomtom_api_key.strip(), **params}
    url = f"{BASE}{path}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        # Nunca se registra la URL: lleva la clave.
        if e.code in (401, 403):
            msg = "TomTom rechazó la clave (revisa que esté bien copiada y que tenga habilitado ese servicio)"
        elif e.code == 429:
            msg = "Se alcanzó el límite de consultas de TomTom por ahora"
        elif e.code == 400:
            msg = "TomTom no aceptó la consulta"
        else:
            msg = f"TomTom respondió con error {e.code}"
        _remember_error(msg)
        raise TomTomError(msg, 429 if e.code == 429 else 502) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        _remember_error("No se pudo conectar con TomTom")
        raise TomTomError("No se pudo conectar con TomTom", 504) from None


# ─────────────── cuadros de tráfico ───────────────


def _tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 2**z

    def lat(yy: int) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))

    return lat(y + 1), x / n * 360 - 180, lat(y), (x + 1) / n * 360 - 180


def tile_allowed(z: int, x: int, y: int) -> bool:
    if not (ZOOM_MIN <= z <= ZOOM_MAX) or not (0 <= x < 2**z) or not (0 <= y < 2**z):
        return False
    s, w, n, e = _tile_bounds(z, x, y)
    ps, pw, pn, pe = PERU
    return not (n < ps or s > pn or e < pw or w > pe)


def _sig_for(window: int) -> str:
    return hmac.new(settings.secret_key.encode(), f"tt-tiles:{window}".encode(), hashlib.sha256).hexdigest()[:20]


def tile_signature() -> tuple[str, int]:
    """Firma compartida para las URL de cuadros (cambia cada 6 h). Así la copia del CDN sirve a todos."""
    w = int(time.time() // SIGN_WINDOW_S)
    return _sig_for(w), (w + 1) * SIGN_WINDOW_S


def signature_ok(sig: str) -> bool:
    w = int(time.time() // SIGN_WINDOW_S)
    return any(hmac.compare_digest(sig or "", _sig_for(k)) for k in (w, w - 1))


# Formatos de pedido, del preferido al más simple. Se usa el primero que TomTom acepte.
FLOW_VARIANTS: list[tuple[str, dict]] = [
    ("relative0", {"tileSize": 256}),
    ("relative", {"tileSize": 256}),
    ("relative", {}),
]
_variant = {"i": 0}


def _flow_params(i: int, z: int) -> tuple[str, dict]:
    style, extra = FLOW_VARIANTS[i]
    params = dict(extra)
    if extra:
        # Líneas finas cuando se ve la ciudad entera; más gruesas al acercarse a la calle.
        params["thickness"] = 2 if z <= 12 else 3 if z <= 14 else 5
    return style, params


def flow_tile(z: int, x: int, y: int) -> bytes:
    key = f"{z}/{x}/{y}"
    hit = _tiles.get(key)
    if hit is not None:
        return hit  # type: ignore[return-value]
    while True:
        style, params = _flow_params(_variant["i"], z)
        try:
            data, ctype = _fetch(f"/traffic/map/4/tile/flow/{style}/{z}/{x}/{y}.png", params)
            break
        except TomTomError:
            # «No aceptó la consulta» (400): se prueba el siguiente formato. Otros errores se informan.
            if _last_error["msg"] == "TomTom no aceptó la consulta" and _variant["i"] + 1 < len(FLOW_VARIANTS):
                _variant["i"] += 1
                continue
            raise
    if "image" not in ctype:
        _remember_error("TomTom no devolvió una imagen de tráfico")
        raise TomTomError("TomTom no devolvió una imagen de tráfico")
    _tiles.put(key, data, TILE_TTL_S)
    _last_error["msg"] = None
    return data


# ─────────────── mapa base (navegación) ───────────────

BASE_STYLES = ("main", "night")
BASE_DAILY_CAP = 6000
_base = _TTLCache(2500)
_base_day = {"dia": "", "n": 0}


def base_tile(style: str, z: int, x: int, y: int) -> bytes:
    """Cuadro del mapa base de TomTom (de día «main» o de noche «night») con nombres en español."""
    if style not in BASE_STYLES:
        raise TomTomError("Estilo de mapa no válido", 400)
    key = f"{style}/{z}/{x}/{y}"
    hit = _base.get(key)
    if hit is not None:
        return hit  # type: ignore[return-value]
    dia = _today()
    if _base_day["dia"] != dia:
        _base_day.update(dia=dia, n=0)
    if int(_base_day["n"]) >= BASE_DAILY_CAP:
        raise TomTomError("Se usó el cupo diario del mapa", 429)
    _base_day["n"] = int(_base_day["n"]) + 1
    try:
        data, ctype = _fetch(f"/map/1/tile/basic/{style}/{z}/{x}/{y}.png", {"tileSize": 256, "language": "es-ES"})
    except TomTomError:
        if _last_error["msg"] != "TomTom no aceptó la consulta":
            raise
        data, ctype = _fetch(f"/map/1/tile/basic/{style}/{z}/{x}/{y}.png", {})
    if "image" not in ctype:
        raise TomTomError("TomTom no devolvió una imagen del mapa")
    _base.put(key, data, 6 * 3600)
    return data


# ─────────────── búsqueda de lugares ───────────────


def _today() -> str:
    return datetime.now(LIMA_TZ).strftime("%Y-%m-%d")


def search_left_today() -> int:
    if _search_day["dia"] != _today():
        return SEARCH_DAILY_CAP
    return max(0, SEARCH_DAILY_CAP - int(_search_day["n"]))


def _clean(q: str) -> str:
    return " ".join(q.strip().split())[:120]


def search(q: str, lat: float | None, lng: float | None) -> list[dict]:
    """Lugares en Perú cerca de un punto. Devuelve [] si ya se usó el cupo del día."""
    q = _clean(q)
    if len(q) < 3:
        return []
    # La misma búsqueda en el mismo barrio (~1 km) se reutiliza todo el día.
    zona = f"{round(lat, 2)},{round(lng, 2)}" if lat is not None and lng is not None else "-"
    key = f"{q.lower()}|{zona}"
    hit = _searches.get(key)
    if hit is not None:
        return hit  # type: ignore[return-value]
    if search_left_today() <= 0:
        return []
    dia = _today()
    if _search_day["dia"] != dia:
        _search_day.update(dia=dia, n=0)
    _search_day["n"] = int(_search_day["n"]) + 1

    params: dict[str, str | int | float] = {
        "countrySet": "PE",
        "language": "es-ES",
        "limit": 8,
        "typeahead": "true",
        "idxSet": "POI,PAD,Addr,Str,Xstr,Geo",
    }
    if lat is not None and lng is not None:
        params.update(lat=round(lat, 5), lon=round(lng, 5))
    data, _ = _fetch(f"/search/2/search/{urllib.parse.quote(q, safe='')}.json", params)
    try:
        body = json.loads(data)
    except ValueError:
        raise TomTomError("Respuesta inválida de TomTom") from None

    out: list[dict] = []
    for r in body.get("results", []):
        pos = r.get("position") or {}
        if "lat" not in pos or "lon" not in pos:
            continue
        addr = r.get("address") or {}
        tipo = r.get("type", "")
        poi = r.get("poi") or {}
        if tipo == "POI" and poi.get("name"):
            nombre = poi["name"]
            detalle = addr.get("freeformAddress") or ", ".join(
                x for x in (addr.get("streetName"), addr.get("municipalitySubdivision"), addr.get("municipality")) if x
            )
            clase = "poi"
        elif tipo in ("Street", "Cross Street", "Point Address", "Address Range"):
            nombre = addr.get("streetName") or addr.get("freeformAddress") or q
            if addr.get("streetNumber"):
                nombre = f"{nombre} {addr['streetNumber']}"
            detalle = ", ".join(x for x in (addr.get("municipalitySubdivision"), addr.get("municipality")) if x)
            clase = "street"
        else:
            nombre = addr.get("municipalitySubdivision") or addr.get("municipality") or addr.get("freeformAddress") or q
            detalle = ", ".join(x for x in (addr.get("municipality"), addr.get("countrySubdivision")) if x and x != nombre)
            clase = "area"
        out.append({"nombre": nombre, "detalle": detalle or None, "lat": pos["lat"], "lng": pos["lon"], "tipo": clase})
    _searches.put(key, out, SEARCH_TTL_S)
    _last_error["msg"] = None
    return out


# ─────────────── estado (para revisar la configuración sin ver la clave) ───────────────

_status_cache: dict[str, object] = {"at": 0.0, "val": None}


def status() -> dict:
    if not configured():
        return {"configurado": False, "funciona": None, "detalle": "Falta la variable TOMTOM_API_KEY en el servidor"}
    cached = _status_cache["val"]
    if cached is not None and time.time() - float(_status_cache["at"]) < 600:
        return cached  # type: ignore[return-value]
    try:
        flow_tile(12, 1171, 2186)  # un cuadro del centro de Lima
        val = {"configurado": True, "funciona": True, "detalle": None, "estilo": FLOW_VARIANTS[_variant["i"]][0]}
    except TomTomError as e:
        val = {"configurado": True, "funciona": False, "detalle": str(e), "intento": _variant["i"] + 1}
    val["busquedasRestantesHoy"] = search_left_today()
    val["rutasRestantesHoy"] = route_left_today()
    _status_cache.update(at=time.time(), val=val)
    return val


# ─────────────── rutas con tráfico en vivo ───────────────

# Plan gratuito: 20 000 cálculos de ruta al mes (~660 al día). Se deja margen.
ROUTE_DAILY_CAP = 500
ROUTE_TTL_S = 60
_routes = _TTLCache(300)
_route_day = {"dia": "", "n": 0}

# Maniobra de TomTom → (tipo, modificador) con los mismos nombres que usa la app para los íconos.
_MANIOBRA = {
    "DEPART": ("depart", ""),
    "ARRIVE": ("arrive", ""),
    "ARRIVE_LEFT": ("arrive", "left"),
    "ARRIVE_RIGHT": ("arrive", "right"),
    "STRAIGHT": ("continue", "straight"),
    "FOLLOW": ("continue", "straight"),
    "KEEP_RIGHT": ("fork", "slight right"),
    "BEAR_RIGHT": ("turn", "slight right"),
    "TURN_RIGHT": ("turn", "right"),
    "SHARP_RIGHT": ("turn", "sharp right"),
    "KEEP_LEFT": ("fork", "slight left"),
    "BEAR_LEFT": ("turn", "slight left"),
    "TURN_LEFT": ("turn", "left"),
    "SHARP_LEFT": ("turn", "sharp left"),
    "MAKE_UTURN": ("turn", "uturn"),
    "TRY_MAKE_UTURN": ("turn", "uturn"),
    "ENTER_MOTORWAY": ("merge", ""),
    "ENTER_FREEWAY": ("merge", ""),
    "ENTER_HIGHWAY": ("merge", ""),
    "ENTRANCE_RAMP": ("on ramp", ""),
    "TAKE_EXIT": ("off ramp", "slight right"),
    "MOTORWAY_EXIT_RIGHT": ("off ramp", "slight right"),
    "MOTORWAY_EXIT_LEFT": ("off ramp", "slight left"),
    "ROUNDABOUT_CROSS": ("roundabout", "straight"),
    "ROUNDABOUT_RIGHT": ("roundabout", "right"),
    "ROUNDABOUT_LEFT": ("roundabout", "left"),
    "ROUNDABOUT_BACK": ("roundabout", "uturn"),
    "SWITCH_PARALLEL_ROAD": ("continue", "straight"),
    "SWITCH_MAIN_ROAD": ("continue", "straight"),
    "TAKE_FERRY": ("continue", "straight"),
    "WAYPOINT_LEFT": ("continue", "left"),
    "WAYPOINT_RIGHT": ("continue", "right"),
    "WAYPOINT_REACHED": ("continue", "straight"),
}


def route_left_today() -> int:
    if _route_day["dia"] != _today():
        return ROUTE_DAILY_CAP
    return max(0, ROUTE_DAILY_CAP - int(_route_day["n"]))


def _avoid_rects(avoid: list[tuple[float, float, float]]) -> list[dict]:
    """Un rectángulo pequeño alrededor de cada calle cerrada (TomTom acepta hasta 10)."""
    rects = []
    for lat, lng, radio in avoid[:10]:
        r = max(radio, 20.0) + 10
        dlat = r / 111_320
        dlng = r / (111_320 * max(0.2, math.cos(math.radians(lat))))
        rects.append(
            {
                "southWestCorner": {"latitude": round(lat - dlat, 6), "longitude": round(lng - dlng, 6)},
                "northEastCorner": {"latitude": round(lat + dlat, 6), "longitude": round(lng + dlng, 6)},
            }
        )
    return rects


def _steps_from(route: dict, geometry: list[list[float]]) -> list[dict]:
    ins = ((route.get("guidance") or {}).get("instructions")) or []
    steps: list[dict] = []
    for k, i in enumerate(ins):
        man = i.get("maneuver") or ""
        tipo, mod = _MANIOBRA.get(man, ("continue", "straight"))
        if k == 0:
            tipo, mod = "depart", ""
        texto = (i.get("message") or "").strip()
        if not texto:
            continue
        # La flecha sigue al texto: «Manténgase a la derecha» siempre apunta a la derecha.
        bajo = texto.lower()
        if "derecha" in bajo and "left" in mod and "izquierda" not in bajo:
            mod = mod.replace("left", "right")
        elif "izquierda" in bajo and "right" in mod and "derecha" not in bajo:
            mod = mod.replace("right", "left")
        pt = i.get("point") or {}
        steps.append(
            {
                "texto": texto.rstrip("."),
                "tipo": tipo,
                "mod": mod,
                "calle": i.get("street") or "",
                "lat": pt.get("latitude", geometry[0][0]),
                "lng": pt.get("longitude", geometry[0][1]),
                "along_m": int(i.get("routeOffsetInMeters") or 0),
                "distancia_m": 0,
            }
        )
    for a, b in zip(steps, steps[1:]):
        a["distancia_m"] = max(0, b["along_m"] - a["along_m"])
    if steps and steps[-1]["tipo"] != "arrive":
        total = int(route.get("summary", {}).get("lengthInMeters") or 0)
        steps.append({"texto": "Has llegado a tu destino", "tipo": "arrive", "mod": "", "calle": "", "lat": geometry[-1][0], "lng": geometry[-1][1], "along_m": total, "distancia_m": 0})
    return steps


def _traffic_sections(route: dict) -> list[dict]:
    """Tramos de la ruta con tráfico: índices del trazado y nivel (lento, pesado, detenido)."""
    out = []
    for sec in route.get("sections") or []:
        if sec.get("sectionType") != "TRAFFIC":
            continue
        mag = int(sec.get("magnitudeOfDelay") or 0)
        cat = sec.get("simpleCategory") or ""
        nivel = "detenido" if mag >= 3 or cat == "ROAD_CLOSURE" else "pesado" if mag == 2 else "lento"
        out.append(
            {
                "desde": int(sec.get("startPointIndex") or 0),
                "hasta": int(sec.get("endPointIndex") or 0),
                "nivel": nivel,
                "demora_s": int(sec.get("delayInSeconds") or 0),
            }
        )
    return out


def routes(
    points: list[tuple[float, float]],
    alternatives: int = 0,
    avoid: list[tuple[float, float, float]] | None = None,
    guidance: bool = False,
) -> list[dict]:
    """Rutas en auto con el tráfico de este momento (la más rápida primero).

    Cada ruta trae: distance_km, duration_min (sin tráfico), duration_live_min (con el tráfico
    de ahora), delay_min, geometry y, si se pide, steps (indicaciones en español).
    Lanza TomTomError si no está configurado, se acabó el cupo del día o TomTom falla.
    """
    if not configured():
        raise TomTomError("TomTom no está configurado", 503)
    avoid = avoid or []
    clave = "|".join(
        [
            ";".join(f"{p[0]:.4f},{p[1]:.4f}" for p in points),
            str(alternatives),
            ";".join(f"{a[0]:.5f},{a[1]:.5f}" for a in avoid),
            "g" if guidance else "",
        ]
    )
    hit = _routes.get(clave)
    if hit is not None:
        return hit  # type: ignore[return-value]
    if route_left_today() <= 0:
        raise TomTomError("Se usó el cupo diario de rutas con tráfico", 429)
    dia = _today()
    if _route_day["dia"] != dia:
        _route_day.update(dia=dia, n=0)
    _route_day["n"] = int(_route_day["n"]) + 1

    params: dict[str, str | int | float] = {
        "traffic": "true",
        "computeTravelTimeFor": "all",
        "routeType": "fastest",
        "travelMode": "car",
        "maxAlternatives": alternatives,
        "routeRepresentation": "polyline",
    }
    if guidance:
        params.update(instructionsType="text", language="es-ES", sectionType="traffic")
    locs = ":".join(f"{p[0]:.6f},{p[1]:.6f}" for p in points)
    path = f"/routing/1/calculateRoute/{locs}/json"
    if avoid:
        data, _ = _post(path, params, {"avoidAreas": {"rectangles": _avoid_rects(avoid)}})
    else:
        data, _ = _fetch(path, params, timeout=8)
    try:
        body = json.loads(data)
    except ValueError:
        raise TomTomError("Respuesta inválida de TomTom") from None

    out: list[dict] = []
    for r in body.get("routes", []):
        geometry = [[p["latitude"], p["longitude"]] for leg in r.get("legs", []) for p in leg.get("points", [])]
        if len(geometry) < 2:
            continue
        s = r.get("summary", {})
        vivo = (s.get("travelTimeInSeconds") or 0) / 60
        libre = (s.get("noTrafficTravelTimeInSeconds") or s.get("travelTimeInSeconds") or 0) / 60
        item = {
            "distance_km": round((s.get("lengthInMeters") or 0) / 1000, 2),
            "duration_min": round(libre, 1),
            "duration_live_min": round(vivo, 1),
            "delay_min": round((s.get("trafficDelayInSeconds") or 0) / 60, 1),
            "arrival": s.get("arrivalTime"),
            "geometry": geometry,
            "fuente": "tomtom",
        }
        if guidance:
            item["steps"] = _steps_from(r, geometry)
            item["tramos_trafico"] = _traffic_sections(r)
        out.append(item)
    if not out:
        raise TomTomError("TomTom no encontró ruta", 404)
    out.sort(key=lambda x: x["duration_live_min"])
    _routes.put(clave, out, ROUTE_TTL_S)
    return out


def _post(path: str, params: dict, payload: dict, timeout: float = 8) -> tuple[bytes, str]:
    if not configured():
        raise TomTomError("TomTom no está configurado", 503)
    q = {"key": settings.tomtom_api_key.strip(), **params}
    url = f"{BASE}{path}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"User-Agent": UA, "Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        msg = "TomTom rechazó la clave" if e.code in (401, 403) else f"TomTom respondió con error {e.code}"
        _remember_error(msg)
        raise TomTomError(msg, 429 if e.code == 429 else 502) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        _remember_error("No se pudo conectar con TomTom")
        raise TomTomError("No se pudo conectar con TomTom", 504) from None


def _thin(path: list[list[float]], max_points: int = 150) -> list[list[float]]:
    if len(path) <= max_points:
        return path
    paso = (len(path) - 1) / (max_points - 1)
    return [path[round(i * paso)] for i in range(max_points)]


def reassess(path: list[list[float]], avoid: list[tuple[float, float, float]] | None = None) -> dict:
    """Compara, con el tráfico de ahora, la ruta que se está siguiendo contra la más rápida.

    `path` va desde la posición actual hasta el destino. TomTom reconstruye esa ruta y propone
    una alternativa; devuelve {"actual_min": tiempo por la ruta de ahora, "mejor": ruta o None}.
    """
    if not configured():
        raise TomTomError("TomTom no está configurado", 503)
    if len(path) < 2:
        raise TomTomError("Ruta muy corta", 400)
    if route_left_today() <= 0:
        raise TomTomError("Se usó el cupo diario de rutas con tráfico", 429)
    dia = _today()
    if _route_day["dia"] != dia:
        _route_day.update(dia=dia, n=0)
    _route_day["n"] = int(_route_day["n"]) + 1

    puntos = _thin(path)
    params: dict[str, str | int | float] = {
        "traffic": "true",
        "computeTravelTimeFor": "all",
        "routeType": "fastest",
        "travelMode": "car",
        "maxAlternatives": 1,
        "minDeviationDistance": 0,
        "minDeviationTime": 0,
        "instructionsType": "text",
        "language": "es-ES",
        "sectionType": "traffic",
    }
    payload: dict = {"supportingPoints": [{"latitude": p[0], "longitude": p[1]} for p in puntos]}
    if avoid:
        payload["avoidAreas"] = {"rectangles": _avoid_rects(avoid)}
    a, b = puntos[0], puntos[-1]
    data, _ = _post(f"/routing/1/calculateRoute/{a[0]:.6f},{a[1]:.6f}:{b[0]:.6f},{b[1]:.6f}/json", params, payload, timeout=10)
    try:
        rutas = json.loads(data).get("routes", [])
    except ValueError:
        raise TomTomError("Respuesta inválida de TomTom") from None
    if not rutas:
        raise TomTomError("TomTom no encontró ruta", 404)
    actual = (rutas[0].get("summary", {}).get("travelTimeInSeconds") or 0) / 60
    mejor = None
    for r in rutas[1:]:
        geometry = [[p["latitude"], p["longitude"]] for leg in r.get("legs", []) for p in leg.get("points", [])]
        if len(geometry) < 2:
            continue
        s = r.get("summary", {})
        mejor = {
            "distance_km": round((s.get("lengthInMeters") or 0) / 1000, 2),
            "duration_min": round((s.get("noTrafficTravelTimeInSeconds") or s.get("travelTimeInSeconds") or 0) / 60, 1),
            "duration_live_min": round((s.get("travelTimeInSeconds") or 0) / 60, 1),
            "delay_min": round((s.get("trafficDelayInSeconds") or 0) / 60, 1),
            "geometry": geometry,
            "steps": _steps_from(r, geometry),
            "tramos_trafico": _traffic_sections(r),
            "fuente": "tomtom",
        }
        break
    return {"actual_min": round(actual, 1), "mejor": mejor}

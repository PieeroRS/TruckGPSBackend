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


def flow_tile(z: int, x: int, y: int) -> bytes:
    key = f"{z}/{x}/{y}"
    hit = _tiles.get(key)
    if hit is not None:
        return hit  # type: ignore[return-value]
    data, ctype = _fetch(
        f"/traffic/map/4/tile/flow/relative0/{z}/{x}/{y}.png",
        {"tileSize": 256, "thickness": 8 if z >= 13 else 5},
    )
    if "image" not in ctype:
        _remember_error("TomTom no devolvió una imagen de tráfico")
        raise TomTomError("TomTom no devolvió una imagen de tráfico")
    _tiles.put(key, data, TILE_TTL_S)
    _last_error["msg"] = None
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
        val = {"configurado": True, "funciona": True, "detalle": None}
    except TomTomError as e:
        val = {"configurado": True, "funciona": False, "detalle": str(e)}
    val["busquedasRestantesHoy"] = search_left_today()
    _status_cache.update(at=time.time(), val=val)
    return val

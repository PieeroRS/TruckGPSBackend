"""Utilidades geográficas: distancias, proyección sobre una ruta e interpolación.

Las rutas se guardan como listas [[lat, lng], ...]. Para distancias cortas se usa
una proyección equirectangular local (error < 0.5 % en Lima), suficiente para
detectar desvíos de unos cientos de metros.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_R = 6371000.0


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(min(1.0, a)))


def _xy(lat: float, lng: float, lat0: float) -> tuple[float, float]:
    kx = 111320.0 * math.cos(math.radians(lat0))
    return lng * kx, lat * 110540.0


def destination(lat: float, lng: float, bearing_deg: float, dist_m: float) -> tuple[float, float]:
    """Punto a `dist_m` metros de (lat, lng) en el rumbo dado (0 = norte, 90 = este)."""
    b = math.radians(bearing_deg)
    dlat = dist_m * math.cos(b) / 110540.0
    dlng = dist_m * math.sin(b) / (111320.0 * math.cos(math.radians(lat)))
    return lat + dlat, lng + dlng


def bearing(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    x1, y1 = _xy(lat1, lng1, lat1)
    x2, y2 = _xy(lat2, lng2, lat1)
    return (math.degrees(math.atan2(x2 - x1, y2 - y1)) + 360) % 360


@dataclass
class Projection:
    distance_m: float  # distancia del punto a la ruta
    along_m: float  # metros recorridos desde el inicio hasta el punto proyectado
    lat: float
    lng: float


def path_length_m(path: list[list[float]]) -> float:
    return sum(haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(path, path[1:]))


def project(path: list[list[float]], lat: float, lng: float) -> Projection | None:
    """Proyecta un punto sobre la ruta: distancia mínima y avance hasta ese punto."""
    if not path:
        return None
    if len(path) == 1:
        return Projection(haversine_m(lat, lng, path[0][0], path[0][1]), 0.0, path[0][0], path[0][1])
    px, py = _xy(lat, lng, lat)
    best: Projection | None = None
    acc = 0.0
    for a, b in zip(path, path[1:]):
        ax, ay = _xy(a[0], a[1], lat)
        bx, by = _xy(b[0], b[1], lat)
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
        qx, qy = ax + t * dx, ay + t * dy
        d = math.hypot(px - qx, py - qy)
        seg_len = math.sqrt(seg2)
        if best is None or d < best.distance_m:
            qlat = a[0] + t * (b[0] - a[0])
            qlng = a[1] + t * (b[1] - a[1])
            best = Projection(d, acc + t * seg_len, qlat, qlng)
        acc += seg_len
    return best


def point_at(path: list[list[float]], along_m: float) -> tuple[float, float, float]:
    """Punto a `along_m` metros desde el inicio de la ruta. Devuelve (lat, lng, rumbo)."""
    if not path:
        raise ValueError("Ruta vacía")
    if len(path) == 1 or along_m <= 0:
        b = bearing(path[0][0], path[0][1], path[1][0], path[1][1]) if len(path) > 1 else 0.0
        return path[0][0], path[0][1], b
    acc = 0.0
    for a, b in zip(path, path[1:]):
        seg = haversine_m(a[0], a[1], b[0], b[1])
        if seg > 0 and acc + seg >= along_m:
            t = (along_m - acc) / seg
            return a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]), bearing(a[0], a[1], b[0], b[1])
        acc += seg
    last, prev = path[-1], path[-2]
    return last[0], last[1], bearing(prev[0], prev[1], last[0], last[1])


def sub_path(path: list[list[float]], from_m: float, to_m: float) -> list[list[float]]:
    """Tramo de la ruta entre dos avances (en metros)."""
    out: list[list[float]] = []
    lat, lng, _ = point_at(path, from_m)
    out.append([lat, lng])
    acc = 0.0
    for a, b in zip(path, path[1:]):
        seg = haversine_m(a[0], a[1], b[0], b[1])
        if from_m < acc + seg <= to_m:
            out.append([b[0], b[1]])
        acc += seg
    lat, lng, _ = point_at(path, to_m)
    out.append([lat, lng])
    return out

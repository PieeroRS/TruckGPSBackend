"""Unidad de prueba SIM-001: se mueve sola por un circuito real del Callao y Lima.

La posición se calcula a partir de la hora (avance = velocidad × tiempo), así
que la unidad está "en movimiento" aunque nadie la mire: cada vez que la app
consulta vehículos, alertas o el panel, se registra la posición actual y corre
el motor de detección. Los escenarios (desvío, spoofing, jamming, salida de
zona, tráfico, sabotaje) alteran esa posición para probar cada regla.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.detection import (
    aware,
    check_signal_loss,
    get_state,
    ingest_position,
    record_event,
    set_state,
    upsert_alert,
)
from app.geo import destination, haversine_m, path_length_m, point_at, project, sub_path
from app.models import NavRoute, RiskZone, SimState, TrafficEvent, Vehicle, VehicleEvent, utcnow

log = logging.getLogger("truckgps.sim")

SIM_PLACA = "SIM-001"
LOOP_NOTE = "[circuito-sim]"
GEOFENCE_CODE = "sim-geocerca-operacion"
EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
LOOP_SPEED_KMH = 35.0
RECORD_EVERY_S = 8
TICK_EVERY_S = 4

_LOOP = json.loads((Path(__file__).resolve().parent / "data" / "sim_loop.json").read_text(encoding="utf-8"))
LOOP_GEOMETRY: list[list[float]] = _LOOP["geometry"]
LOOP_LEN_M = path_length_m(LOOP_GEOMETRY)
_lats = [p[0] for p in LOOP_GEOMETRY]
_lngs = [p[1] for p in LOOP_GEOMETRY]
GEOFENCE_CENTER = ((min(_lats) + max(_lats)) / 2, (min(_lngs) + max(_lngs)) / 2)
GEOFENCE_RADIUS_M = 4000

SCENARIOS = [
    {
        "tipo": "desvio",
        "titulo": "Desvío de ruta",
        "casos": ["CP011", "CP024"],
        "duracion_s": 240,
        "descripcion": "La unidad abandona su ruta activa a 60 km/h. A los ~15 s está a más de 250 m (alerta media) y al pasar 1 km se escala a alta. Prueba «Recalcular ruta» en la alerta.",
    },
    {
        "tipo": "spoofing",
        "titulo": "GPS spoofing",
        "casos": ["CP007"],
        "duracion_s": 120,
        "descripcion": "La posición reportada salta ~35 km de golpe (velocidad imposible). Se detecta en el siguiente reporte.",
    },
    {
        "tipo": "jamming",
        "titulo": "GPS jamming",
        "casos": ["CP013"],
        "duracion_s": 180,
        "descripcion": "La unidad deja de reportar posición mientras circula. A los 45 s sin señal se alerta posible inhibidor; al volver la señal queda registrado.",
    },
    {
        "tipo": "salida_zona",
        "titulo": "Salida de zona geográfica",
        "casos": ["CP015"],
        "duracion_s": 300,
        "descripcion": "La unidad sale de la geocerca de operación (4 km de radio alrededor del circuito) a 110 km/h y se detiene afuera.",
    },
    {
        "tipo": "trafico",
        "titulo": "Cambio de tráfico",
        "casos": ["CP022", "CP021"],
        "duracion_s": 1200,
        "descripcion": "Aparece una congestión 1.5 km adelante en la ruta. El sistema estima la demora, calcula una ruta alternativa y propone aplicarla (ajuste dinámico).",
    },
    {
        "tipo": "sabotaje",
        "titulo": "Sabotaje",
        "casos": ["CP016"],
        "duracion_s": 180,
        "descripcion": "Genera historial de desvíos y una detención en zona de riesgo, y luego corta la señal GPS. El motor suma los factores y alerta posible sabotaje.",
    },
]
SCENARIO_BY_TYPE = {s["tipo"]: s for s in SCENARIOS}

_last_tick = 0.0


# ─────────────── creación ───────────────

def get_sim_vehicle(db: Session) -> Vehicle | None:
    return db.scalar(select(Vehicle).where(Vehicle.placa == SIM_PLACA))


def _loop_route(db: Session, vehicle: Vehicle) -> NavRoute | None:
    return db.scalar(select(NavRoute).where(NavRoute.vehicle_id == vehicle.id, NavRoute.notes == LOOP_NOTE))


def ensure_simulation(db: Session) -> Vehicle:
    """Crea (o repara) la unidad simulada, su circuito, su geocerca y su estado."""
    v = get_sim_vehicle(db)
    if v is None:
        v = Vehicle(
            placa=SIM_PLACA,
            modelo="Unidad de prueba (simulada)",
            estado="ok",
            ruta=_LOOP["nombre"],
            kilometraje=50000,
            prox_mantenimiento="2027-01-15",
            lat=LOOP_GEOMETRY[0][0],
            lng=LOOP_GEOMETRY[0][1],
            velocidad=0,
            rendimiento_km_gal=9.0,
            tanque_gal=100,
            simulado=True,
        )
        db.add(v)
        db.flush()
    elif not v.simulado:
        v.simulado = True

    route = _loop_route(db, v)
    if route is None:
        route = NavRoute(
            vehicle_id=v.id,
            origin_name="Antepuerto del Callao (Av. Atalaya)",
            origin_lat=LOOP_GEOMETRY[0][0],
            origin_lng=LOOP_GEOMETRY[0][1],
            dest_name="Antepuerto del Callao (Av. Atalaya)",
            dest_lat=LOOP_GEOMETRY[-1][0],
            dest_lng=LOOP_GEOMETRY[-1][1],
            route_label=f"{_LOOP['nombre']} ({SIM_PLACA})",
            distance_km=round(LOOP_LEN_M / 1000, 2),
            duration_min=int(LOOP_LEN_M / 1000 / LOOP_SPEED_KMH * 60),
            geometry=LOOP_GEOMETRY,
            status="en-curso",
            progress_pct=0,
            notes=LOOP_NOTE,
            started_at=EPOCH,
        )
        db.add(route)
        db.flush()
    elif route.status != "en-curso":
        route.status = "en-curso"
        route.geometry = LOOP_GEOMETRY

    if db.scalar(select(RiskZone).where(RiskZone.codigo == GEOFENCE_CODE)) is None:
        db.add(
            RiskZone(
                codigo=GEOFENCE_CODE,
                nombre="Geocerca de operación – circuito de prueba",
                tipo="geocerca",
                alerta_en="salida",
                lat=round(GEOFENCE_CENTER[0], 5),
                lng=round(GEOFENCE_CENTER[1], 5),
                radio_m=GEOFENCE_RADIUS_M,
                activa=True,
                origen="usuario",
                notas="Zona permitida de la unidad de prueba: avisa si sale.",
            )
        )

    st = db.get(SimState, v.id)
    if st is None:
        db.add(
            SimState(
                vehicle_id=v.id,
                nav_route_id=route.id,
                anchor_at=EPOCH,
                offset_m=0.0,
                velocidad_kmh=LOOP_SPEED_KMH,
                ciclica=True,
                escenario="normal",
                saltar_chequeo=True,
            )
        )
    db.flush()
    return v


# ─────────────── posición ───────────────

def _route_for_state(db: Session, st: SimState) -> NavRoute | None:
    return db.get(NavRoute, st.nav_route_id) if st.nav_route_id else None


def base_position(db: Session, st: SimState, now: datetime) -> dict:
    """Posición sobre la ruta asignada, sin escenarios."""
    route = _route_for_state(db, st)
    geom = route.geometry if route and route.geometry and len(route.geometry) >= 2 else LOOP_GEOMETRY
    total = path_length_m(geom)
    elapsed = max(0.0, (now - aware(st.anchor_at)).total_seconds())
    along = st.offset_m + st.velocidad_kmh / 3.6 * elapsed
    arrived = False
    if st.ciclica:
        along = along % total
    elif along >= total:
        along, arrived = total, True
    lat, lng, rumbo = point_at(geom, along)
    # Pequeña variación de velocidad para que se vea realista (la posición usa la media).
    vel = 0 if arrived else max(5, round(st.velocidad_kmh + 7 * ((int(elapsed / 20) % 5) - 2) / 2))
    return {"lat": lat, "lng": lng, "rumbo": rumbo, "along": along, "total": total, "vel": vel, "arrived": arrived, "route": route}


def current_position(db: Session, st: SimState, now: datetime) -> dict:
    """Posición a reportar ahora, aplicando el escenario activo. report=False si no hay señal."""
    base = base_position(db, st, now)
    datos = st.escenario_datos or {}
    esc = st.escenario
    t = max(0.0, (now - aware(st.escenario_inicio)).total_seconds()) if st.escenario_inicio else 0.0
    if esc in ("desvio", "salida_zona"):
        kmh = 60.0 if esc == "desvio" else 110.0
        dist = min(kmh / 3.6 * t, datos.get("max_m", 4000))
        lat, lng = destination(datos["inicio"][0], datos["inicio"][1], datos["rumbo"], dist)
        moving = dist < datos.get("max_m", 4000)
        return {**base, "lat": lat, "lng": lng, "vel": int(kmh) if moving else 0, "report": True}
    if esc == "spoofing":
        lat, lng = destination(base["lat"], base["lng"], datos.get("rumbo", 160), datos.get("salto_m", 35000))
        return {**base, "lat": lat, "lng": lng, "report": True}
    if esc in ("jamming", "sabotaje"):
        return {**base, "report": False}
    if esc == "retorno":
        puente = datos["puente"]
        largo = path_length_m(puente)
        d = st.velocidad_kmh / 3.6 * t
        if d >= largo:
            return {**base, "report": True, "fin_retorno": True}
        lat, lng, rumbo = point_at(puente, d)
        return {**base, "lat": lat, "lng": lng, "rumbo": rumbo, "report": True}
    return {**base, "report": True}


# ─────────────── control de escenarios ───────────────

def _reanchor(st: SimState, along: float, now: datetime) -> None:
    st.anchor_at = now
    st.offset_m = along


def end_scenario(db: Session, st: SimState, now: datetime, motivo: str = "fin") -> None:
    if st.escenario == "normal":
        return
    v = db.get(Vehicle, st.vehicle_id)
    if st.escenario == "retorno":
        # Retoma la ruta en el punto donde se unió el tramo de retorno.
        rejoin = (st.escenario_datos or {}).get("rejoin_m", 0.0)
        _reanchor(st, rejoin, now)
    elif st.escenario in ("salida_zona", "desvio", "spoofing"):
        st.saltar_chequeo = True  # vuelve a su lugar en la ruta sin falsa alarma de spoofing
    if v and st.escenario in ("salida_zona", "desvio", "spoofing", "jamming", "sabotaje"):
        record_event(db, v, "simulacion", "low", f"Fin del escenario «{SCENARIO_BY_TYPE.get(st.escenario, {}).get('titulo', st.escenario)}» ({motivo}).")
    st.escenario = "normal"
    st.escenario_inicio = None
    st.escenario_fin = None
    st.escenario_datos = None


def _generate_history(db: Session, v: Vehicle, now: datetime) -> int:
    """Historial simulado de las últimas horas: 2 desvíos y 1 detención en zona de riesgo."""
    previos = [
        ("desvio", "medium", 8 * 60, "Desvío de 620 m de la ruta en Av. Argentina (historial simulado)."),
        ("desvio", "medium", 3 * 60, "Desvío de 480 m de la ruta cerca del Antepuerto (historial simulado)."),
        ("parada_riesgo", "medium", 2 * 60, "Detención de 6 min dentro de «Antepuerto del Callao (Av. Atalaya)» (historial simulado)."),
    ]
    for tipo, sev, mins, desc in previos:
        lat, lng, _ = point_at(LOOP_GEOMETRY, (mins * 97) % LOOP_LEN_M)
        record_event(db, v, tipo, sev, desc, lat, lng, {"historial_simulado": True}, when=now - timedelta(minutes=mins))
    return len(previos)


def start_scenario(db: Session, tipo: str, duracion_s: int | None = None, now: datetime | None = None) -> dict:
    from app.models import SimState as _S  # noqa: F401

    if tipo not in SCENARIO_BY_TYPE:
        raise ValueError("Escenario desconocido")
    now = now or utcnow()
    v = ensure_simulation(db)
    st = db.get(SimState, v.id)
    # Registrar la posición actual antes de cambiar el comportamiento.
    advance(db, v, st, now, force=True)
    if st.escenario != "normal":
        end_scenario(db, st, now, "reemplazado por otro escenario")
    base = base_position(db, st, now)
    meta = SCENARIO_BY_TYPE[tipo]
    dur = duracion_s or meta["duracion_s"]
    datos: dict = {}
    info = ""
    if tipo == "desvio":
        # Perpendicular a la ruta, hacia afuera del circuito (para no acercarse a otro tramo).
        lados = [(base["rumbo"] + 90) % 360, (base["rumbo"] + 270) % 360]
        rumbo = max(
            lados,
            key=lambda b: haversine_m(GEOFENCE_CENTER[0], GEOFENCE_CENTER[1], *destination(base["lat"], base["lng"], b, 1000)),
        )
        datos = {"inicio": [base["lat"], base["lng"]], "rumbo": rumbo, "max_m": 3000}
        info = "Se alejará de la ruta a 60 km/h."
    elif tipo == "salida_zona":
        from app.geo import bearing

        rumbo = bearing(GEOFENCE_CENTER[0], GEOFENCE_CENTER[1], base["lat"], base["lng"])
        restante = GEOFENCE_RADIUS_M - haversine_m(GEOFENCE_CENTER[0], GEOFENCE_CENTER[1], base["lat"], base["lng"])
        datos = {"inicio": [base["lat"], base["lng"]], "rumbo": rumbo, "max_m": max(0, restante) + 900}
        info = f"Saldrá de la geocerca en ~{max(10, int(restante / (110 / 3.6)))} s."
    elif tipo == "spoofing":
        datos = {"rumbo": 160, "salto_m": 35000}
        info = "Las posiciones aparecerán ~35 km al sur (cerca de Lurín/Pachacámac)."
    elif tipo == "jamming":
        info = "Sin reportes; la alerta llega a los 45 s."
    elif tipo == "sabotaje":
        n = _generate_history(db, v, now)
        info = f"Se generaron {n} eventos de historial; se corta la señal GPS y la alerta de sabotaje llega con la de jamming (~45 s)."
    elif tipo == "trafico":
        res = simulate_traffic(db, v, st, base, now, dur)
        return {"escenario": "trafico", "info": res["info"], "alerta_id": res.get("alerta_id")}

    st.escenario = tipo
    st.escenario_inicio = now
    st.escenario_fin = now + timedelta(seconds=dur)
    st.escenario_datos = datos
    record_event(db, v, "simulacion", "low", f"Inicio del escenario «{meta['titulo']}». {info}")
    db.flush()
    return {"escenario": tipo, "info": info, "fin": st.escenario_fin.isoformat()}


def reset_simulation(db: Session, now: datetime | None = None) -> None:
    now = now or utcnow()
    v = ensure_simulation(db)
    st = db.get(SimState, v.id)
    end_scenario(db, st, now, "reinicio")
    loop = _loop_route(db, v)
    for r in db.scalars(select(NavRoute).where(NavRoute.vehicle_id == v.id, NavRoute.status == "en-curso")).all():
        if r.id != loop.id:
            r.status = "asignada"
    loop.status = "en-curso"
    loop.geometry = LOOP_GEOMETRY
    st.nav_route_id = loop.id
    st.ciclica = True
    st.velocidad_kmh = LOOP_SPEED_KMH
    st.anchor_at = EPOCH
    st.offset_m = 0.0
    st.saltar_chequeo = True
    for clave in ("desvio", "senal", "parada"):
        set_state(db, v.id, clave, None)
    # Historial limpio para que una prueba no contamine la siguiente (p. ej. el puntaje de sabotaje).
    from sqlalchemy import delete

    from app.models import AiAlert, VehiclePosition, ZoneState

    db.execute(delete(VehicleEvent).where(VehicleEvent.vehicle_id == v.id))
    db.execute(delete(VehiclePosition).where(VehiclePosition.vehicle_id == v.id))
    db.execute(delete(ZoneState).where(ZoneState.vehicle_id == v.id))
    for a in db.scalars(select(AiAlert).where(AiAlert.vehicle_id == v.id, AiAlert.estado.in_(("activa", "en_atencion")))).all():
        a.estado = "resuelta"
        a.activa = False
    for ev in db.scalars(select(TrafficEvent).where(TrafficEvent.simulado.is_(True), TrafficEvent.activo.is_(True))).all():
        ev.activo = False
    v.estado = "ok"
    db.flush()


# ─────────────── rutas asignadas a la unidad simulada ───────────────

def follow_route(db: Session, vehicle: Vehicle, route: NavRoute, now: datetime | None = None) -> None:
    """La unidad simulada empieza a recorrer una ruta asignada (desde su inicio)."""
    now = now or utcnow()
    st = db.get(SimState, vehicle.id)
    if st is None:
        return
    end_scenario(db, st, now, "nueva ruta")
    st.nav_route_id = route.id
    st.ciclica = route.notes == LOOP_NOTE
    if route.distance_km and route.duration_min:
        st.velocidad_kmh = max(25.0, min(80.0, route.distance_km / (route.duration_min / 60)))
    else:
        st.velocidad_kmh = 45.0
    st.anchor_at = now
    st.offset_m = 0.0
    st.saltar_chequeo = True
    loop = _loop_route(db, vehicle)
    if loop and loop.id != route.id and loop.status == "en-curso":
        loop.status = "asignada"
    set_state(db, vehicle.id, "desvio", None)


def back_to_loop(db: Session, vehicle: Vehicle, now: datetime) -> None:
    st = db.get(SimState, vehicle.id)
    loop = _loop_route(db, vehicle)
    if not st or not loop:
        return
    loop.status = "en-curso"
    st.nav_route_id = loop.id
    st.ciclica = True
    st.velocidad_kmh = LOOP_SPEED_KMH
    st.anchor_at = EPOCH
    st.offset_m = 0.0
    st.saltar_chequeo = True


def reroute_simulated(db: Session, vehicle: Vehicle, puente: list[list[float]], rejoin_m: float | None, now: datetime) -> None:
    """Hace que la unidad simulada siga un tramo nuevo (recalculado o alternativo)."""
    st = db.get(SimState, vehicle.id)
    if st is None:
        return
    if st.escenario not in ("normal", "retorno"):
        st.escenario = "normal"
    if st.ciclica and rejoin_m is not None:
        st.escenario = "retorno"
        st.escenario_inicio = now
        st.escenario_fin = now + timedelta(hours=2)
        st.escenario_datos = {"puente": puente, "rejoin_m": rejoin_m}
    else:
        st.escenario = "normal"
        st.escenario_inicio = st.escenario_fin = None
        st.escenario_datos = None
        st.anchor_at = now
        st.offset_m = 0.0
    st.saltar_chequeo = True


# ─────────────── tráfico ───────────────

def simulate_traffic(db: Session, v: Vehicle, st: SimState, base: dict, now: datetime, dur: int) -> dict:
    """Crea una congestión 1.5 km adelante en la ruta y evalúa el ajuste dinámico."""
    geom = base["route"].geometry if base["route"] and base["route"].geometry else LOOP_GEOMETRY
    total = base["total"]
    ahead = base["along"] + 1500
    if st.ciclica:
        ahead %= total
    else:
        ahead = min(ahead, total - 50)
    lat, lng, _ = point_at(geom, ahead)
    ev = TrafficEvent(
        descripcion=f"Congestión simulada en la ruta de {v.placa}",
        nivel="congestionado",
        lat=round(lat, 5),
        lng=round(lng, 5),
        radio_m=350,
        simulado=True,
        activo=True,
        expira_at=now + timedelta(seconds=dur),
    )
    db.add(ev)
    db.flush()
    record_event(db, v, "simulacion", "low", "Inicio del escenario «Cambio de tráfico»: congestión 1.5 km adelante.")
    alert = evaluate_traffic(db, v, now)
    info = "Congestión creada 1.5 km adelante."
    if alert and (alert.payload or {}).get("alternativa"):
        alt = alert.payload["alternativa"]
        info += f" Hay una alternativa que ahorra ~{alt['ahorro_min']:.0f} min: revisa la alerta y pulsa «Aplicar ruta alternativa»."
    elif alert:
        info += " No se encontró una alternativa mejor; la alerta propone mantener la ruta."
    return {"info": info, "alerta_id": alert.id if alert else None}


def evaluate_traffic(db: Session, vehicle: Vehicle, now: datetime | None = None) -> object | None:
    """Si la ruta activa atraviesa un evento de tráfico, estima la demora y busca una alternativa."""
    from app.detection import active_route
    from app.routing import EVENT_DELAY_MIN, RoutingError, estimate, events_on_path, osrm_routes, detour_alternative

    now = now or utcnow()
    route = active_route(db, vehicle)
    if route is None or not route.geometry:
        return None
    events = db.scalars(select(TrafficEvent).where(TrafficEvent.activo.is_(True))).all()
    geom = route.geometry
    pr = project(geom, vehicle.lat, vehicle.lng)
    along = pr.along_m if pr else 0.0
    total = path_length_m(geom)
    st = db.get(SimState, vehicle.id) if vehicle.simulado else None
    ciclica = bool(st and st.ciclica)

    # Tramo que falta (en el circuito: hasta 1 km después del evento, donde se retoma).
    hits_all = events_on_path(geom, events)
    if not hits_all:
        return None
    ev = hits_all[0]
    ev_along = project(geom, ev.lat, ev.lng).along_m
    if ciclica:
        if ev_along < along:
            ev_along += total
        rejoin = ev_along + ev.radio_m + 800
        target_along = rejoin % total
        tramo = sub_path(geom, along, min(rejoin, total)) if rejoin <= total else sub_path(geom, along, total) + sub_path(geom, 0, rejoin - total)
    else:
        if ev_along < along:
            return None
        target_along = None
        tramo = sub_path(geom, along, total)
    target = tramo[-1]
    tramo_km = path_length_m(tramo) / 1000
    vel = (st.velocidad_kmh if st else 40.0) or 40.0
    actual = estimate({"distance_km": round(tramo_km, 2), "duration_min": round(tramo_km / vel * 60, 1), "geometry": tramo}, events)

    alternativa = None
    try:
        cands = osrm_routes([(vehicle.lat, vehicle.lng), (target[0], target[1])], alternatives=3)
        cands += detour_alternative((vehicle.lat, vehicle.lng), (target[0], target[1]), (ev.lat, ev.lng))
        mejores = [estimate(c, events) for c in cands if not events_on_path(c["geometry"], [ev])]
        mejores.sort(key=lambda c: c["duration_traffic_min"])
        if mejores and mejores[0]["duration_traffic_min"] < actual["duration_traffic_min"] - 1:
            m = mejores[0]
            alternativa = {
                "geometry": m["geometry"],
                "distance_km": m["distance_km"],
                "duration_traffic_min": m["duration_traffic_min"],
                "ahorro_min": round(actual["duration_traffic_min"] - m["duration_traffic_min"], 1),
                "rejoin_m": target_along,
            }
    except RoutingError:
        pass

    demora = EVENT_DELAY_MIN.get(ev.nivel, 10)
    desc = (
        f"{ev.descripcion}: la ruta «{route.route_label}» pasa por la zona afectada (+{demora} min estimados). "
        + (
            f"Alternativa disponible: {alternativa['distance_km']} km, ~{alternativa['duration_traffic_min']:.0f} min, ahorra ~{alternativa['ahorro_min']:.0f} min."
            if alternativa
            else "No se encontró una alternativa más rápida."
        )
    )
    return upsert_alert(
        db,
        vehicle,
        "trafico",
        "medium",
        f"Cambio de tráfico en la ruta de {vehicle.placa}",
        desc,
        ev.lat,
        ev.lng,
        {
            "evento_id": ev.id,
            "demora_min": demora,
            "tiempo_actual_min": actual["duration_traffic_min"],
            "alternativa": alternativa,
        },
        icon="route",
        nav_route_id=route.id,
    )


# ─────────────── avance periódico ───────────────

def advance(db: Session, v: Vehicle, st: SimState, now: datetime, force: bool = False) -> None:
    if st.escenario_fin and now >= aware(st.escenario_fin):
        end_scenario(db, st, now, "tiempo cumplido")
    pos = current_position(db, st, now)
    if pos.get("fin_retorno"):
        end_scenario(db, st, now, "retomó la ruta")
        pos = current_position(db, st, now)
    if pos.get("arrived") and not st.ciclica and pos["route"] is not None:
        pos["route"].status = "completada"
        pos["route"].progress_pct = 100
        pos["route"].completed_at = now
        record_event(db, v, "ruta_completada", "low", f"{v.placa} completó la ruta «{pos['route'].route_label}».")
        back_to_loop(db, v, now)
        pos = current_position(db, st, now)
    if not pos["report"]:
        return
    last = aware(st.ultimo_registro)
    if not force and last and (now - last).total_seconds() < RECORD_EVERY_S:
        return
    skip = st.saltar_chequeo
    st.saltar_chequeo = False
    st.ultimo_registro = now
    ingest_position(db, v, round(pos["lat"], 6), round(pos["lng"], 6), pos["vel"], now, skip_jump=skip)


def tick(db: Session, force: bool = False) -> None:
    """Avanza la simulación y revisa pérdidas de señal. Se llama al consultar datos."""
    global _last_tick
    mono = time.monotonic()
    if not force and mono - _last_tick < TICK_EVERY_S:
        return
    _last_tick = mono
    now = utcnow()
    try:
        with db.begin_nested():
            v = ensure_simulation(db)
            st = db.get(SimState, v.id)
            for ev in db.scalars(select(TrafficEvent).where(TrafficEvent.activo.is_(True))).all():
                if ev.expira_at and now >= aware(ev.expira_at):
                    ev.activo = False
            advance(db, v, st, now)
            check_signal_loss(db, now)
    except Exception:  # noqa: BLE001 - la simulación nunca debe romper la consulta principal
        log.exception("Error en la simulación")


def sim_snapshot(db: Session) -> dict:
    """Estado del simulador para la pantalla de pruebas."""
    v = ensure_simulation(db)
    st = db.get(SimState, v.id)
    now = utcnow()
    route = _route_for_state(db, st)
    restante = int((aware(st.escenario_fin) - now).total_seconds()) if st.escenario_fin else None
    puente = (st.escenario_datos or {}).get("puente") if st.escenario == "retorno" else None
    eventos = db.scalars(
        select(VehicleEvent).where(VehicleEvent.vehicle_id == v.id).order_by(VehicleEvent.created_at.desc()).limit(40)
    ).all()
    return {
        "vehicle": v,
        "state": st,
        "route": route,
        "restante_s": max(0, restante) if restante is not None and st.escenario not in ("normal", "retorno") else None,
        "puente": puente,
        "eventos": eventos,
        "senal_perdida": bool(get_state(db, v.id, "senal").get("perdida")),
        "desviado": bool(get_state(db, v.id, "desvio").get("activo")),
    }

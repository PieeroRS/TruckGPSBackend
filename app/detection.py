"""Motor de detección de anomalías de telemetría.

Reglas (para vehículos reales y para la unidad simulada):
- Spoofing: salto de posición imposible (> 2 km a más de 200 km/h).
- Desvío de ruta: más de 250 m fuera de la ruta activa; se escala a alta al superar 1 km.
- Pérdida de señal (posible jamming): una unidad en ruta que circulaba deja de reportar.
- Detención en zona de riesgo: más de 2 min detenida dentro de una zona peligrosa.
- Salida de geocerca: se maneja en routers/zones.check_zones.
- Sabotaje: puntaje ponderado de los eventos de las últimas 24 h (no es un modelo
  de aprendizaje automático; son reglas con pesos, explicables).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.geo import haversine_m, path_length_m, project
from app.models import (
    AiAlert,
    DetectionState,
    NavRoute,
    RiskZone,
    Vehicle,
    VehicleEvent,
    VehiclePosition,
    ZoneState,
    utcnow,
)

SEV_ORDER = {"low": 0, "medium": 1, "high": 2}
LIMA = timezone(timedelta(hours=-5))

SPOOF_MIN_JUMP_M = 2000
# Con una precisión peor que esto (ubicación por WiFi/IP) no se evalúan desvíos ni paradas,
# y con peor que COARSE_ZONES_M tampoco zonas: un punto impreciso daría falsas alarmas.
COARSE_RULES_M = 300
COARSE_ZONES_M = 500
SPOOF_MAX_KMH = 200
DEVIATION_M = 250
DEVIATION_HIGH_M = 1000
BACK_ON_ROUTE_M = 150
SIGNAL_LOSS_S_REAL = 300
SIGNAL_LOSS_S_SIM = 45
SIGNAL_LOSS_MAX_AGE_S = 1800
STOP_IN_RISK_S = 120
RISK_ZONE_TYPES = {"robo", "homicidio", "extorsion", "riesgo", "peligro_vial"}

SABOTAGE_WEIGHTS = {"desvio": 25, "spoofing": 35, "jamming": 35, "parada_riesgo": 20, "salida_zona": 20}
SABOTAGE_THRESHOLD = 60
EVENT_LABELS = {
    "desvio": "desvío de ruta",
    "spoofing": "posible GPS spoofing",
    "jamming": "pérdida de señal GPS",
    "parada_riesgo": "detención en zona de riesgo",
    "salida_zona": "salida de geocerca",
}


def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def lima_hhmm(dt: datetime) -> str:
    return aware(dt).astimezone(LIMA).strftime("%d/%m %H:%M")


# ─────────────── estado y eventos ───────────────

def get_state(db: Session, vehicle_id: str, clave: str) -> dict:
    row = db.get(DetectionState, (vehicle_id, clave))
    return dict(row.valor or {}) if row else {}


def set_state(db: Session, vehicle_id: str, clave: str, valor: dict | None) -> None:
    row = db.get(DetectionState, (vehicle_id, clave))
    if valor is None:
        if row:
            db.delete(row)
        return
    if row is None:
        row = DetectionState(vehicle_id=vehicle_id, clave=clave)
        db.add(row)
    row.valor = valor
    row.updated_at = utcnow()


def record_event(db: Session, vehicle: Vehicle, tipo: str, severidad: str, descripcion: str, lat=None, lng=None, datos=None, when=None) -> VehicleEvent:
    ev = VehicleEvent(
        vehicle_id=vehicle.id,
        tipo=tipo,
        severidad=severidad,
        descripcion=descripcion,
        lat=lat,
        lng=lng,
        datos=datos,
        simulado=bool(vehicle.simulado),
        created_at=when or utcnow(),
    )
    db.add(ev)
    db.flush()
    return ev


def add_history(alert: AiAlert, codigo: str, label: str, resultado: str, usuario: str = "Sistema", nota: str | None = None) -> None:
    hist = list(alert.historial or [])
    hist.append(
        {
            "codigo": codigo,
            "label": label,
            "usuario": usuario,
            "fecha": utcnow().isoformat(),
            "resultado": resultado,
            **({"nota": nota} if nota else {}),
        }
    )
    alert.historial = hist
    alert.updated_at = utcnow()


def active_alert(db: Session, vehicle_id: str, tipo: str) -> AiAlert | None:
    return db.scalar(
        select(AiAlert)
        .where(AiAlert.vehicle_id == vehicle_id, AiAlert.tipo == tipo, AiAlert.estado.in_(("activa", "en_atencion")))
        .order_by(AiAlert.created_at.desc())
    )


def _notify(db: Session, alert: AiAlert, vehicle: Vehicle) -> None:
    from app.notify import Alert, send_alert

    prefijo = "[SIMULACIÓN] " if vehicle.simulado else ""
    conductor = vehicle.driver.full_name if vehicle.driver else "Sin conductor asignado"
    send_alert(
        db,
        Alert(
            tipo="motor",
            titulo=prefijo + alert.titulo,
            lineas=[alert.descripcion, f"Unidad: {vehicle.placa} · Conductor: {conductor}", f"Severidad: {alert.severidad}"],
            lat=alert.lat,
            lng=alert.lng,
            vehicle_id=vehicle.id,
            path="/dashboard",
        ),
    )


def upsert_alert(
    db: Session,
    vehicle: Vehicle,
    tipo: str,
    severidad: str,
    titulo: str,
    descripcion: str,
    lat=None,
    lng=None,
    payload: dict | None = None,
    icon: str = "alert-triangle",
    nav_route_id: str | None = None,
) -> AiAlert:
    """Crea la alerta, o actualiza la que ya está activa para esa unidad y tipo (sin duplicar)."""
    alert = active_alert(db, vehicle.id, tipo)
    escalated = False
    if alert is None:
        alert = AiAlert(
            titulo=titulo,
            descripcion=descripcion,
            severidad=severidad,
            icon=icon,
            placas_afectadas=[vehicle.placa],
            activa=True,
            tipo=tipo,
            origen="simulacion" if vehicle.simulado else "motor",
            estado="activa",
            vehicle_id=vehicle.id,
            nav_route_id=nav_route_id,
            lat=lat,
            lng=lng,
            payload=payload or {},
            historial=[],
        )
        db.add(alert)
        add_history(alert, "detectada", "Detectada", descripcion)
        escalated = severidad == "high"
    else:
        if SEV_ORDER.get(severidad, 0) > SEV_ORDER.get(alert.severidad, 0):
            alert.severidad = severidad
            escalated = severidad == "high"
            add_history(alert, "escalada", "Escalada", f"Severidad elevada a {severidad}: {descripcion}")
        alert.titulo = titulo
        alert.descripcion = descripcion
        alert.lat, alert.lng = lat, lng
        alert.payload = {**(alert.payload or {}), **(payload or {})}
        if nav_route_id:
            alert.nav_route_id = nav_route_id
        alert.updated_at = utcnow()
    db.flush()
    if escalated:
        _notify(db, alert, vehicle)
    return alert


# ─────────────── reglas ───────────────

def active_route(db: Session, vehicle: Vehicle) -> NavRoute | None:
    return db.scalar(
        select(NavRoute)
        .where(NavRoute.vehicle_id == vehicle.id, NavRoute.status == "en-curso")
        .order_by(NavRoute.started_at.desc().nullslast(), NavRoute.created_at.desc())
    )


def route_geometry(db: Session, vehicle: Vehicle, route: NavRoute | None) -> list[list[float]] | None:
    """Geometría contra la que se mide el desvío (en la unidad simulada puede ser un tramo de retorno)."""
    if vehicle.simulado:
        from app.models import SimState

        st = db.get(SimState, vehicle.id)
        if st and st.escenario == "retorno" and (st.escenario_datos or {}).get("puente"):
            return st.escenario_datos["puente"]
    if route and route.geometry and len(route.geometry) >= 2:
        return route.geometry
    return None


def check_spoofing(
    db: Session, vehicle: Vehicle, prev: VehiclePosition | None, lat: float, lng: float, now: datetime, precision_m: float | None = None
) -> bool:
    if prev is None:
        return False
    dt = (now - aware(prev.recorded_at)).total_seconds()
    if dt <= 0:
        return False
    dist = haversine_m(prev.lat, prev.lng, lat, lng)
    # Un salto explicable por la imprecisión de los dos puntos no es spoofing
    # (p. ej. pasar de ubicación aproximada por IP a GPS).
    incertidumbre = (prev.precision_m or 0) + (precision_m or 0)
    if incertidumbre > COARSE_ZONES_M or dist - incertidumbre < SPOOF_MIN_JUMP_M:
        return False
    kmh = dist / dt * 3.6
    if kmh < SPOOF_MAX_KMH:
        return False
    desc = (
        f"La posición de {vehicle.placa} saltó {dist / 1000:.1f} km en {dt:.0f} s "
        f"(velocidad implícita de {kmh:,.0f} km/h, imposible para un camión). La señal GPS podría estar siendo falsificada."
    )
    record_event(db, vehicle, "spoofing", "high", desc, lat, lng, {"salto_km": round(dist / 1000, 2), "segundos": round(dt), "kmh": round(kmh)})
    upsert_alert(
        db,
        vehicle,
        "spoofing",
        "high",
        f"Posible GPS spoofing en {vehicle.placa}",
        desc,
        lat,
        lng,
        {"salto_km": round(dist / 1000, 2), "kmh": round(kmh), "ultima_confiable": [prev.lat, prev.lng]},
        icon="shield-alert",
    )
    vehicle.estado = "alert"
    return True


def check_signal_recovered(db: Session, vehicle: Vehicle, lat: float, lng: float, now: datetime) -> None:
    st = get_state(db, vehicle.id, "senal")
    if not st.get("perdida"):
        return
    desde = datetime.fromisoformat(st["desde"])
    secs = (now - aware(desde)).total_seconds()
    record_event(db, vehicle, "senal_recuperada", "low", f"Señal GPS recuperada tras {secs:.0f} s sin reportes.", lat, lng)
    alert = active_alert(db, vehicle.id, "jamming")
    if alert:
        add_history(alert, "senal_recuperada", "Señal recuperada", f"La unidad volvió a reportar tras {secs:.0f} s.")
    set_state(db, vehicle.id, "senal", None)


def check_route_deviation(db: Session, vehicle: Vehicle, lat: float, lng: float) -> None:
    route = active_route(db, vehicle)
    geom = route_geometry(db, vehicle, route)
    if not route or not geom:
        set_state(db, vehicle.id, "desvio", None)
        return
    pr = project(geom, lat, lng)
    if pr is None:
        return
    st = get_state(db, vehicle.id, "desvio")
    dist = pr.distance_m
    if dist > DEVIATION_M:
        sev = "high" if dist >= DEVIATION_HIGH_M else "medium"
        desc = (
            f"{vehicle.placa} está a {dist:,.0f} m de su ruta «{route.route_label}». "
            + ("Supera 1 km: posible cambio de destino no autorizado." if sev == "high" else "Se salió del recorrido planificado.")
        )
        first = not st.get("activo")
        crossed = st.get("max_m", 0) < DEVIATION_HIGH_M <= dist
        salida = st.get("salida") or [round(pr.lat, 6), round(pr.lng, 6)]
        if first:
            record_event(db, vehicle, "desvio", sev, desc, lat, lng, {"distancia_m": round(dist), "ruta": route.route_label})
        if first or crossed or st.get("sev") != sev:
            upsert_alert(
                db,
                vehicle,
                "desvio",
                sev,
                f"Desvío de ruta: {vehicle.placa}",
                desc,
                lat,
                lng,
                {"distancia_m": round(dist), "ruta": route.route_label, "salida": salida},
                icon="route",
                nav_route_id=route.id,
            )
        set_state(db, vehicle.id, "desvio", {"activo": True, "max_m": max(dist, st.get("max_m", 0)), "sev": sev, "salida": salida})
    else:
        if st.get("activo") and dist < BACK_ON_ROUTE_M:
            record_event(db, vehicle, "regreso_ruta", "low", f"{vehicle.placa} volvió a su ruta.", lat, lng)
            alert = active_alert(db, vehicle.id, "desvio")
            if alert:
                add_history(alert, "regreso_ruta", "Volvió a la ruta", "La unidad está nuevamente sobre la ruta planificada.")
            set_state(db, vehicle.id, "desvio", None)
        total = path_length_m(geom)
        if total > 0 and route.status == "en-curso":
            route.progress_pct = max(0, min(99, int(pr.along_m / total * 100)))


def check_stop_in_risk_zone(db: Session, vehicle: Vehicle, speed: float, lat: float, lng: float, now: datetime) -> None:
    inside_ids = [
        s.zone_id
        for s in db.scalars(select(ZoneState).where(ZoneState.vehicle_id == vehicle.id, ZoneState.inside.is_(True))).all()
    ]
    zone = None
    if inside_ids:
        zone = db.scalar(
            select(RiskZone).where(RiskZone.id.in_(inside_ids), RiskZone.tipo.in_(RISK_ZONE_TYPES), RiskZone.activa.is_(True))
        )
    st = get_state(db, vehicle.id, "parada")
    if speed >= 3 or zone is None:
        if st:
            set_state(db, vehicle.id, "parada", None)
        return
    if not st or st.get("zone") != zone.id:
        set_state(db, vehicle.id, "parada", {"zone": zone.id, "desde": now.isoformat(), "alertada": False})
        return
    secs = (now - aware(datetime.fromisoformat(st["desde"]))).total_seconds()
    if secs >= STOP_IN_RISK_S and not st.get("alertada"):
        desc = f"{vehicle.placa} lleva {secs / 60:.0f} min detenida dentro de «{zone.nombre}»."
        record_event(db, vehicle, "parada_riesgo", "medium", desc, lat, lng, {"zona": zone.nombre})
        upsert_alert(db, vehicle, "parada_riesgo", "medium", f"Detención en zona de riesgo: {vehicle.placa}", desc, lat, lng, {"zona": zone.nombre}, icon="map-pin")
        st["alertada"] = True
        set_state(db, vehicle.id, "parada", st)
        evaluate_sabotage(db, vehicle)


def evaluate_sabotage(db: Session, vehicle: Vehicle) -> AiAlert | None:
    """Suma ponderada de eventos de las últimas 24 h. Desde 60 puntos alerta posible sabotaje."""
    since = utcnow() - timedelta(hours=24)
    rows = db.execute(
        select(VehicleEvent.tipo, func.count())
        .where(VehicleEvent.vehicle_id == vehicle.id, VehicleEvent.created_at >= since, VehicleEvent.tipo.in_(list(SABOTAGE_WEIGHTS)))
        .group_by(VehicleEvent.tipo)
    ).all()
    factores = []
    score = 0
    for tipo, n in rows:
        pts = SABOTAGE_WEIGHTS[tipo] * min(n, 2)
        score += pts
        factores.append({"tipo": tipo, "label": EVENT_LABELS[tipo], "cantidad": n, "puntos": pts})
    score = min(100, score)
    if score < SABOTAGE_THRESHOLD:
        return None
    factores.sort(key=lambda f: -f["puntos"])
    detalle = ", ".join(f"{f['cantidad']} {f['label']}" for f in factores)
    desc = (
        f"Puntaje de riesgo {score}/100 en las últimas 24 h ({detalle}). "
        "La combinación de eventos coincide con un patrón de sabotaje o robo de la unidad."
    )
    last = db.scalar(select(VehiclePosition).where(VehiclePosition.vehicle_id == vehicle.id).order_by(VehiclePosition.recorded_at.desc()))
    return upsert_alert(
        db,
        vehicle,
        "sabotaje",
        "high",
        f"Posible sabotaje o robo: {vehicle.placa}",
        desc,
        last.lat if last else vehicle.lat,
        last.lng if last else vehicle.lng,
        {"puntaje": score, "umbral": SABOTAGE_THRESHOLD, "factores": factores},
        icon="siren",
    )


def check_signal_loss(db: Session, now: datetime | None = None) -> int:
    """Unidades en ruta que circulaban y dejaron de reportar posición. Devuelve cuántas alertó."""
    now = now or utcnow()
    last_rows = db.execute(
        select(VehiclePosition.vehicle_id, func.max(VehiclePosition.recorded_at)).group_by(VehiclePosition.vehicle_id)
    ).all()
    count = 0
    for vehicle_id, last_at in last_rows:
        vehicle = db.get(Vehicle, vehicle_id)
        if vehicle is None:
            continue
        age = (now - aware(last_at)).total_seconds()
        threshold = SIGNAL_LOSS_S_SIM if vehicle.simulado else SIGNAL_LOSS_S_REAL
        if age < threshold or age > SIGNAL_LOSS_MAX_AGE_S:
            continue
        if get_state(db, vehicle.id, "senal").get("perdida"):
            continue
        if not active_route(db, vehicle):
            continue
        last = db.scalar(
            select(VehiclePosition).where(VehiclePosition.vehicle_id == vehicle.id).order_by(VehiclePosition.recorded_at.desc())
        )
        if not last or (last.velocidad or 0) < 5:
            continue
        desc = (
            f"{vehicle.placa} no reporta posición desde hace {age:.0f} s; circulaba en ruta a {last.velocidad} km/h "
            f"(último reporte {lima_hhmm(last.recorded_at)}). Puede ser un inhibidor de señal (jammer), el equipo apagado o falta de cobertura."
        )
        record_event(db, vehicle, "jamming", "high", desc, last.lat, last.lng, {"segundos_sin_senal": round(age)})
        upsert_alert(
            db,
            vehicle,
            "jamming",
            "high",
            f"Pérdida de señal GPS: {vehicle.placa} (posible jamming)",
            desc,
            last.lat,
            last.lng,
            {"segundos_sin_senal": round(age)},
            icon="wifi-off",
        )
        set_state(db, vehicle.id, "senal", {"perdida": True, "desde": aware(last.recorded_at).isoformat()})
        evaluate_sabotage(db, vehicle)
        count += 1
    return count


def ingest_position(
    db: Session,
    vehicle: Vehicle,
    lat: float,
    lng: float,
    speed: float,
    now: datetime | None = None,
    skip_jump: bool = False,
    precision_m: float | None = None,
) -> dict:
    """Guarda una posición y corre todas las reglas. Devuelve lo que se disparó."""
    from app.routers.zones import check_zones

    now = now or utcnow()
    prev = db.scalar(
        select(VehiclePosition).where(VehiclePosition.vehicle_id == vehicle.id).order_by(VehiclePosition.recorded_at.desc())
    )
    db.add(VehiclePosition(vehicle_id=vehicle.id, lat=lat, lng=lng, velocidad=int(speed), precision_m=precision_m, recorded_at=now))
    vehicle.lat, vehicle.lng, vehicle.velocidad = lat, lng, int(speed)
    vehicle.precision_m = precision_m
    vehicle.ultima_posicion_at = now
    vehicle.updated_at = now
    db.add(vehicle)
    db.flush()

    spoofed = False if skip_jump else check_spoofing(db, vehicle, prev, lat, lng, now, precision_m)
    if spoofed:
        evaluate_sabotage(db, vehicle)
        return {"spoofing": True, "zonas": [], "aproximada": False}
    check_signal_recovered(db, vehicle, lat, lng, now)
    aproximada = precision_m is not None and precision_m > COARSE_RULES_M
    if not aproximada:
        check_route_deviation(db, vehicle, lat, lng)
    zonas = check_zones(db, vehicle, lat, lng) if precision_m is None or precision_m <= COARSE_ZONES_M else []
    if not aproximada:
        check_stop_in_risk_zone(db, vehicle, speed, lat, lng, now)
    if get_state(db, vehicle.id, "desvio").get("activo"):
        evaluate_sabotage(db, vehicle)
    return {"spoofing": False, "zonas": [z.nombre for z in zonas], "aproximada": aproximada}

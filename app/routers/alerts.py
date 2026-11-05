from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alert_actions import ACTIONS, CLOSED, ONE_SHOT, available_actions
from app.auth import get_current_user, require_staff
from app.database import get_db
from app.detection import SEV_LABEL, add_history, set_state
from app.geo import path_length_m, point_at, project
from app.models import AiAlert, Incident, IncidentEvent, NavRoute, User, Vehicle, utcnow
from app.schemas import AiAlertIn, AlertActionIn
from app.serializers import alert_out, new_incident_id

router = APIRouter(prefix="/alerts", tags=["alerts"])

INCIDENT_CATEGORY = {
    "desvio": "Desvío de ruta",
    "spoofing": "Seguridad (GPS)",
    "jamming": "Seguridad (GPS)",
    "salida_zona": "Geocerca",
    "parada_riesgo": "Seguridad",
    "trafico": "Operativo",
    "sabotaje": "Seguridad",
}


def tick_simulation(db: Session) -> None:
    from app.simulation import tick

    tick(db)


@router.get("")
def list_alerts(_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tick_simulation(db)
    rows = db.scalars(
        select(AiAlert).where(AiAlert.activa.is_(True)).order_by(AiAlert.created_at.desc())
    ).all()
    return [alert_out(r) for r in rows]


@router.get("/historial")
def alert_history(_user: User = Depends(require_staff), db: Session = Depends(get_db)):
    """Alertas cerradas recientes, con su historial de acciones."""
    rows = db.scalars(
        select(AiAlert).where(AiAlert.activa.is_(False)).order_by(AiAlert.updated_at.desc().nullslast()).limit(30)
    ).all()
    return [alert_out(r) for r in rows]


@router.post("")
def create_alert(body: AiAlertIn, _user: User = Depends(require_staff), db: Session = Depends(get_db)):
    placas = [p.strip().upper() for p in (body.placas_afectadas or []) if p.strip()]
    vehicle = db.scalar(select(Vehicle).where(Vehicle.placa == placas[0])) if placas else None
    alert = AiAlert(
        titulo=body.titulo.strip(),
        descripcion=body.descripcion.strip(),
        severidad=body.severidad,
        icon=body.icon or "alert-triangle",
        placas_afectadas=placas,
        vehicle_id=vehicle.id if vehicle else None,
        lat=vehicle.lat if vehicle else None,
        lng=vehicle.lng if vehicle else None,
        historial=[],
    )
    add_history(alert, "creada", "Creada", "Alerta registrada manualmente.", _user.full_name)
    db.add(alert)
    db.flush()
    return alert_out(alert)


@router.patch("/{alert_id}")
def update_alert(
    alert_id: str, body: AiAlertIn, _user: User = Depends(require_staff), db: Session = Depends(get_db)
):
    alert = db.get(AiAlert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alerta no encontrada")
    alert.titulo = body.titulo.strip()
    alert.descripcion = body.descripcion.strip()
    alert.severidad = body.severidad
    alert.icon = body.icon or "alert-triangle"
    alert.placas_afectadas = body.placas_afectadas or []
    alert.updated_at = utcnow()
    db.add(alert)
    db.flush()
    return alert_out(alert)


@router.delete("/{alert_id}")
def delete_alert(alert_id: str, _user: User = Depends(require_staff), db: Session = Depends(get_db)):
    alert = db.get(AiAlert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alerta no encontrada")
    db.delete(alert)
    return {"ok": True}


@router.post("/{alert_id}/dismiss")
def dismiss_alert(alert_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    alert = db.get(AiAlert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alerta no encontrada")
    alert.activa = False
    if (alert.estado or "activa") not in CLOSED:
        alert.estado = "descartada"
        add_history(alert, "descartar", "Descartada", "Alerta descartada.", user.full_name)
    db.add(alert)
    return {"ok": True}


# ─────────────── acciones de respuesta ───────────────

def _vehicle_of(db: Session, alert: AiAlert) -> Vehicle | None:
    if alert.vehicle_id:
        return db.get(Vehicle, alert.vehicle_id)
    placas = alert.placas_afectadas or []
    return db.scalar(select(Vehicle).where(Vehicle.placa == placas[0])) if placas else None


def _create_incident(db: Session, alert: AiAlert, vehicle: Vehicle, user: User, severidad: str | None = None) -> Incident:
    existing = (alert.payload or {}).get("incidente_id")
    if existing:
        inc = db.get(Incident, existing)
        if inc:
            return inc
    now = utcnow()
    lat = alert.lat if alert.lat is not None else vehicle.lat
    lng = alert.lng if alert.lng is not None else vehicle.lng
    inc = Incident(
        id=new_incident_id(db),
        vehicle_id=vehicle.id,
        reported_by=user.id,
        titulo=alert.titulo[:200],
        categoria=INCIDENT_CATEGORY.get(alert.tipo or "", "Alerta"),
        severidad=severidad or alert.severidad,
        estado="reportado",
        ubicacion=f"{lat:.5f}, {lng:.5f}" if lat is not None and lng is not None else "Sin ubicación",
        lat=lat,
        lng=lng,
        descripcion=alert.descripcion + ("\n\n[Generada desde una alerta de la unidad de prueba]" if vehicle.simulado else ""),
        es_panico=False,
        created_at=now,
        updated_at=now,
    )
    db.add(inc)
    db.add(IncidentEvent(incident_id=inc.id, estado="reportado", notas="Incidencia creada desde una alerta", changed_by=user.id))
    alert.payload = {**(alert.payload or {}), "incidente_id": inc.id}
    db.flush()
    return inc


def _notify(db: Session, alert: AiAlert, vehicle: Vehicle | None, prefijo: str = "") -> str:
    from app.notify import Alert, email_configured, send_alert

    sim = "[SIMULACIÓN] " if vehicle is not None and vehicle.simulado else ""
    conductor = vehicle.driver.full_name if vehicle is not None and vehicle.driver else "Sin conductor asignado"
    logs = send_alert(
        db,
        Alert(
            tipo="motor",
            titulo=sim + prefijo + alert.titulo,
            lineas=[alert.descripcion, f"Unidad: {vehicle.placa if vehicle else '—'} · Conductor: {conductor}", f"Severidad: {SEV_LABEL.get(alert.severidad, alert.severidad)}"],
            lat=alert.lat,
            lng=alert.lng,
            vehicle_id=vehicle.id if vehicle else None,
            path="/dashboard",
        ),
    )
    enviados = [l for l in logs if l.estado == "enviado"]
    if enviados:
        n = sum(len([d for d in (l.destinatarios or "").split(",") if d.strip()]) for l in enviados)
        return f"Aviso enviado a {n} destinatario(s)."
    if not email_configured():
        return "Aviso registrado en el historial de notificaciones (el correo no está configurado)."
    errores = [l.error for l in logs if l.error]
    return "No se pudo enviar el correo" + (f": {errores[0]}" if errores else ".")


def _recalculate(db: Session, alert: AiAlert, vehicle: Vehicle) -> str:
    from app.routing import RoutingError, osrm_routes
    from app.simulation import LOOP_GEOMETRY, LOOP_NOTE, reroute_simulated

    route = db.get(NavRoute, alert.nav_route_id) if alert.nav_route_id else None
    if route is None or route.status != "en-curso":
        raise HTTPException(status_code=400, detail="La unidad no tiene una ruta en curso para recalcular")
    now = utcnow()
    ciclica = route.notes == LOOP_NOTE
    if ciclica:
        geom = LOOP_GEOMETRY
        total = path_length_m(geom)
        # Retoma el circuito ~1.2 km más adelante del punto donde salió.
        inicio = (alert.payload or {}).get("salida") or [vehicle.lat, vehicle.lng]
        pr = project(geom, inicio[0], inicio[1])
        rejoin = ((pr.along_m if pr else 0) + 1200) % total
        tlat, tlng, _ = point_at(geom, rejoin)
        try:
            nueva = osrm_routes([(vehicle.lat, vehicle.lng), (tlat, tlng)])[0]
            puente = nueva["geometry"]
            texto = f"Nueva ruta de retorno: {nueva['distance_km']} km (~{nueva['duration_min']:.0f} min) hasta retomar el circuito."
        except RoutingError:
            puente = [[vehicle.lat, vehicle.lng], [tlat, tlng]]
            texto = "Servicio de rutas no disponible: se trazó un retorno directo al circuito."
        if vehicle.simulado:
            reroute_simulated(db, vehicle, puente, rejoin, now)
        alert.payload = {**(alert.payload or {}), "ruta_recalculada": puente}
    else:
        try:
            nueva = osrm_routes([(vehicle.lat, vehicle.lng), (route.dest_lat, route.dest_lng)])[0]
        except RoutingError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        route.geometry = nueva["geometry"]
        route.distance_km = nueva["distance_km"]
        route.duration_min = int(round(nueva["duration_min"]))
        route.progress_pct = 0
        texto = f"Ruta recalculada desde la posición actual hasta {route.dest_name}: {nueva['distance_km']} km (~{nueva['duration_min']:.0f} min)."
        if vehicle.simulado:
            reroute_simulated(db, vehicle, nueva["geometry"], None, now)
        alert.payload = {**(alert.payload or {}), "ruta_recalculada": nueva["geometry"]}
    set_state(db, vehicle.id, "desvio", None)
    return texto


def _apply_alternative(db: Session, alert: AiAlert, vehicle: Vehicle) -> str:
    from app.simulation import LOOP_NOTE, evaluate_traffic, reroute_simulated

    # La unidad siguió avanzando desde que se calculó la alternativa: se recalcula desde su posición actual.
    if alert.tipo == "trafico":
        if evaluate_traffic(db, vehicle) is None:
            raise HTTPException(status_code=400, detail="La unidad ya no pasa por la zona con tráfico; no hace falta cambiar la ruta")
    alt = (alert.payload or {}).get("alternativa")
    if not alt or not alt.get("geometry"):
        raise HTTPException(status_code=400, detail="Esta alerta no tiene una ruta alternativa")
    route = db.get(NavRoute, alert.nav_route_id) if alert.nav_route_id else None
    if route is None or route.status != "en-curso":
        raise HTTPException(status_code=400, detail="La ruta ya no está en curso")
    now = utcnow()
    if route.notes == LOOP_NOTE:
        if vehicle.simulado:
            reroute_simulated(db, vehicle, alt["geometry"], alt.get("rejoin_m"), now)
    else:
        route.geometry = alt["geometry"]
        route.distance_km = alt["distance_km"]
        route.duration_min = int(round(alt["duration_traffic_min"]))
        route.progress_pct = 0
        if vehicle.simulado:
            reroute_simulated(db, vehicle, alt["geometry"], None, now)
    route.route_label = route.route_label if "(alterna)" in route.route_label else f"{route.route_label} (alterna)"[:200]
    set_state(db, vehicle.id, "desvio", None)
    return f"Ruta alternativa aplicada: {alt['distance_km']} km, ~{alt['duration_traffic_min']:.0f} min con tráfico (ahorra ~{alt['ahorro_min']:.0f} min)."


@router.post("/{alert_id}/acciones")
def execute_action(
    alert_id: str,
    body: AlertActionIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    alert = db.get(AiAlert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alerta no encontrada")
    codigo = body.codigo
    disponibles = {a["codigo"]: a for a in available_actions(alert)}
    if codigo not in disponibles:
        raise HTTPException(status_code=400, detail="Esa acción no está disponible para esta alerta")
    if disponibles[codigo]["ejecutada"]:
        raise HTTPException(status_code=409, detail="Esa acción ya se ejecutó")
    staff_only = {"crear_incidencia", "notificar_supervisor", "recalcular_ruta", "aplicar_ruta_alternativa", "mantener_ruta", "activar_protocolo", "resolver", "descartar"}
    if codigo in staff_only and user.role not in ("admin", "supervisor"):
        raise HTTPException(status_code=403, detail="Se requiere rol de supervisor")

    vehicle = _vehicle_of(db, alert)
    label = ACTIONS[codigo][0]
    nota = (body.nota or "").strip() or None
    navegar = None
    resultado = ""

    if codigo == "atender":
        alert.estado = "en_atencion"
        resultado = f"{user.full_name} está atendiendo la alerta."
    elif codigo == "contactar_conductor":
        conductor = vehicle.driver.full_name if vehicle and vehicle.driver else None
        resultado = f"Contacto con {conductor} registrado." if conductor else "La unidad no tiene conductor asignado; contacto registrado con la central."
        alert.estado = "en_atencion"
    elif codigo == "crear_incidencia":
        if vehicle is None:
            raise HTTPException(status_code=400, detail="La alerta no está asociada a una unidad")
        inc = _create_incident(db, alert, vehicle, user)
        resultado = f"Incidencia {inc.id} creada."
        navegar = f"/incidencias?id={inc.id}"
        alert.estado = "en_atencion"
    elif codigo == "notificar_supervisor":
        resultado = _notify(db, alert, vehicle)
    elif codigo == "recalcular_ruta":
        resultado = _recalculate(db, alert, vehicle)
        alert.estado = "resuelta"
    elif codigo == "aplicar_ruta_alternativa":
        resultado = _apply_alternative(db, alert, vehicle)
        alert.estado = "resuelta"
    elif codigo == "mantener_ruta":
        resultado = "Se mantiene la ruta actual."
        alert.estado = "resuelta"
    elif codigo == "activar_protocolo":
        if vehicle is None:
            raise HTTPException(status_code=400, detail="La alerta no está asociada a una unidad")
        vehicle.estado = "alert"
        inc = _create_incident(db, alert, vehicle, user, severidad="high")
        aviso = _notify(db, alert, vehicle, prefijo="PROTOCOLO ACTIVADO · ")
        resultado = f"Protocolo de seguridad activado: unidad en alerta, incidencia {inc.id}. {aviso}"
        alert.estado = "en_atencion"
    elif codigo == "ver_mapa":
        resultado = "Abierto en el mapa en vivo."
        navegar = f"/mapa-vivo?vehiculo={vehicle.id}" if vehicle else "/mapa-vivo"
    elif codigo == "resolver":
        alert.estado = "resuelta"
        resultado = "Alerta resuelta."
    elif codigo == "descartar":
        alert.estado = "descartada"
        resultado = "Descartada como falsa alarma."

    if codigo != "ver_mapa":
        add_history(alert, codigo, label, resultado, user.full_name, nota)
    if alert.estado in CLOSED:
        alert.activa = False
        if vehicle is not None and vehicle.estado == "alert" and alert.tipo in ("spoofing", "jamming", "sabotaje"):
            vehicle.estado = "ok"
    alert.updated_at = utcnow()
    db.flush()
    return {"ok": True, "resultado": resultado, "navegar": navegar, "alerta": alert_out(alert), "unaVez": codigo in ONE_SHOT}

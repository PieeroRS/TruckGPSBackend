from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.auth import get_current_user, is_staff, require_staff
from app.database import get_db
from app.models import NavRoute, User, Vehicle, utcnow
from app.schemas import GuideIn, TrafficCheckIn, NavRouteCreateIn, NavRouteStatusIn, RoutePlanIn
from app.serializers import nav_route_out

router = APIRouter(prefix="/nav-routes", tags=["nav-routes"])
ALLOWED_STATUS = {"solicitada", "asignada", "en-curso", "completada", "cancelada"}


def _query(db: Session):
    return select(NavRoute).options(joinedload(NavRoute.vehicle), joinedload(NavRoute.driver))


def _sim_route_changed(db: Session, route: NavRoute, status: str) -> None:
    """La unidad simulada sigue la ruta que se inicia y vuelve a su circuito al terminar."""
    from app.simulation import LOOP_NOTE, back_to_loop, follow_route

    if not route.vehicle_id or route.notes == LOOP_NOTE:
        return
    vehicle = db.get(Vehicle, route.vehicle_id)
    if vehicle is None or not vehicle.simulado:
        return
    if status == "en-curso":
        if not route.geometry or len(route.geometry) < 2:
            raise HTTPException(status_code=400, detail="La ruta no tiene trazado para que la unidad de prueba la recorra")
        # Una sola ruta en curso por unidad.
        for other in db.scalars(
            select(NavRoute).where(NavRoute.vehicle_id == vehicle.id, NavRoute.status == "en-curso", NavRoute.id != route.id)
        ).all():
            if other.notes == LOOP_NOTE:
                continue
            other.status = "asignada"
        follow_route(db, vehicle, route)
    elif status in ("completada", "cancelada", "asignada"):
        from app.models import SimState

        st = db.get(SimState, vehicle.id)
        if st and st.nav_route_id == route.id:
            back_to_loop(db, vehicle, utcnow())


@router.post("/plan")
def plan_route(body: RoutePlanIn, _user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Rutas alternativas con tiempo estimado con tráfico (franja horaria de Lima + eventos activos)."""
    from app.routers.traffic import active_events
    from app.routing import RoutingError, lima_traffic_factor, plan_with_alternatives

    events = active_events(db)
    try:
        rutas = plan_with_alternatives((body.origin_lat, body.origin_lng), (body.dest_lat, body.dest_lng), events)
    except RoutingError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    factor, franja = lima_traffic_factor()
    from app.history import predict

    historico = predict(db, (body.origin_lat, body.origin_lng), (body.dest_lat, body.dest_lng), rutas[0]["duration_min"]) if rutas else None
    if historico and historico.get("viajes"):
        for r in rutas:
            r["duration_pred_min"] = round(r["duration_min"] * historico["ratio"], 1)
    return {
        "rutas": rutas,
        "historico": historico,
        "franja": franja,
        "factorHora": factor,
        "enVivo": bool(rutas and rutas[0].get("en_vivo")),
        "nota": (
            "Tiempo con el tráfico de este momento (TomTom)."
            if rutas and rutas[0].get("en_vivo")
            else "Tiempo con tráfico estimado según la hora en Lima y los eventos de tráfico registrados."
        ),
    }


def _can_drive(db: Session, route: NavRoute, user: User) -> bool:
    if is_staff(user) or route.driver_id == user.id or route.created_by == user.id:
        return True
    if route.vehicle_id:
        veh = db.get(Vehicle, route.vehicle_id)
        return bool(veh and veh.conductor_id == user.id)
    return False


@router.post("/guia")
def guide(body: GuideIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Guía giro a giro desde la posición actual hasta el destino, rodeando las calles cerradas.

    - Con route_id usa el destino de esa ruta.
    - motivo="cierre": la ruta en curso se actualiza con el nuevo trazado (hay una calle cerrada
      reportada) y queda registrado para el centro de control.
    - motivo="desvio": solo devuelve la guía. El trazado vigilado no cambia: si el conductor se
      sale de la ruta autorizada, el centro de control sigue viendo el desvío.
    """
    from app.routers.traffic import active_events
    from app.routing import RoutingError, closures_of, estimate, events_on_path, route_avoiding

    route = None
    if body.route_id:
        route = db.get(NavRoute, body.route_id)
        if route is None:
            raise HTTPException(status_code=404, detail="Ruta no encontrada")
        if not _can_drive(db, route, user):
            raise HTTPException(status_code=403, detail="Sin permiso para navegar esta ruta")
        dest = (route.dest_lat, route.dest_lng)
    elif body.dest_lat is not None and body.dest_lng is not None:
        dest = (body.dest_lat, body.dest_lng)
    else:
        raise HTTPException(status_code=400, detail="Falta el destino")
    origin = (body.origin_lat, body.origin_lng)
    events = active_events(db)
    cierres = closures_of(events)
    try:
        res = route_avoiding(origin, dest, cierres, steps=True)
    except RoutingError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    est = estimate({k: res[k] for k in ("distance_km", "duration_min", "duration_live_min", "geometry") if k in res}, events)

    actualizada = False
    if route is not None and route.status == "en-curso" and body.motivo == "cierre" and res["cierres_evitados"]:
        # El trazado autorizado tenía una calle cerrada por delante: se reemplaza por la ruta alterna.
        restante = route.geometry or []
        if restante and events_on_path(restante, cierres):
            route.geometry = res["geometry"]
            actualizada = True
            if route.vehicle_id:
                vehicle = db.get(Vehicle, route.vehicle_id)
                if vehicle is not None:
                    from app.detection import record_event, set_state

                    set_state(db, vehicle.id, "desvio", None)
                    nombres = ", ".join(c["descripcion"] for c in res["cierres_evitados"])
                    record_event(
                        db,
                        vehicle,
                        "ruta_recalculada",
                        "low",
                        f"{vehicle.placa} tomó una ruta alterna por calle cerrada ({nombres}). Nuevo trazado: {res['distance_km']} km.",
                        body.origin_lat,
                        body.origin_lng,
                        {"ruta": route.route_label, "cierres": [c["id"] for c in res["cierres_evitados"]]},
                    )
    return {
        "enVivo": bool(est.get("en_vivo")),
        "demoraTraficoMin": res.get("delay_min"),
        "geometry": res["geometry"],
        "steps": res.get("steps", []),
        "distanceKm": res["distance_km"],
        "durationMin": res["duration_min"],
        "durationTrafficMin": est["duration_traffic_min"],
        "cierresEvitados": res["cierres_evitados"],
        "cierresEnRuta": res["cierres_en_ruta"],
        "rutaActualizada": actualizada,
        "destino": {"lat": dest[0], "lng": dest[1], "nombre": route.dest_name if route else None},
    }


@router.get("")
def list_routes(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.simulation import tick

    tick(db)
    q = _query(db).where(NavRoute.status != "cancelada").order_by(NavRoute.created_at.desc()).limit(50)
    rows = db.scalars(q).unique().all()
    if is_staff(user):
        return [nav_route_out(r) for r in rows]
    # El conductor ve sus rutas y las de la unidad que está manejando.
    mis_unidades = {v.id for v in db.scalars(select(Vehicle).where(Vehicle.conductor_id == user.id)).all()}
    return [
        nav_route_out(r)
        for r in rows
        if r.driver_id == user.id or r.created_by == user.id or (r.vehicle_id and r.vehicle_id in mis_unidades)
    ]


@router.post("")
def create_route(body: NavRouteCreateIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    status = "asignada" if body.vehicle_id else "solicitada"
    route = NavRoute(
        vehicle_id=body.vehicle_id or None,
        driver_id=body.driver_id or None,
        created_by=user.id,
        origin_name=body.origin_name,
        origin_lat=body.origin_lat,
        origin_lng=body.origin_lng,
        dest_name=body.dest_name,
        dest_lat=body.dest_lat,
        dest_lng=body.dest_lng,
        route_label=body.route_label,
        distance_km=body.distance_km,
        duration_min=body.duration_min,
        geometry=body.geometry,
        status=status,
        fare_estimate=body.fare_estimate,
        notes=body.notes,
    )
    db.add(route)
    if route.vehicle_id and status in ("asignada", "en-curso"):
        vehicle = db.get(Vehicle, route.vehicle_id)
        if vehicle:
            vehicle.ruta = route.route_label
            vehicle.updated_at = utcnow()
            db.add(vehicle)
    db.flush()
    route = db.scalar(_query(db).where(NavRoute.id == route.id))
    return nav_route_out(route)


@router.patch("/{route_id}")
def update_route(
    route_id: str,
    body: NavRouteStatusIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if body.status not in ALLOWED_STATUS:
        raise HTTPException(status_code=400, detail="Estado inválido")
    route = db.get(NavRoute, route_id)
    if route is None:
        raise HTTPException(status_code=404, detail="Ruta no encontrada")
    conduce = False
    if route.vehicle_id:
        veh = db.get(Vehicle, route.vehicle_id)
        conduce = bool(veh and veh.conductor_id == user.id)
    if not is_staff(user) and route.driver_id != user.id and route.created_by != user.id and not conduce:
        raise HTTPException(status_code=403, detail="Sin permiso para actualizar esta ruta")

    if route.notes and route.notes.startswith("[circuito-sim]") and body.status != "en-curso":
        raise HTTPException(status_code=400, detail="El circuito de la unidad de prueba no se puede detener; usa Reiniciar en el Simulador")
    route.status = body.status
    if body.progress_pct is not None:
        route.progress_pct = body.progress_pct
    if body.status == "en-curso":
        route.started_at = utcnow()
    if body.status == "completada":
        route.completed_at = utcnow()
        route.progress_pct = 100
        from app.history import record_completed_route

        record_completed_route(db, route)
    if route.vehicle_id and body.status in ("asignada", "en-curso"):
        vehicle = db.get(Vehicle, route.vehicle_id)
        if vehicle:
            vehicle.ruta = route.route_label
            vehicle.updated_at = utcnow()
            db.add(vehicle)
    db.add(route)
    db.flush()
    _sim_route_changed(db, route, body.status)
    return {"ok": True}


@router.delete("/{route_id}")
def delete_route(route_id: str, _user: User = Depends(require_staff), db: Session = Depends(get_db)):
    route = db.get(NavRoute, route_id)
    if route is None:
        raise HTTPException(status_code=404, detail="Ruta no encontrada")
    if route.notes and route.notes.startswith("[circuito-sim]"):
        raise HTTPException(status_code=400, detail="El circuito de la unidad de prueba no se puede eliminar")
    _sim_route_changed(db, route, "cancelada")
    db.delete(route)
    return {"ok": True}


AHORRO_MIN = 3.0


@router.post("/guia/trafico")
def traffic_check(body: TrafficCheckIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Durante la navegación: ¿hay una ruta más rápida por el tráfico de ahora?

    Devuelve el tiempo que falta por la ruta actual (con tráfico real) y, si otra ruta ahorra al
    menos 3 minutos y un 10 %, la guía completa por esa ruta. Si la ruta está en curso, el
    trazado vigilado se actualiza y queda registrado para el centro de control.
    """
    from app import tomtom
    from app.geo import project, sub_path, path_length_m
    from app.routers.traffic import active_events
    from app.routing import _avoid_list, closures_of, estimate, events_on_path

    if not tomtom.configured():
        return {"disponible": False}
    route = None
    origin = [body.origin_lat, body.origin_lng]
    camino: list[list[float]] = []
    if body.route_id:
        route = db.get(NavRoute, body.route_id)
        if route is None:
            raise HTTPException(status_code=404, detail="Ruta no encontrada")
        if not _can_drive(db, route, user):
            raise HTTPException(status_code=403, detail="Sin permiso para navegar esta ruta")
        if route.status == "en-curso" and route.geometry:
            pr = project(route.geometry, origin[0], origin[1])
            if pr and pr.distance_m <= 150:
                camino = [origin] + sub_path(route.geometry, pr.along_m, path_length_m(route.geometry))[1:]
    if len(camino) < 2:
        camino = [origin] + [p for p in body.ruta_actual if isinstance(p, list) and len(p) == 2][1:]
    if len(camino) < 2 or path_length_m(camino) < 1500:
        return {"disponible": True, "cambio": False}  # ya está cerca: no vale la pena cambiar

    events = active_events(db)
    cierres = closures_of(events)
    try:
        res = tomtom.reassess(camino, _avoid_list(cierres))
    except tomtom.TomTomError as exc:
        return {"disponible": False, "detalle": str(exc)}
    actual = res["actual_min"]
    mejor = res["mejor"]
    if not mejor or (cierres and events_on_path(mejor["geometry"], cierres)):
        return {"disponible": True, "cambio": False, "etaMin": actual}
    ahorro = round(actual - mejor["duration_live_min"], 1)
    if ahorro < max(AHORRO_MIN, actual * 0.10):
        return {"disponible": True, "cambio": False, "etaMin": actual}

    actualizada = False
    if route is not None and route.status == "en-curso":
        route.geometry = mejor["geometry"]
        actualizada = True
        if route.vehicle_id:
            vehicle = db.get(Vehicle, route.vehicle_id)
            if vehicle is not None:
                from app.detection import record_event

                record_event(
                    db,
                    vehicle,
                    "ruta_recalculada",
                    "low",
                    f"{vehicle.placa} tomó una ruta más rápida por el tráfico (ahorra ~{ahorro:.0f} min). Nuevo trazado: {mejor['distance_km']} km.",
                    body.origin_lat,
                    body.origin_lng,
                    {"ruta": route.route_label, "motivo": "trafico", "ahorro_min": ahorro},
                )
    est = estimate({k: mejor[k] for k in ("distance_km", "duration_min", "duration_live_min", "geometry")}, events)
    dest = mejor["geometry"][-1]
    return {
        "disponible": True,
        "cambio": True,
        "ahorroMin": ahorro,
        "etaMin": actual,
        "guia": {
            "enVivo": True,
            "demoraTraficoMin": mejor["delay_min"],
            "geometry": mejor["geometry"],
            "steps": mejor["steps"],
            "distanceKm": mejor["distance_km"],
            "durationMin": mejor["duration_min"],
            "durationTrafficMin": est["duration_traffic_min"],
            "cierresEvitados": [],
            "cierresEnRuta": [],
            "rutaActualizada": actualizada,
            "destino": {"lat": dest[0], "lng": dest[1], "nombre": route.dest_name if route else None},
        },
    }

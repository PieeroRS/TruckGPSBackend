from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.auth import get_current_user, is_staff, require_staff
from app.database import get_db
from app.models import NavRoute, User, Vehicle, utcnow
from app.schemas import NavRouteCreateIn, NavRouteStatusIn, RoutePlanIn
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
    from app.models import TrafficEvent
    from app.routing import RoutingError, lima_traffic_factor, plan_with_alternatives

    events = db.scalars(select(TrafficEvent).where(TrafficEvent.activo.is_(True))).all()
    try:
        rutas = plan_with_alternatives((body.origin_lat, body.origin_lng), (body.dest_lat, body.dest_lng), events)
    except RoutingError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    factor, franja = lima_traffic_factor()
    return {
        "rutas": rutas,
        "franja": franja,
        "factorHora": factor,
        "nota": "Tiempo con tráfico estimado según la hora en Lima y los eventos de tráfico registrados; OSRM no tiene tráfico en vivo.",
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

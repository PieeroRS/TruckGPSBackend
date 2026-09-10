from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.auth import get_current_user, is_staff, require_staff
from app.database import get_db
from app.models import NavRoute, User, Vehicle, utcnow
from app.schemas import NavRouteCreateIn, NavRouteStatusIn
from app.serializers import nav_route_out

router = APIRouter(prefix="/nav-routes", tags=["nav-routes"])
ALLOWED_STATUS = {"solicitada", "asignada", "en-curso", "completada", "cancelada"}


def _query(db: Session):
    return select(NavRoute).options(joinedload(NavRoute.vehicle), joinedload(NavRoute.driver))


@router.get("")
def list_routes(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    q = _query(db).where(NavRoute.status != "cancelada").order_by(NavRoute.created_at.desc()).limit(50)
    rows = db.scalars(q).unique().all()
    if is_staff(user):
        return [nav_route_out(r) for r in rows]
    return [nav_route_out(r) for r in rows if r.driver_id == user.id or r.created_by == user.id]


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
    if not is_staff(user) and route.driver_id != user.id and route.created_by != user.id:
        raise HTTPException(status_code=403, detail="Sin permiso para actualizar esta ruta")

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
    return {"ok": True}


@router.delete("/{route_id}")
def delete_route(route_id: str, _user: User = Depends(require_staff), db: Session = Depends(get_db)):
    route = db.get(NavRoute, route_id)
    if route is None:
        raise HTTPException(status_code=404, detail="Ruta no encontrada")
    db.delete(route)
    return {"ok": True}

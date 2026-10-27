from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.auth import get_current_user, is_staff, require_staff
from app.database import get_db
from app.models import Incident, User, Vehicle, VehiclePosition, utcnow
from app.schemas import PositionIn, VehicleUpsertIn
from app.routers.zones import check_zones
from app.serializers import vehicle_option_out, vehicle_out

router = APIRouter(prefix="/vehicles", tags=["vehicles"])


def _load_vehicle(db: Session, vehicle_id: str) -> Vehicle:
    vehicle = db.scalar(select(Vehicle).options(joinedload(Vehicle.driver)).where(Vehicle.id == vehicle_id))
    if vehicle is None:
        raise HTTPException(status_code=404, detail="Vehículo no encontrado")
    return vehicle


@router.get("")
def list_vehicles(_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Vehicle).options(joinedload(Vehicle.driver)).order_by(Vehicle.placa)).unique().all()
    return [vehicle_out(v) for v in rows]


@router.get("/options")
def vehicle_options(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Vehicle).options(joinedload(Vehicle.driver)).order_by(Vehicle.placa)).unique().all()
    return [vehicle_option_out(v, user.id) for v in rows]


@router.post("")
def create_vehicle(body: VehicleUpsertIn, _user: User = Depends(require_staff), db: Session = Depends(get_db)):
    placa = body.placa.strip().upper()
    exists = db.scalar(select(Vehicle).where(Vehicle.placa == placa))
    if exists:
        raise HTTPException(status_code=400, detail="Ya existe un vehículo con esa placa")
    vehicle = Vehicle(
        placa=placa,
        modelo=body.modelo.strip(),
        estado=body.estado,
        ruta=body.ruta.strip() if body.ruta else None,
        kilometraje=body.kilometraje,
        prox_mantenimiento=body.prox_mantenimiento or None,
        lat=body.lat if body.lat is not None else -12.0464,
        lng=body.lng if body.lng is not None else -77.0428,
        velocidad=0,
    )
    db.add(vehicle)
    db.flush()
    db.refresh(vehicle)
    return vehicle_out(vehicle)


@router.patch("/{vehicle_id}")
def update_vehicle(
    vehicle_id: str,
    body: VehicleUpsertIn,
    _user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    vehicle = _load_vehicle(db, vehicle_id)
    placa = body.placa.strip().upper()
    clash = db.scalar(select(Vehicle).where(Vehicle.placa == placa, Vehicle.id != vehicle_id))
    if clash:
        raise HTTPException(status_code=400, detail="Ya existe un vehículo con esa placa")
    vehicle.placa = placa
    vehicle.modelo = body.modelo.strip()
    vehicle.estado = body.estado
    vehicle.ruta = body.ruta.strip() if body.ruta else None
    vehicle.kilometraje = body.kilometraje
    vehicle.prox_mantenimiento = body.prox_mantenimiento or None
    if body.lat is not None:
        vehicle.lat = body.lat
    if body.lng is not None:
        vehicle.lng = body.lng
    vehicle.updated_at = utcnow()
    db.add(vehicle)
    db.flush()
    return vehicle_out(vehicle)


@router.delete("/{vehicle_id}")
def delete_vehicle(vehicle_id: str, _user: User = Depends(require_staff), db: Session = Depends(get_db)):
    vehicle = _load_vehicle(db, vehicle_id)
    incident_count = db.scalar(select(func.count()).select_from(Incident).where(Incident.vehicle_id == vehicle_id)) or 0
    if incident_count:
        raise HTTPException(status_code=400, detail="No se puede eliminar: el vehículo tiene incidencias")
    db.delete(vehicle)
    return {"ok": True}


@router.post("/{vehicle_id}/claim")
def claim_vehicle(vehicle_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vehicle = _load_vehicle(db, vehicle_id)
    if vehicle.conductor_id and vehicle.conductor_id != user.id:
        raise HTTPException(status_code=409, detail="Este vehículo ya está asignado a otro conductor")
    vehicle.conductor_id = user.id
    vehicle.estado = "ok"
    vehicle.updated_at = utcnow()
    db.add(vehicle)
    db.flush()
    db.refresh(vehicle)
    return vehicle_out(vehicle)


@router.post("/{vehicle_id}/release")
def release_vehicle(vehicle_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vehicle = _load_vehicle(db, vehicle_id)
    if vehicle.conductor_id == user.id or is_staff(user):
        vehicle.conductor_id = None
        vehicle.velocidad = 0
        vehicle.updated_at = utcnow()
        db.add(vehicle)
    return {"ok": True}


@router.post("/{vehicle_id}/position")
def report_position(
    vehicle_id: str,
    body: PositionIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    vehicle = _load_vehicle(db, vehicle_id)
    if vehicle.conductor_id != user.id and not is_staff(user):
        raise HTTPException(
            status_code=403,
            detail="No puedes reportar posición de un vehículo que no es tuyo. Activa el GPS primero.",
        )
    if vehicle.conductor_id is None:
        vehicle.conductor_id = user.id

    db.add(
        VehiclePosition(
            vehicle_id=vehicle.id,
            lat=body.lat,
            lng=body.lng,
            velocidad=body.velocidad,
        )
    )
    vehicle.lat = body.lat
    vehicle.lng = body.lng
    vehicle.velocidad = body.velocidad
    vehicle.estado = "ok"
    vehicle.updated_at = utcnow()
    db.add(vehicle)
    zonas = check_zones(db, vehicle, body.lat, body.lng)
    return {
        "ok": True,
        "zonas_alertadas": [z.nombre for z in zonas],
        "vehicle_id": vehicle.id,
        "lat": body.lat,
        "lng": body.lng,
        "velocidad": body.velocidad,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }

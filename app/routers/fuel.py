"""Módulo de consumo de combustible.

Dos fuentes de cálculo, sin sensores en el camión:
- Real: cargas registradas (vales de grifo) con odómetro y tanque lleno.
  Rendimiento = km entre dos llenados / galones cargados en ese tramo.
- Estimado: km recorridos (sumados de las posiciones GPS, o la distancia de la
  ruta) / rendimiento de referencia del vehículo.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.auth import get_current_user, is_staff, require_staff
from app.database import get_db
from app.models import FuelLog, NavRoute, User, Vehicle, VehiclePosition
from app.schemas import FuelLogIn, VehicleEfficiencyIn, iso
from app.serializers import fuel_log_out, vehicle_out

router = APIRouter(prefix="/fuel", tags=["fuel"])

# Rendimiento real por debajo de este % del de referencia se marca como "bajo".
UMBRAL_BAJO_PCT = -15.0
# Segmentos GPS con velocidad implícita mayor a esto se consideran ruido.
VEL_MAX_KMH = 150.0


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def gps_km(positions: list[VehiclePosition]) -> float:
    """Suma la distancia entre posiciones consecutivas, descartando ruido."""
    total = 0.0
    prev = None
    for pos in positions:
        if prev is None:
            prev = pos
            continue
        d = haversine_km(prev.lat, prev.lng, pos.lat, pos.lng)
        horas = (_aware(pos.recorded_at) - _aware(prev.recorded_at)).total_seconds() / 3600
        if d < 0.02:
            continue  # vibración del GPS estando detenido
        if horas > 0 and d >= 1 and d / horas > VEL_MAX_KMH:
            continue  # salto imposible: se ignora este punto y se compara el siguiente con el anterior válido
        total += d
        prev = pos
    return total


def real_efficiency(logs: list[FuelLog]) -> tuple[float | None, float, float]:
    """Rendimiento real (km/gal) entre llenados completos con odómetro.

    Devuelve (rendimiento, km_medidos, galones_medidos). Las cargas parciales
    entre dos llenados suman sus galones al tramo.
    """
    con_odo = sorted((l for l in logs if l.odometro_km is not None), key=lambda l: (l.odometro_km, _aware(l.fecha)))
    km_total = 0.0
    gal_total = 0.0
    ancla = None
    gal_tramo = 0.0
    for log in con_odo:
        if ancla is None:
            if log.tanque_lleno:
                ancla = log
            continue
        gal_tramo += log.galones
        if log.tanque_lleno:
            km = log.odometro_km - ancla.odometro_km
            if km > 0 and gal_tramo > 0:
                km_total += km
                gal_total += gal_tramo
            ancla = log
            gal_tramo = 0.0
    if gal_total <= 0:
        return None, 0.0, 0.0
    return km_total / gal_total, km_total, gal_total


def _load_vehicle(db: Session, vehicle_id: str) -> Vehicle:
    vehicle = db.scalar(select(Vehicle).options(joinedload(Vehicle.driver)).where(Vehicle.id == vehicle_id))
    if vehicle is None:
        raise HTTPException(status_code=404, detail="Vehículo no encontrado")
    return vehicle


@router.get("/logs")
def list_logs(
    vehicle_id: str | None = None,
    days: int = Query(default=90, ge=1, le=730),
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    desde = datetime.now(timezone.utc) - timedelta(days=days)
    q = select(FuelLog).options(joinedload(FuelLog.vehicle)).where(FuelLog.fecha >= desde)
    if vehicle_id:
        q = q.where(FuelLog.vehicle_id == vehicle_id)
    rows = db.scalars(q.order_by(FuelLog.fecha.desc())).all()
    return [fuel_log_out(r) for r in rows]


@router.post("/logs")
def create_log(body: FuelLogIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vehicle = _load_vehicle(db, body.vehicle_id)
    if not is_staff(user) and vehicle.conductor_id != user.id:
        raise HTTPException(status_code=403, detail="Solo puedes registrar cargas del vehículo que conduces")
    if body.nav_route_id and db.get(NavRoute, body.nav_route_id) is None:
        raise HTTPException(status_code=404, detail="Ruta no encontrada")
    fecha = _aware(body.fecha) if body.fecha else datetime.now(timezone.utc)
    if fecha > datetime.now(timezone.utc) + timedelta(hours=1):
        raise HTTPException(status_code=400, detail="La fecha de la carga no puede estar en el futuro")
    log = FuelLog(
        vehicle_id=vehicle.id,
        nav_route_id=body.nav_route_id or None,
        registrado_por=user.id,
        fecha=fecha,
        galones=body.galones,
        monto=body.monto,
        odometro_km=body.odometro_km,
        tanque_lleno=body.tanque_lleno,
        grifo=(body.grifo or "").strip() or None,
        notas=(body.notas or "").strip() or None,
    )
    db.add(log)
    if body.odometro_km is not None and body.odometro_km > (vehicle.kilometraje or 0):
        vehicle.kilometraje = body.odometro_km
        db.add(vehicle)
    db.flush()
    log = db.scalar(select(FuelLog).options(joinedload(FuelLog.vehicle)).where(FuelLog.id == log.id))
    return fuel_log_out(log)


@router.delete("/logs/{log_id}")
def delete_log(log_id: str, _user: User = Depends(require_staff), db: Session = Depends(get_db)):
    log = db.get(FuelLog, log_id)
    if log is None:
        raise HTTPException(status_code=404, detail="Carga no encontrada")
    db.delete(log)
    return {"ok": True}


@router.patch("/vehicles/{vehicle_id}")
def update_efficiency(
    vehicle_id: str,
    body: VehicleEfficiencyIn,
    _user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    vehicle = _load_vehicle(db, vehicle_id)
    vehicle.rendimiento_km_gal = body.rendimiento_km_gal
    vehicle.tanque_gal = body.tanque_gal
    db.add(vehicle)
    db.flush()
    return vehicle_out(vehicle)


@router.get("/summary")
def summary(
    days: int = Query(default=30, ge=1, le=365),
    precio_galon: float | None = Query(default=None, gt=0, le=100),
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    hasta = datetime.now(timezone.utc)
    desde = hasta - timedelta(days=days)

    vehicles = db.scalars(select(Vehicle).order_by(Vehicle.placa)).all()
    logs = db.scalars(select(FuelLog).where(FuelLog.fecha >= desde).order_by(FuelLog.fecha)).all()
    positions = db.scalars(
        select(VehiclePosition).where(VehiclePosition.recorded_at >= desde).order_by(VehiclePosition.recorded_at)
    ).all()

    total_gal = sum(l.galones for l in logs)
    total_monto = sum(l.monto for l in logs)
    precio_medio = (total_monto / total_gal) if total_gal else None
    precio_ref = precio_galon or precio_medio

    logs_by_vehicle: dict[str, list[FuelLog]] = {}
    for l in logs:
        logs_by_vehicle.setdefault(l.vehicle_id, []).append(l)
    pos_by_vehicle: dict[str, list[VehiclePosition]] = {}
    for p in positions:
        pos_by_vehicle.setdefault(p.vehicle_id, []).append(p)

    filas = []
    total_km = 0.0
    total_costo_medido = 0.0
    total_km_costo = 0.0
    for v in vehicles:
        vlogs = logs_by_vehicle.get(v.id, [])
        km_gps = gps_km(pos_by_vehicle.get(v.id, []))
        rend_real, km_medidos, gal_medidos = real_efficiency(vlogs)
        metodo = "odometro" if rend_real is not None else None
        galones = sum(l.galones for l in vlogs)
        monto = sum(l.monto for l in vlogs)

        odos = [l.odometro_km for l in vlogs if l.odometro_km is not None]
        km_odo = (max(odos) - min(odos)) if len(odos) >= 2 else 0
        km = km_odo or km_gps
        total_km += km

        if rend_real is None and km_gps > 0 and galones > 0 and len(vlogs) >= 2:
            # Aproximación: km GPS / galones cargados, sin contar la primera carga.
            gal_aprox = galones - vlogs[0].galones
            if gal_aprox > 0:
                rend_real = km_gps / gal_aprox
                metodo = "gps"

        # Costo por km: con odómetro se usa solo el combustible consumido en los
        # tramos medidos (la primera carga se gastó antes del periodo).
        precio_v = (monto / galones) if galones else precio_ref
        if km_medidos and gal_medidos and precio_v:
            costo_medido, km_costo = gal_medidos * precio_v, km_medidos
        elif km and monto:
            costo_medido, km_costo = monto, km
        else:
            costo_medido, km_costo = 0.0, 0.0
        total_costo_medido += costo_medido
        total_km_costo += km_costo

        ref = v.rendimiento_km_gal
        desviacion = ((rend_real - ref) / ref * 100) if (rend_real and ref) else None
        if rend_real is None:
            estado = "sin_datos"
        elif desviacion is not None and desviacion <= UMBRAL_BAJO_PCT:
            estado = "bajo"
        else:
            estado = "ok"

        galones_est = (km_gps / ref) if (ref and km_gps) else None
        filas.append(
            {
                "vehicleId": v.id,
                "placa": v.placa,
                "modelo": v.modelo,
                "rendimientoRef": ref,
                "tanqueGal": v.tanque_gal,
                "cargas": len(vlogs),
                "galones": round(galones, 2),
                "gasto": round(monto, 2),
                "km": round(km, 1),
                "kmGps": round(km_gps, 1),
                "kmOdometro": km_odo,
                "kmMedidos": round(km_medidos, 1),
                "rendimientoReal": round(rend_real, 2) if rend_real else None,
                "metodo": metodo,
                "desviacionPct": round(desviacion, 1) if desviacion is not None else None,
                "costoKm": round(costo_medido / km_costo, 2) if km_costo else None,
                "galonesEstimadosGps": round(galones_est, 2) if galones_est else None,
                "estado": estado,
                "ultimaCarga": iso(vlogs[-1].fecha) if vlogs else None,
            }
        )

    rutas_rows = db.scalars(
        select(NavRoute)
        .options(joinedload(NavRoute.vehicle))
        .where(NavRoute.created_at >= desde, NavRoute.status != "cancelada")
        .order_by(NavRoute.created_at.desc())
        .limit(50)
    ).unique().all()
    rutas = []
    for r in rutas_rows:
        ref = r.vehicle.rendimiento_km_gal if r.vehicle else None
        gal = (r.distance_km / ref) if (r.distance_km and ref) else None
        rutas.append(
            {
                "id": r.id,
                "routeLabel": r.route_label,
                "placa": r.vehicle.placa if r.vehicle else None,
                "status": r.status,
                "distanceKm": r.distance_km,
                "rendimientoRef": ref,
                "galonesEstimados": round(gal, 2) if gal else None,
                "costoEstimado": round(gal * precio_ref, 2) if (gal and precio_ref) else None,
                "createdAt": iso(r.created_at),
            }
        )

    return {
        "periodo": {"desde": iso(desde), "hasta": iso(hasta), "dias": days},
        "precioGalonRef": round(precio_ref, 2) if precio_ref else None,
        "precioGalonFuente": "manual" if precio_galon else ("cargas" if precio_medio else None),
        "umbralBajoPct": UMBRAL_BAJO_PCT,
        "totales": {
            "cargas": len(logs),
            "galones": round(total_gal, 2),
            "gasto": round(total_monto, 2),
            "km": round(total_km, 1),
            "costoKm": round(total_costo_medido / total_km_costo, 2) if total_km_costo else None,
        },
        "vehiculos": filas,
        "rutas": rutas,
    }

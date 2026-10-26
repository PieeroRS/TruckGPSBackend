from __future__ import annotations

import random
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import AiAlert, FuelLog, Incident, NavRoute, User, Vehicle
from app.schemas import iso, relative_time


def profile_out(user: User, include_created: bool = False) -> dict:
    data = {
        "id": user.id,
        "full_name": user.full_name,
        "email": user.email,
        "role": user.role,
    }
    if include_created:
        data["created_at"] = iso(user.created_at)
    return data


def vehicle_out(v: Vehicle) -> dict:
    return {
        "id": v.id,
        "placa": v.placa,
        "conductor": v.driver.full_name if v.driver else "Sin asignar",
        "modelo": v.modelo,
        "estado": v.estado,
        "ruta": v.ruta or "—",
        "lat": v.lat if v.lat is not None else -12.0464,
        "lng": v.lng if v.lng is not None else -77.0428,
        "velocidad": v.velocidad or 0,
        "km": v.kilometraje,
        "proxMant": v.prox_mantenimiento or "",
        "rendimientoKmGal": v.rendimiento_km_gal,
        "tanqueGal": v.tanque_gal,
    }


def vehicle_option_out(v: Vehicle, user_id: str) -> dict:
    conductor_id = v.conductor_id
    es_mio = conductor_id == user_id
    return {
        "id": v.id,
        "placa": v.placa,
        "modelo": v.modelo,
        "ruta": v.ruta or "—",
        "conductorNombre": v.driver.full_name if v.driver else None,
        "ocupado": bool(conductor_id) and not es_mio,
        "esMio": es_mio,
    }


def incident_out(inc: Incident) -> dict:
    attachments = inc.attachments or []
    return {
        "id": inc.id,
        "titulo": inc.titulo,
        "placa": inc.vehicle.placa if inc.vehicle else "—",
        "conductor": inc.vehicle.driver.full_name if inc.vehicle and inc.vehicle.driver else "—",
        "categoria": inc.categoria,
        "severidad": inc.severidad,
        "estado": inc.estado,
        "ubicacion": inc.ubicacion,
        "lat": inc.lat or 0,
        "lng": inc.lng or 0,
        "descripcion": inc.descripcion,
        "creado": iso(inc.created_at),
        "actualizado": iso(inc.updated_at),
        "asignadoA": inc.asignado_a,
        "foto": any(a.type == "photo" for a in attachments),
        "audio": any(a.type == "audio" for a in attachments),
    }


def alert_out(row: AiAlert) -> dict:
    return {
        "id": row.id,
        "titulo": row.titulo,
        "descripcion": row.descripcion,
        "severidad": row.severidad,
        "icon": row.icon,
        "tiempo": relative_time(row.created_at),
        "afecta": row.placas_afectadas or [],
    }


def nav_route_out(row: NavRoute) -> dict:
    return {
        "id": row.id,
        "vehicleId": row.vehicle_id,
        "driverId": row.driver_id,
        "createdBy": row.created_by,
        "originName": row.origin_name,
        "originLat": row.origin_lat,
        "originLng": row.origin_lng,
        "destName": row.dest_name,
        "destLat": row.dest_lat,
        "destLng": row.dest_lng,
        "routeLabel": row.route_label,
        "distanceKm": row.distance_km,
        "durationMin": row.duration_min,
        "geometry": row.geometry,
        "status": row.status,
        "progressPct": row.progress_pct,
        "fareEstimate": row.fare_estimate,
        "notes": row.notes,
        "createdAt": iso(row.created_at),
        "startedAt": iso(row.started_at),
        "completedAt": iso(row.completed_at),
        "vehiclePlaca": row.vehicle.placa if row.vehicle else None,
        "driverName": row.driver.full_name if row.driver else None,
    }


def new_incident_id(db: Session) -> str:
    year = datetime.now(timezone.utc).year
    for _ in range(20):
        candidate = f"INC-{year}-{random.randint(100, 999)}"
        if db.get(Incident, candidate) is None:
            return candidate
    return f"INC-{year}-{random.randint(1000, 9999)}"


def fuel_log_out(row: FuelLog) -> dict:
    return {
        "id": row.id,
        "vehicleId": row.vehicle_id,
        "placa": row.vehicle.placa if row.vehicle else "—",
        "navRouteId": row.nav_route_id,
        "fecha": iso(row.fecha),
        "galones": round(row.galones, 2),
        "monto": round(row.monto, 2),
        "precioGalon": round(row.monto / row.galones, 2) if row.galones else None,
        "odometroKm": row.odometro_km,
        "tanqueLleno": row.tanque_lleno,
        "grifo": row.grifo,
        "notas": row.notas,
    }

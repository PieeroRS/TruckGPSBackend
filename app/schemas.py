from datetime import datetime, timezone

from pydantic import BaseModel, Field


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def relative_time(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    mins = int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
    if mins < 1:
        return "hace un momento"
    if mins < 60:
        return f"hace {mins} min"
    hours = mins // 60
    if hours < 24:
        return f"hace {hours} h"
    return f"hace {hours // 24} d"


class LoginIn(BaseModel):
    email: str
    password: str


class ProfileOut(BaseModel):
    id: str
    full_name: str
    email: str
    role: str
    created_at: str | None = None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    profile: ProfileOut


class ProfileUpdateIn(BaseModel):
    full_name: str | None = None
    role: str | None = None


class CreateUserIn(BaseModel):
    full_name: str
    email: str
    password: str
    role: str = "conductor"


class VehicleUpsertIn(BaseModel):
    placa: str
    modelo: str
    estado: str = "ok"
    ruta: str | None = None
    kilometraje: int = 0
    prox_mantenimiento: str = ""
    lat: float | None = None
    lng: float | None = None


class PositionIn(BaseModel):
    lat: float
    lng: float
    velocidad: int = 0


class IncidentStatusIn(BaseModel):
    estado: str
    notas: str | None = None


class IncidentAssignIn(BaseModel):
    asignado_a: str


class IncidentCommentIn(BaseModel):
    notas: str


class AiAlertIn(BaseModel):
    titulo: str
    descripcion: str
    severidad: str
    icon: str = "alert-triangle"
    placas_afectadas: list[str] = Field(default_factory=list)


class NavRouteCreateIn(BaseModel):
    vehicle_id: str | None = None
    driver_id: str | None = None
    origin_name: str
    origin_lat: float
    origin_lng: float
    dest_name: str
    dest_lat: float
    dest_lng: float
    route_label: str
    distance_km: float | None = None
    duration_min: int | None = None
    geometry: list | None = None
    fare_estimate: float | None = None
    notes: str | None = None


class NavRouteStatusIn(BaseModel):
    status: str
    progress_pct: int | None = None


class FuelLogIn(BaseModel):
    vehicle_id: str
    galones: float = Field(gt=0, le=2000)
    monto: float = Field(ge=0, le=200000)
    fecha: datetime | None = None
    odometro_km: int | None = Field(default=None, ge=0)
    tanque_lleno: bool = True
    grifo: str | None = None
    nav_route_id: str | None = None
    notas: str | None = None


class VehicleEfficiencyIn(BaseModel):
    rendimiento_km_gal: float | None = Field(default=None, gt=0, le=100)
    tanque_gal: float | None = Field(default=None, gt=0, le=2000)


class RiskZoneIn(BaseModel):
    nombre: str = Field(min_length=1, max_length=120)
    tipo: str = "riesgo"
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    radio_m: int = Field(default=500, ge=50, le=20000)
    activa: bool = True
    notas: str | None = None

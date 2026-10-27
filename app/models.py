from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(200), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="conductor")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    vehicles: Mapped[list[Vehicle]] = relationship(back_populates="driver")


class Vehicle(Base):
    __tablename__ = "vehicles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    placa: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    modelo: Mapped[str] = mapped_column(String(120), nullable=False)
    conductor_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"))
    estado: Mapped[str] = mapped_column(String(20), nullable=False, default="ok")
    ruta: Mapped[str | None] = mapped_column(String(200))
    kilometraje: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prox_mantenimiento: Mapped[str | None] = mapped_column(String(20))
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)
    velocidad: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Rendimiento de referencia (km por galón) y capacidad del tanque, para el módulo de consumo.
    rendimiento_km_gal: Mapped[float | None] = mapped_column(Float)
    tanque_gal: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    driver: Mapped[User | None] = relationship(back_populates="vehicles")
    positions: Mapped[list[VehiclePosition]] = relationship(back_populates="vehicle", cascade="all, delete-orphan")
    incidents: Mapped[list[Incident]] = relationship(back_populates="vehicle")


class VehiclePosition(Base):
    __tablename__ = "vehicle_positions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    vehicle_id: Mapped[str] = mapped_column(String(36), ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    velocidad: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    vehicle: Mapped[Vehicle] = relationship(back_populates="positions")


class Trip(Base):
    __tablename__ = "trips"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    vehicle_id: Mapped[str] = mapped_column(String(36), ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=False)
    ruta: Mapped[str] = mapped_column(String(200), nullable=False)
    progress_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    vehicle_id: Mapped[str] = mapped_column(String(36), ForeignKey("vehicles.id", ondelete="RESTRICT"), nullable=False)
    reported_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"))
    titulo: Mapped[str] = mapped_column(String(200), nullable=False)
    categoria: Mapped[str] = mapped_column(String(80), nullable=False)
    severidad: Mapped[str] = mapped_column(String(20), nullable=False)
    estado: Mapped[str] = mapped_column(String(20), nullable=False, default="reportado")
    ubicacion: Mapped[str] = mapped_column(String(300), nullable=False)
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)
    descripcion: Mapped[str] = mapped_column(Text, nullable=False)
    asignado_a: Mapped[str | None] = mapped_column(String(200))
    es_panico: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    vehicle: Mapped[Vehicle] = relationship(back_populates="incidents")
    events: Mapped[list[IncidentEvent]] = relationship(back_populates="incident", cascade="all, delete-orphan")
    attachments: Mapped[list[IncidentAttachment]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )


class IncidentEvent(Base):
    __tablename__ = "incident_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    incident_id: Mapped[str] = mapped_column(String(40), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False)
    estado: Mapped[str] = mapped_column(String(20), nullable=False)
    notas: Mapped[str | None] = mapped_column(Text)
    changed_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    incident: Mapped[Incident] = relationship(back_populates="events")


class IncidentAttachment(Base):
    __tablename__ = "incident_attachments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    incident_id: Mapped[str] = mapped_column(String(40), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(400), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    incident: Mapped[Incident] = relationship(back_populates="attachments")


class AiAlert(Base):
    __tablename__ = "ai_alerts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    titulo: Mapped[str] = mapped_column(String(200), nullable=False)
    descripcion: Mapped[str] = mapped_column(Text, nullable=False)
    severidad: Mapped[str] = mapped_column(String(20), nullable=False)
    icon: Mapped[str] = mapped_column(String(40), nullable=False, default="alert-triangle")
    placas_afectadas: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    activa: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NavRoute(Base):
    __tablename__ = "nav_routes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    vehicle_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("vehicles.id", ondelete="SET NULL"))
    driver_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"))
    created_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"))
    origin_name: Mapped[str] = mapped_column(String(200), nullable=False)
    origin_lat: Mapped[float] = mapped_column(Float, nullable=False)
    origin_lng: Mapped[float] = mapped_column(Float, nullable=False)
    dest_name: Mapped[str] = mapped_column(String(200), nullable=False)
    dest_lat: Mapped[float] = mapped_column(Float, nullable=False)
    dest_lng: Mapped[float] = mapped_column(Float, nullable=False)
    route_label: Mapped[str] = mapped_column(String(200), nullable=False)
    distance_km: Mapped[float | None] = mapped_column(Float)
    duration_min: Mapped[int | None] = mapped_column(Integer)
    geometry: Mapped[list | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="solicitada")
    progress_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fare_estimate: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    vehicle: Mapped[Vehicle | None] = relationship()
    driver: Mapped[User | None] = relationship(foreign_keys=[driver_id])


class FuelLog(Base):
    """Carga de combustible (vale de grifo) de un vehículo."""

    __tablename__ = "fuel_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    vehicle_id: Mapped[str] = mapped_column(String(36), ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=False)
    nav_route_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("nav_routes.id", ondelete="SET NULL"))
    registrado_por: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"))
    fecha: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    galones: Mapped[float] = mapped_column(Float, nullable=False)
    monto: Mapped[float] = mapped_column(Float, nullable=False)
    odometro_km: Mapped[int | None] = mapped_column(Integer)
    tanque_lleno: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    grifo: Mapped[str | None] = mapped_column(String(200))
    notas: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    vehicle: Mapped[Vehicle] = relationship()


class RiskZone(Base):
    """Zona o punto peligroso: al entrar un vehículo se avisa al supervisor."""

    __tablename__ = "risk_zones"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    nombre: Mapped[str] = mapped_column(String(120), nullable=False)
    tipo: Mapped[str] = mapped_column(String(20), nullable=False, default="riesgo")
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    radio_m: Mapped[int] = mapped_column(Integer, nullable=False, default=500)
    activa: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notas: Mapped[str | None] = mapped_column(Text)
    # Zonas "oficiales" vienen de fuentes públicas (Mininter, Sinadef, prensa) y
    # se cargan desde app/data/zonas_lima_callao.json; las "usuario" las crea el supervisor.
    origen: Mapped[str] = mapped_column(String(20), nullable=False, default="usuario")
    codigo: Mapped[str | None] = mapped_column(String(60), unique=True)
    nivel: Mapped[str | None] = mapped_column(String(20))  # alto | muy_alto
    fuentes: Mapped[list | None] = mapped_column(JSON)  # [{"medio", "fecha", "url"}]
    periodo: Mapped[str | None] = mapped_column(String(60))
    created_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ZoneState(Base):
    """Si un vehículo está dentro de una zona y cuándo se avisó por última vez."""

    __tablename__ = "zone_states"

    vehicle_id: Mapped[str] = mapped_column(String(36), ForeignKey("vehicles.id", ondelete="CASCADE"), primary_key=True)
    zone_id: Mapped[str] = mapped_column(String(36), ForeignKey("risk_zones.id", ondelete="CASCADE"), primary_key=True)
    inside: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_alert_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationLog(Base):
    """Registro de cada aviso enviado (o que falló) por correo o SMS."""

    __tablename__ = "notification_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tipo: Mapped[str] = mapped_column(String(20), nullable=False)  # panico | incidente | zona | prueba
    titulo: Mapped[str] = mapped_column(String(200), nullable=False)
    mensaje: Mapped[str] = mapped_column(Text, nullable=False)
    canal: Mapped[str] = mapped_column(String(20), nullable=False)  # email | sms
    destinatarios: Mapped[str] = mapped_column(Text, nullable=False, default="")
    estado: Mapped[str] = mapped_column(String(20), nullable=False)  # enviado | fallido | sin_configurar
    error: Mapped[str | None] = mapped_column(Text)
    vehicle_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("vehicles.id", ondelete="SET NULL"))
    zone_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("risk_zones.id", ondelete="SET NULL"))
    incident_id: Mapped[str | None] = mapped_column(String(40), ForeignKey("incidents.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

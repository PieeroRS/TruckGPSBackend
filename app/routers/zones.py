"""Zonas peligrosas y avisos al supervisor."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_staff
from app.config import settings
from app.database import get_db
from app.models import NotificationLog, RiskZone, User, Vehicle, ZoneState, utcnow
from app.notify import Alert, email_configured, record_alert, send_alert, sms_configured, split_list
from app.routers.fuel import haversine_km
from app.schemas import RiskZoneIn, iso

router = APIRouter(tags=["zones"])

ZONE_TYPES = {
    "robo": "Zona de robos",
    "homicidio": "Homicidios / sicariato",
    "extorsion": "Extorsión a transportistas",
    "sin_senal": "Sin señal",
    "peligro_vial": "Peligro vial",
    "riesgo": "Zona de riesgo",
}
# Para salir de la zona hay que alejarse un 10% más que el radio (evita avisos
# repetidos cuando el GPS oscila justo en el borde).
HISTERESIS = 1.1


def zone_out(z: RiskZone) -> dict:
    return {
        "id": z.id,
        "nombre": z.nombre,
        "tipo": z.tipo,
        "tipoLabel": ZONE_TYPES.get(z.tipo, z.tipo),
        "lat": z.lat,
        "lng": z.lng,
        "radioM": z.radio_m,
        "activa": z.activa,
        "notas": z.notas,
        "origen": z.origen or "usuario",
        "codigo": z.codigo,
        "nivel": z.nivel,
        "fuentes": z.fuentes or [],
        "periodo": z.periodo,
        "createdAt": iso(z.created_at),
    }


def log_out(row: NotificationLog) -> dict:
    return {
        "id": row.id,
        "tipo": row.tipo,
        "titulo": row.titulo,
        "mensaje": row.mensaje,
        "canal": row.canal,
        "destinatarios": row.destinatarios,
        "estado": row.estado,
        "error": row.error,
        "createdAt": iso(row.created_at),
    }


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def check_zones(db: Session, vehicle: Vehicle, lat: float, lng: float) -> list[RiskZone]:
    """Revisa si la nueva posición entra a una zona activa y avisa. Devuelve las zonas que dispararon aviso."""
    zones = db.scalars(select(RiskZone).where(RiskZone.activa.is_(True))).all()
    if not zones:
        return []
    now = utcnow()
    cooldown = timedelta(minutes=settings.zone_alert_cooldown_min)
    fired: list[RiskZone] = []
    for z in zones:
        dist_m = haversine_km(lat, lng, z.lat, z.lng) * 1000
        state = db.get(ZoneState, (vehicle.id, z.id))
        if state is None:
            state = ZoneState(vehicle_id=vehicle.id, zone_id=z.id, inside=False)
            db.add(state)
        if dist_m <= z.radio_m:
            last = _aware(state.last_alert_at)
            if not state.inside and (last is None or now - last >= cooldown):
                conductor = vehicle.driver.full_name if vehicle.driver else "Sin conductor asignado"
                oficial = (z.origen or "usuario") == "oficial"
                deliver = send_alert if (not oficial or settings.official_zone_alerts) else record_alert
                fuente = (z.fuentes or [{}])[0] if oficial else {}
                deliver(
                    db,
                    Alert(
                        tipo="zona",
                        titulo=f"{vehicle.placa} entró a zona peligrosa: {z.nombre}",
                        lineas=[
                            f"Tipo: {ZONE_TYPES.get(z.tipo, z.tipo)}",
                            f"Conductor: {conductor}",
                            f"A {int(dist_m)} m del centro de la zona (radio {z.radio_m} m)",
                            f"Hora: {now.astimezone(timezone(timedelta(hours=-5))).strftime('%d/%m/%Y %H:%M')} (Lima)",
                            *([f"Nota: {z.notas}"] if z.notas else []),
                            *([f"Fuente: {fuente.get('medio')} ({fuente.get('fecha')})"] if fuente.get("medio") else []),
                        ],
                        lat=lat,
                        lng=lng,
                        vehicle_id=vehicle.id,
                        zone_id=z.id,
                        path="/mapa-vivo",
                    ),
                )
                state.last_alert_at = now
                fired.append(z)
            state.inside = True
        elif dist_m > z.radio_m * HISTERESIS:
            state.inside = False
    return fired


# ─────────────── Zonas ───────────────

@router.get("/zones")
def list_zones(_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(RiskZone).order_by(RiskZone.created_at.desc())).all()
    return [zone_out(z) for z in rows]


def _apply(z: RiskZone, body: RiskZoneIn) -> None:
    if body.tipo not in ZONE_TYPES:
        raise HTTPException(status_code=400, detail="Tipo de zona inválido")
    z.nombre = body.nombre.strip()
    z.tipo = body.tipo
    z.lat = body.lat
    z.lng = body.lng
    z.radio_m = body.radio_m
    z.activa = body.activa
    z.notas = (body.notas or "").strip() or None


@router.post("/zones")
def create_zone(body: RiskZoneIn, user: User = Depends(require_staff), db: Session = Depends(get_db)):
    z = RiskZone(created_by=user.id)
    _apply(z, body)
    db.add(z)
    db.flush()
    return zone_out(z)


@router.patch("/zones/{zone_id}")
def update_zone(zone_id: str, body: RiskZoneIn, _user: User = Depends(require_staff), db: Session = Depends(get_db)):
    z = db.get(RiskZone, zone_id)
    if z is None:
        raise HTTPException(status_code=404, detail="Zona no encontrada")
    if z.origen == "oficial":
        # Las zonas oficiales vienen de fuentes públicas: solo se pueden activar o desactivar.
        z.activa = body.activa
        db.add(z)
        db.flush()
        return zone_out(z)
    _apply(z, body)
    db.add(z)
    db.flush()
    return zone_out(z)


@router.delete("/zones/{zone_id}")
def delete_zone(zone_id: str, _user: User = Depends(require_staff), db: Session = Depends(get_db)):
    z = db.get(RiskZone, zone_id)
    if z is None:
        raise HTTPException(status_code=404, detail="Zona no encontrada")
    if z.origen == "oficial":
        raise HTTPException(status_code=400, detail="Las zonas oficiales no se eliminan; desactívala si no quieres usarla")
    for st in db.scalars(select(ZoneState).where(ZoneState.zone_id == zone_id)).all():
        db.delete(st)
    db.delete(z)
    return {"ok": True}


# ─────────────── Avisos ───────────────

def _mask_email(e: str) -> str:
    user, _, domain = e.partition("@")
    return (user[:2] + "•••@" + domain) if domain else "•••"


def _mask_phone(p: str) -> str:
    return ("•••" + p[-3:]) if len(p) > 3 else "•••"


@router.get("/notifications/status")
def notifications_status(_user: User = Depends(require_staff)):
    return {
        "email": {
            "configurado": email_configured(),
            "destinatarios": [_mask_email(e) for e in split_list(settings.alert_emails)],
            "faltan": [k for k, v in (("RESEND_API_KEY", settings.resend_api_key), ("ALERT_EMAILS", settings.alert_emails)) if not v],
        },
        "sms": {
            "configurado": sms_configured(),
            "destinatarios": [_mask_phone(p) for p in split_list(settings.alert_phones)],
            "faltan": [
                k
                for k, v in (
                    ("TWILIO_ACCOUNT_SID", settings.twilio_account_sid),
                    ("TWILIO_AUTH_TOKEN", settings.twilio_auth_token),
                    ("TWILIO_FROM", settings.twilio_from),
                    ("ALERT_PHONES", settings.alert_phones),
                )
                if not v
            ],
        },
        "cooldownMin": settings.zone_alert_cooldown_min,
        "zonasOficialesAvisan": settings.official_zone_alerts,
    }


@router.get("/notifications")
def list_notifications(
    limit: int = Query(default=50, ge=1, le=200),
    _user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    rows = db.scalars(select(NotificationLog).order_by(NotificationLog.created_at.desc()).limit(limit)).all()
    return [log_out(r) for r in rows]


@router.post("/notifications/test")
def test_notification(user: User = Depends(require_staff), db: Session = Depends(get_db)):
    logs = send_alert(
        db,
        Alert(
            tipo="prueba",
            titulo="Aviso de prueba",
            lineas=[f"Enviado por {user.full_name} desde TruckGPS.", "Si recibiste esto, los avisos están funcionando."],
            path="/zonas",
        ),
    )
    return [log_out(r) for r in logs]

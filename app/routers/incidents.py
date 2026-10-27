from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.auth import get_current_user, require_staff
from app.config import UPLOADS_DIR
from app.database import get_db
from app.models import Incident, IncidentAttachment, IncidentEvent, User, Vehicle, utcnow
from app.schemas import IncidentAssignIn, IncidentCommentIn, IncidentStatusIn
from app.notify import Alert, send_alert
from app.serializers import incident_out, new_incident_id

router = APIRouter(prefix="/incidents", tags=["incidents"])

ALLOWED_STATUS = {"reportado", "asignado", "en-atencion", "resuelto"}


def _query(db: Session):
    return (
        select(Incident)
        .options(
            joinedload(Incident.vehicle).joinedload(Vehicle.driver),
            joinedload(Incident.attachments),
        )
        .order_by(Incident.created_at.desc())
    )


@router.get("")
def list_incidents(_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.scalars(_query(db)).unique().all()
    return [incident_out(inc) for inc in rows]


def _to_float(value: str | None) -> float | None:
    if value is None or value.strip() in ("", "null", "undefined"):
        return None
    return float(value)


@router.post("")
async def create_incident(
    titulo: str = Form(...),
    placa: str = Form(...),
    categoria: str = Form(...),
    severidad: str = Form(...),
    ubicacion: str = Form(...),
    descripcion: str = Form(...),
    lat: str | None = Form(default=None),
    lng: str | None = Form(default=None),
    es_panico: str = Form(default="false"),
    photos: list[UploadFile] | None = File(default=None),
    audio: UploadFile | None = File(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    vehicle = db.scalar(select(Vehicle).where(Vehicle.placa == placa.strip().upper()))
    if vehicle is None:
        raise HTTPException(status_code=404, detail=f"Vehículo {placa} no encontrado.")

    panic = es_panico.strip().lower() in ("1", "true", "yes", "on")
    incident_id = new_incident_id(db)
    now = utcnow()
    incident = Incident(
        id=incident_id,
        vehicle_id=vehicle.id,
        reported_by=user.id,
        titulo=titulo.strip(),
        categoria=categoria.strip(),
        severidad=severidad,
        estado="reportado",
        ubicacion=ubicacion.strip(),
        lat=_to_float(lat),
        lng=_to_float(lng),
        descripcion=descripcion.strip(),
        asignado_a=None,
        es_panico=panic,
        created_at=now,
        updated_at=now,
    )
    db.add(incident)
    db.add(
        IncidentEvent(
            incident_id=incident_id,
            estado="reportado",
            notas="Reporte de emergencia" if panic else "Incidencia registrada",
            changed_by=user.id,
        )
    )
    db.flush()

    folder = UPLOADS_DIR / incident_id
    folder.mkdir(parents=True, exist_ok=True)

    for index, file in enumerate(photos or []):
        if not file.filename:
            continue
        safe_name = Path(file.filename).name
        rel = f"{incident_id}/photo-{index + 1}-{safe_name}"
        dest = UPLOADS_DIR / rel
        dest.write_bytes(await file.read())
        db.add(IncidentAttachment(incident_id=incident_id, type="photo", storage_path=rel))

    if audio is not None and audio.filename:
        rel = f"{incident_id}/audio.webm"
        dest = UPLOADS_DIR / rel
        dest.write_bytes(await audio.read())
        db.add(IncidentAttachment(incident_id=incident_id, type="audio", storage_path=rel))

    db.flush()
    incident = db.scalar(_query(db).where(Incident.id == incident_id))
    _notify_incident(db, incident, user)
    return incident_out(incident)


SEVERIDAD = {"high": "Alta", "medium": "Media", "low": "Baja"}


def _notify_incident(db: Session, incident: Incident, user: User) -> None:
    placa = incident.vehicle.placa if incident.vehicle else "—"
    if incident.es_panico:
        titulo = f"🚨 PÁNICO en {placa}: {incident.titulo}"
    else:
        titulo = f"Incidencia en {placa}: {incident.titulo}"
    send_alert(
        db,
        Alert(
            tipo="panico" if incident.es_panico else "incidente",
            titulo=titulo,
            lineas=[
                f"Reportado por: {user.full_name}",
                f"Categoría: {incident.categoria} · Severidad: {SEVERIDAD.get(incident.severidad, incident.severidad)}",
                f"Ubicación: {incident.ubicacion}",
                f"Detalle: {incident.descripcion[:300]}",
                f"Código: {incident.id}",
            ],
            lat=incident.lat,
            lng=incident.lng,
            vehicle_id=incident.vehicle_id,
            incident_id=incident.id,
            path="/incidencias",
        ),
    )


@router.patch("/{incident_id}/status")
def update_status(
    incident_id: str,
    body: IncidentStatusIn,
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    if body.estado not in ALLOWED_STATUS:
        raise HTTPException(status_code=400, detail="Estado inválido")
    incident = db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incidencia no encontrada")
    incident.estado = body.estado
    incident.updated_at = utcnow()
    db.add(incident)
    db.add(
        IncidentEvent(
            incident_id=incident_id,
            estado=body.estado,
            notas=body.notas or f"Estado cambiado a {body.estado}",
            changed_by=user.id,
        )
    )
    return {"ok": True}


@router.patch("/{incident_id}/assign")
def assign_incident(
    incident_id: str,
    body: IncidentAssignIn,
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    incident = db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incidencia no encontrada")
    name = body.asignado_a.strip()
    incident.asignado_a = name
    incident.estado = "asignado"
    incident.updated_at = utcnow()
    db.add(incident)
    db.add(
        IncidentEvent(
            incident_id=incident_id,
            estado="asignado",
            notas=f"Asignado a {name}",
            changed_by=user.id,
        )
    )
    return {"ok": True}


@router.post("/{incident_id}/comments")
def add_comment(
    incident_id: str,
    body: IncidentCommentIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    incident = db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incidencia no encontrada")
    notes = body.notas.strip()
    if not notes:
        raise HTTPException(status_code=400, detail="El comentario no puede estar vacío")
    db.add(
        IncidentEvent(
            incident_id=incident_id,
            estado=incident.estado,
            notas=notes,
            changed_by=user.id,
        )
    )
    incident.updated_at = utcnow()
    db.add(incident)
    return {"ok": True}


@router.delete("/{incident_id}")
def delete_incident(incident_id: str, _user: User = Depends(require_staff), db: Session = Depends(get_db)):
    incident = db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incidencia no encontrada")
    db.delete(incident)
    return {"ok": True}

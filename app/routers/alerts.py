from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_staff
from app.database import get_db
from app.models import AiAlert, User
from app.schemas import AiAlertIn
from app.serializers import alert_out

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("")
def list_alerts(_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(AiAlert).where(AiAlert.activa.is_(True)).order_by(AiAlert.created_at.desc())
    ).all()
    return [alert_out(r) for r in rows]


@router.post("")
def create_alert(body: AiAlertIn, _user: User = Depends(require_staff), db: Session = Depends(get_db)):
    alert = AiAlert(
        titulo=body.titulo.strip(),
        descripcion=body.descripcion.strip(),
        severidad=body.severidad,
        icon=body.icon or "alert-triangle",
        placas_afectadas=body.placas_afectadas or [],
    )
    db.add(alert)
    db.flush()
    return alert_out(alert)


@router.patch("/{alert_id}")
def update_alert(
    alert_id: str, body: AiAlertIn, _user: User = Depends(require_staff), db: Session = Depends(get_db)
):
    alert = db.get(AiAlert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alerta no encontrada")
    alert.titulo = body.titulo.strip()
    alert.descripcion = body.descripcion.strip()
    alert.severidad = body.severidad
    alert.icon = body.icon or "alert-triangle"
    alert.placas_afectadas = body.placas_afectadas or []
    db.add(alert)
    db.flush()
    return alert_out(alert)


@router.delete("/{alert_id}")
def delete_alert(alert_id: str, _user: User = Depends(require_staff), db: Session = Depends(get_db)):
    alert = db.get(AiAlert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alerta no encontrada")
    db.delete(alert)
    return {"ok": True}


@router.post("/{alert_id}/dismiss")
def dismiss_alert(alert_id: str, _user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    alert = db.get(AiAlert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alerta no encontrada")
    alert.activa = False
    db.add(alert)
    return {"ok": True}

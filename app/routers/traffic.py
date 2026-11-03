from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_staff
from app.database import get_db
from app.detection import aware
from app.models import NavRoute, TrafficEvent, User, Vehicle, utcnow
from app.routers.simulator import traffic_out
from app.schemas import TrafficEventIn

router = APIRouter(prefix="/traffic", tags=["traffic"])
NIVELES = {"moderado", "congestionado", "bloqueo"}


@router.get("")
def list_traffic(_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.routing import lima_traffic_factor, traffic_level

    now = utcnow()
    rows = db.scalars(select(TrafficEvent).where(TrafficEvent.activo.is_(True))).all()
    out = []
    for t in rows:
        if t.expira_at and aware(t.expira_at) <= now:
            t.activo = False
            continue
        out.append(traffic_out(t))
    factor, franja = lima_traffic_factor(now)
    return {"eventos": out, "factor": factor, "franja": franja, "nivel": traffic_level(factor)}


@router.post("")
def create_traffic(body: TrafficEventIn, _user: User = Depends(require_staff), db: Session = Depends(get_db)):
    if body.nivel not in NIVELES:
        raise HTTPException(status_code=400, detail="Nivel inválido")
    ev = TrafficEvent(
        descripcion=body.descripcion.strip(),
        nivel=body.nivel,
        lat=body.lat,
        lng=body.lng,
        radio_m=body.radio_m,
        simulado=False,
        activo=True,
        expira_at=utcnow() + timedelta(minutes=body.duracion_min),
    )
    db.add(ev)
    db.flush()
    # Ajuste dinámico: revisar las unidades en ruta que pasan por el evento.
    from app.simulation import evaluate_traffic

    afectadas = []
    vehicle_ids = {r.vehicle_id for r in db.scalars(select(NavRoute).where(NavRoute.status == "en-curso")).all() if r.vehicle_id}
    for vid in vehicle_ids:
        v = db.get(Vehicle, vid)
        if v and evaluate_traffic(db, v):
            afectadas.append(v.placa)
    return {**traffic_out(ev), "unidadesAfectadas": afectadas}


@router.delete("/{event_id}")
def delete_traffic(event_id: str, _user: User = Depends(require_staff), db: Session = Depends(get_db)):
    ev = db.get(TrafficEvent, event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="Evento no encontrado")
    ev.activo = False
    return {"ok": True}

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.auth import get_current_user
from app.database import get_db
from app.models import AiAlert, Incident, IncidentEvent, NavRoute, Trip, User
from app.serializers import alert_out

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("")
def dashboard(_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    alerts = db.scalars(
        select(AiAlert).where(AiAlert.activa.is_(True)).order_by(AiAlert.created_at.desc())
    ).all()

    nav_routes = db.scalars(
        select(NavRoute)
        .where(NavRoute.status.in_(("en-curso", "asignada", "solicitada")))
        .order_by(NavRoute.created_at.desc())
        .limit(6)
    ).all()

    trips = db.scalars(select(Trip).where(Trip.status == "active")).all()

    events = db.scalars(
        select(IncidentEvent)
        .options(joinedload(IncidentEvent.incident).joinedload(Incident.vehicle))
        .order_by(IncidentEvent.created_at.desc())
        .limit(8)
    ).unique().all()

    if nav_routes:
        routes = [
            {
                "ruta": r.route_label,
                "pct": r.progress_pct if r.progress_pct else (35 if r.status == "en-curso" else 5),
                "color": "emerald" if (r.progress_pct or 0) >= 60 else "amber",
            }
            for r in nav_routes
        ]
    else:
        routes = [
            {
                "ruta": t.ruta,
                "pct": t.progress_pct,
                "color": "emerald" if t.progress_pct >= 60 else "amber",
            }
            for t in trips
        ]

    activity = []
    for event in events:
        inc = event.incident
        placa = inc.vehicle.placa if inc and inc.vehicle else "—"
        titulo = inc.titulo if inc else (event.notas or "Evento")
        dot = "emerald" if event.estado == "resuelto" else "red" if event.estado == "reportado" else "amber"
        activity.append(
            {
                "dot": dot,
                "html": f'<strong class="text-slate-900">{placa}</strong> · {titulo} <span class="text-slate-400">({event.estado})</span>',
            }
        )

    return {
        "alerts": [alert_out(a) for a in alerts],
        "routes": routes,
        "activity": activity,
    }

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_staff
from app.database import get_db
from app.models import AiAlert, TrafficEvent, User
from app.schemas import SimScenarioIn, iso
from app.serializers import alert_out, nav_route_out, vehicle_out

router = APIRouter(prefix="/simulator", tags=["simulator"])


def event_out(e) -> dict:
    return {
        "id": e.id,
        "tipo": e.tipo,
        "severidad": e.severidad,
        "descripcion": e.descripcion,
        "lat": e.lat,
        "lng": e.lng,
        "datos": e.datos or {},
        "creado": iso(e.created_at),
    }


def traffic_out(t: TrafficEvent) -> dict:
    return {
        "id": t.id,
        "descripcion": t.descripcion,
        "nivel": t.nivel,
        "lat": t.lat,
        "lng": t.lng,
        "radioM": t.radio_m,
        "simulado": t.simulado,
        "expira": iso(t.expira_at),
    }


def _snapshot(db: Session) -> dict:
    from app.simulation import GEOFENCE_CENTER, GEOFENCE_RADIUS_M, SCENARIOS, sim_snapshot

    snap = sim_snapshot(db)
    v, st = snap["vehicle"], snap["state"]
    alerts = db.scalars(
        select(AiAlert).where(AiAlert.vehicle_id == v.id, AiAlert.activa.is_(True)).order_by(AiAlert.created_at.desc())
    ).all()
    traffic = db.scalars(select(TrafficEvent).where(TrafficEvent.activo.is_(True))).all()
    return {
        "vehicle": vehicle_out(v),
        "escenario": st.escenario,
        "restanteS": snap["restante_s"],
        "velocidadKmh": round(st.velocidad_kmh),
        "ruta": nav_route_out(snap["route"]) if snap["route"] else None,
        "tramoRetorno": snap["puente"],
        "senalPerdida": snap["senal_perdida"],
        "desviado": snap["desviado"],
        "geocerca": {"lat": GEOFENCE_CENTER[0], "lng": GEOFENCE_CENTER[1], "radioM": GEOFENCE_RADIUS_M},
        "escenarios": SCENARIOS,
        "eventos": [event_out(e) for e in snap["eventos"]],
        "alertas": [alert_out(a) for a in alerts],
        "trafico": [traffic_out(t) for t in traffic],
    }


@router.get("")
def simulator_state(_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.simulation import tick

    tick(db, force=True)
    return _snapshot(db)


@router.post("/escenario")
def run_scenario(body: SimScenarioIn, _user: User = Depends(require_staff), db: Session = Depends(get_db)):
    from app.simulation import start_scenario

    try:
        res = start_scenario(db, body.tipo, body.duracion_s)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**res, "estado": _snapshot(db)}


@router.post("/terminar")
def stop_scenario(_user: User = Depends(require_staff), db: Session = Depends(get_db)):
    from app.models import SimState, utcnow
    from app.simulation import end_scenario, ensure_simulation, tick

    v = ensure_simulation(db)
    st = db.get(SimState, v.id)
    end_scenario(db, st, utcnow(), "detenido por el usuario")
    db.flush()
    tick(db, force=True)
    return _snapshot(db)


@router.post("/reiniciar")
def reset(_user: User = Depends(require_staff), db: Session = Depends(get_db)):
    from app.simulation import reset_simulation, tick

    reset_simulation(db)
    tick(db, force=True)
    return _snapshot(db)

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user, is_staff, require_staff
from app.database import get_db
from app.detection import aware
from app.models import NavRoute, TrafficEvent, User, Vehicle, utcnow
from app.routers.simulator import traffic_out
from app.schemas import ClosureIn, TrafficEventIn

router = APIRouter(prefix="/traffic", tags=["traffic"])
NIVELES = {"moderado", "congestionado", "bloqueo"}


def active_events(db: Session) -> list[TrafficEvent]:
    """Eventos de tráfico vigentes (da de baja los vencidos)."""
    now = utcnow()
    out = []
    for t in db.scalars(select(TrafficEvent).where(TrafficEvent.activo.is_(True))).all():
        if t.expira_at and aware(t.expira_at) <= now:
            t.activo = False
            continue
        out.append(t)
    return out


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
    return {**traffic_out(ev), "unidadesAfectadas": _notify_routes(db)}


def _notify_routes(db: Session) -> list[str]:
    """Ajuste dinámico: revisa las unidades en ruta que pasan por el evento."""
    from app.simulation import evaluate_traffic

    afectadas = []
    vehicle_ids = {r.vehicle_id for r in db.scalars(select(NavRoute).where(NavRoute.status == "en-curso")).all() if r.vehicle_id}
    for vid in vehicle_ids:
        v = db.get(Vehicle, vid)
        try:
            if v and evaluate_traffic(db, v):
                afectadas.append(v.placa)
        except Exception:  # noqa: BLE001 - el aviso a otras unidades no debe impedir el reporte
            continue
    return afectadas


@router.post("/cierre")
def report_closure(body: ClosureIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Calle cerrada reportada por un conductor o supervisor: las rutas la rodean hasta que venza o se reabra."""
    from app.geo import haversine_m, project
    from app.routing import CLOSURE_RADIUS_M

    lat, lng = body.lat, body.lng
    # Navegando, el GPS puede estar unos metros fuera de la vía: el cierre se pega a la ruta.
    if body.route_id:
        route = db.get(NavRoute, body.route_id)
        if route is not None and route.geometry:
            pr = project(route.geometry, lat, lng)
            if pr and pr.distance_m <= 80:
                lat, lng = pr.lat, pr.lng
    now = utcnow()
    expira = now + timedelta(hours=body.duracion_h)
    desc = (body.descripcion or "").strip() or "Calle cerrada"
    # Si ya hay un cierre activo en el mismo punto, se confirma (se extiende) en vez de duplicarlo.
    for ev in db.scalars(select(TrafficEvent).where(TrafficEvent.activo.is_(True), TrafficEvent.nivel == "cierre")).all():
        if ev.expira_at and aware(ev.expira_at) <= now:
            ev.activo = False
            continue
        if haversine_m(ev.lat, ev.lng, lat, lng) <= CLOSURE_RADIUS_M * 1.5:
            if not ev.expira_at or aware(ev.expira_at) < expira:
                ev.expira_at = expira
            return {**traffic_out(ev), "confirmado": True, "unidadesAfectadas": []}
    ev = TrafficEvent(
        descripcion=desc,
        nivel="cierre",
        lat=round(lat, 6),
        lng=round(lng, 6),
        radio_m=CLOSURE_RADIUS_M,
        simulado=False,
        activo=True,
        expira_at=expira,
        reportado_por=user.full_name,
        reportado_por_id=user.id,
    )
    db.add(ev)
    db.flush()
    return {**traffic_out(ev), "confirmado": False, "unidadesAfectadas": _notify_routes(db)}


@router.delete("/{event_id}")
def delete_traffic(event_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ev = db.get(TrafficEvent, event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="Evento no encontrado")
    # Una calle cerrada la puede reabrir cualquier usuario que pase por ahí; el resto, solo el personal de control.
    if ev.nivel != "cierre" and not is_staff(user):
        raise HTTPException(status_code=403, detail="Solo el personal de control puede quitar este evento")
    ev.activo = False
    return {"ok": True}

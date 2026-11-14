from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import app_settings as cfg
from app.auth import get_current_user, require_staff
from app.database import get_db
from app.models import User, Vehicle

router = APIRouter(prefix="/settings", tags=["settings"])


def _who(user: User) -> str:
    return f"{user.full_name} ({user.email})"


@router.get("/umbrales")
def get_umbrales(_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return {
        "valores": cfg.get_umbrales(db),
        "porDefecto": cfg.UMBRALES_DEFAULT,
        "rangos": {k: {"min": lo, "max": hi} for k, (lo, hi) in cfg.UMBRALES_RANGO.items()},
        **cfg.meta(db, "umbrales"),
    }


@router.put("/umbrales")
def put_umbrales(body: dict, user: User = Depends(require_staff), db: Session = Depends(get_db)):
    try:
        cfg.set_umbrales(db, body.get("valores", body), _who(user))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return get_umbrales(user, db)


@router.post("/umbrales/restablecer")
def reset_umbrales(user: User = Depends(require_staff), db: Session = Depends(get_db)):
    cfg.set_umbrales(db, cfg.UMBRALES_DEFAULT, _who(user))
    return get_umbrales(user, db)


@router.get("/interfaz")
def get_interfaz(_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return {
        "roles": cfg.get_interfaz(db),
        "modulos": cfg.MODULOS,
        "fijos": {k: sorted(v) for k, v in cfg.INTERFAZ_FIJOS.items()},
        **cfg.meta(db, "interfaz"),
    }


@router.put("/interfaz")
def put_interfaz(body: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Solo el administrador puede personalizar la interfaz por rol")
    cfg.set_interfaz(db, body.get("roles", body), _who(user))
    return get_interfaz(user, db)


@router.get("/flota")
def get_flota(_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    unidades = db.scalar(select(func.count()).select_from(Vehicle)) or 0
    return {"datos": cfg.get_flota(db), "unidades": unidades, **cfg.meta(db, "flota")}


@router.put("/flota")
def put_flota(body: dict, user: User = Depends(require_staff), db: Session = Depends(get_db)):
    try:
        cfg.set_flota(db, body.get("datos", body), _who(user))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return get_flota(user, db)

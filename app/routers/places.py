from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.auth import get_current_user, is_staff
from app.database import get_db
from app.models import SavedPlace, User
from app.schemas import SavedPlaceIn

router = APIRouter(prefix="/places", tags=["places"])
MAX_PER_USER = 60


def place_out(p: SavedPlace, user: User) -> dict:
    return {
        "id": p.id,
        "nombre": p.nombre,
        "direccion": p.direccion,
        "lat": p.lat,
        "lng": p.lng,
        "compartido": p.compartido,
        "mio": p.created_by == user.id,
    }


@router.get("")
def list_places(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Lugares guardados: los propios y los compartidos con toda la empresa."""
    rows = db.scalars(
        select(SavedPlace).where(or_(SavedPlace.created_by == user.id, SavedPlace.compartido.is_(True))).order_by(SavedPlace.nombre)
    ).all()
    return [place_out(p, user) for p in rows]


@router.post("")
def save_place(body: SavedPlaceIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    nombre = body.nombre.strip()
    if not nombre:
        raise HTTPException(status_code=400, detail="Ponle un nombre al lugar")
    propios = db.scalars(select(SavedPlace).where(SavedPlace.created_by == user.id)).all()
    # Mismo nombre: se actualiza el punto (por ejemplo «Casa» al mudarse).
    for p in propios:
        if p.nombre.lower() == nombre.lower():
            p.lat, p.lng, p.direccion = body.lat, body.lng, (body.direccion or "").strip() or None
            if body.compartido is not None and is_staff(user):
                p.compartido = body.compartido
            return place_out(p, user)
    if len(propios) >= MAX_PER_USER:
        raise HTTPException(status_code=400, detail=f"Llegaste al máximo de {MAX_PER_USER} lugares guardados; borra alguno")
    # Solo el personal de control comparte lugares con toda la empresa (clientes, almacenes).
    compartido = bool(body.compartido) if is_staff(user) else False
    p = SavedPlace(nombre=nombre, direccion=(body.direccion or "").strip() or None, lat=body.lat, lng=body.lng, compartido=compartido, created_by=user.id)
    db.add(p)
    db.flush()
    return place_out(p, user)


@router.delete("/{place_id}")
def delete_place(place_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    p = db.get(SavedPlace, place_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Lugar no encontrado")
    if p.created_by != user.id and not (p.compartido and is_staff(user)):
        raise HTTPException(status_code=403, detail="Solo quien guardó el lugar puede borrarlo")
    db.delete(p)
    return {"ok": True}

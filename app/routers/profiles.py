from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user, hash_password, require_staff
from app.database import get_db
from app.models import User
from app.schemas import CreateUserIn, ProfileUpdateIn
from app.serializers import profile_out

router = APIRouter(prefix="/profiles", tags=["profiles"])
ALLOWED_ROLES = {"admin", "supervisor", "conductor"}


@router.get("")
def list_profiles(_user: User = Depends(require_staff), db: Session = Depends(get_db)):
    rows = db.scalars(select(User).order_by(User.full_name)).all()
    return [profile_out(u, include_created=True) for u in rows]


@router.patch("/{user_id}")
def update_profile(
    user_id: str,
    body: ProfileUpdateIn,
    actor: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Perfil no encontrado")
    if body.full_name is not None:
        name = body.full_name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="El nombre no puede estar vacío")
        user.full_name = name
    if body.role is not None:
        if body.role not in ALLOWED_ROLES:
            raise HTTPException(status_code=400, detail="Rol inválido")
        if actor.id == user.id and actor.role not in ("admin", "supervisor"):
            raise HTTPException(status_code=403, detail="No puedes cambiar tu propio rol")
        user.role = body.role
    db.add(user)
    db.flush()
    return profile_out(user, include_created=True)


@router.post("")
def create_profile(body: CreateUserIn, _user: User = Depends(require_staff), db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status_code=400, detail="Ya existe un usuario con ese email")
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="La contraseña debe tener al menos 6 caracteres")
    role = body.role if body.role in ALLOWED_ROLES else "conductor"
    user = User(
        full_name=body.full_name.strip(),
        email=email,
        password_hash=hash_password(body.password),
        role=role,
    )
    db.add(user)
    db.flush()
    return profile_out(user, include_created=True)

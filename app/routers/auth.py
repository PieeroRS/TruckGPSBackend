import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import create_access_token, get_current_user, hash_password, verify_password
from app.database import get_db
from app.models import User
from app.schemas import LoginIn, ProfileUpdateIn
from app.serializers import profile_out

router = APIRouter(prefix="/auth", tags=["auth"])


def normalize_email(identifier: str) -> str:
    value = identifier.strip().lower()
    if "@" not in value:
        value = f"{value}@truckgps.pe"
    return value


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    email = normalize_email(body.email)
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Credenciales inválidas")
    return {
        "access_token": create_access_token(user),
        "token_type": "bearer",
        "profile": profile_out(user),
    }


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return profile_out(user)


@router.patch("/me")
def update_me(body: ProfileUpdateIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if body.full_name is not None:
        name = body.full_name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="El nombre no puede estar vacío")
        user.full_name = name
    if body.telefono is not None:
        tel = body.telefono.strip()
        if tel and not re.fullmatch(r"\+?[0-9 ]{6,20}", tel):
            raise HTTPException(status_code=400, detail="Teléfono inválido: usa solo números (opcional +51)")
        user.telefono = tel or None
    if body.licencia is not None:
        lic = body.licencia.strip().upper()
        if lic and not re.fullmatch(r"[A-Z0-9-]{5,15}", lic):
            raise HTTPException(status_code=400, detail="N.° de licencia inválido (5 a 15 letras o números)")
        user.licencia = lic or None
    db.add(user)
    db.flush()
    return profile_out(user)

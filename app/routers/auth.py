import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import accounts
from app import app_settings as cfg
from app.auth import create_access_token, get_current_user, hash_password, verify_password
from app.database import get_db
from app.models import User
from app.schemas import LoginIn, ProfileUpdateIn
from app.serializers import profile_out

NO_VERIFICADO = "Tu correo aún no está verificado. Abre el enlace que te enviamos o pide uno nuevo."

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
    if not user.email_verificado:
        raise HTTPException(status_code=403, detail=NO_VERIFICADO, headers={"X-Motivo": "correo_no_verificado"})
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


# ─────────────── CP001: crear cuenta y verificar el correo ───────────────

@router.post("/registro")
def registro(body: dict, db: Session = Depends(get_db)):
    nombre = str(body.get("full_name", "")).strip()
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    if len(nombre) < 3:
        raise HTTPException(status_code=400, detail="Escribe tu nombre y apellido")
    if not accounts.EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Escribe un correo válido")
    try:
        accounts.validar_password(password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if body.get("password2") is not None and body.get("password2") != password:
        raise HTTPException(status_code=400, detail="Las contraseñas no coinciden")
    reg = cfg.get_registro(db)
    if not reg["activo"]:
        raise HTTPException(status_code=403, detail="El registro de cuentas está desactivado. Pide a tu supervisor que te cree la cuenta.")
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status_code=400, detail="Ya existe una cuenta con ese correo. Inicia sesión o recupera tu contraseña.")
    user = User(
        full_name=nombre,
        email=email,
        password_hash=hash_password(password),
        role="conductor",
        email_verificado=False,
        onboarding_ok=False,
    )
    db.add(user)
    db.flush()
    enviado, error = accounts.enviar_verificacion(db, user)
    return {
        "ok": True,
        "email": email,
        "correoEnviado": enviado,
        "mensaje": (
            f"Te enviamos un correo a {email} para verificar tu cuenta."
            if enviado
            else "Tu cuenta se creó, pero no pudimos enviar el correo de verificación. Pide a tu supervisor que la active desde Equipo."
        ),
    }


@router.post("/verificar")
def verificar(body: dict, db: Session = Depends(get_db)):
    try:
        data = accounts.leer_token(str(body.get("token", "")), "verificar")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    user = db.get(User, data.get("sub")) or db.scalar(select(User).where(User.email == data.get("email")))
    if user is None:
        raise HTTPException(status_code=404, detail="La cuenta ya no existe")
    user.email_verificado = True
    db.flush()
    # Al verificar se inicia sesión: el siguiente paso es el asistente de configuración.
    return {"access_token": create_access_token(user), "token_type": "bearer", "profile": profile_out(user)}


@router.post("/reenviar-verificacion")
def reenviar_verificacion(body: dict, db: Session = Depends(get_db)):
    email = normalize_email(str(body.get("email", "")))
    user = db.scalar(select(User).where(User.email == email))
    if user is not None and not user.email_verificado:
        accounts.enviar_verificacion(db, user)
    # Misma respuesta exista o no la cuenta (no revela qué correos están registrados).
    return {"ok": True, "mensaje": "Si la cuenta existe y falta verificarla, te enviamos un nuevo correo."}


# ─────────────── CP004: recuperar la contraseña ───────────────

@router.post("/recuperar")
def recuperar(body: dict, db: Session = Depends(get_db)):
    email = normalize_email(str(body.get("email", "")))
    if not accounts.EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Escribe un correo válido")
    user = db.scalar(select(User).where(User.email == email))
    if user is not None:
        accounts.enviar_recuperacion(db, user)
    return {"ok": True, "mensaje": f"Si {email} está registrado, te enviamos un enlace para crear una nueva contraseña. Revisa también la carpeta de spam."}


@router.get("/restablecer/validar")
def validar_restablecer(token: str, db: Session = Depends(get_db)):
    try:
        data = accounts.leer_token(token, "recuperar")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    user = db.get(User, data.get("sub"))
    if user is None or not accounts.token_recuperacion_vigente(user, data):
        raise HTTPException(status_code=400, detail="Este enlace ya se usó o venció. Pide uno nuevo.")
    return {"ok": True, "email": user.email}


@router.post("/restablecer")
def restablecer(body: dict, db: Session = Depends(get_db)):
    try:
        data = accounts.leer_token(str(body.get("token", "")), "recuperar")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    password = str(body.get("password", ""))
    if password != str(body.get("password2", "")):
        raise HTTPException(status_code=400, detail="Las contraseñas no coinciden")
    try:
        accounts.validar_password(password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    user = db.get(User, data.get("sub"))
    if user is None or not accounts.token_recuperacion_vigente(user, data):
        raise HTTPException(status_code=400, detail="Este enlace ya se usó o venció. Pide uno nuevo.")
    user.password_hash = hash_password(password)
    # Quien recupera la contraseña desde su correo también demuestra que el correo es suyo.
    user.email_verificado = True
    db.flush()
    return {"ok": True, "mensaje": "Tu contraseña se actualizó. Ya puedes iniciar sesión con la nueva."}


# ─────────────── CP002: preferencias y contraseña desde la app ───────────────

@router.get("/me/preferencias")
def get_preferencias(user: User = Depends(get_current_user)):
    return accounts.preferencias_de(user)


@router.put("/me/preferencias")
def put_preferencias(body: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        user.preferencias = accounts.validar_preferencias(body.get("preferencias", body), accounts.preferencias_de(user))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if body.get("onboarding_ok"):
        user.onboarding_ok = True
    db.flush()
    return profile_out(user)


@router.post("/me/password")
def cambiar_password(body: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not verify_password(str(body.get("actual", "")), user.password_hash):
        raise HTTPException(status_code=400, detail="La contraseña actual no es correcta")
    nueva = str(body.get("nueva", ""))
    if nueva != str(body.get("nueva2", nueva)):
        raise HTTPException(status_code=400, detail="Las contraseñas nuevas no coinciden")
    try:
        accounts.validar_password(nueva)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    user.password_hash = hash_password(nueva)
    db.flush()
    return {"ok": True, "mensaje": "Contraseña actualizada"}

"""Cuentas: registro con verificación de correo, recuperación de contraseña y preferencias.

Los enlaces de verificación y de recuperación son tokens firmados con SECRET_KEY:
no hace falta guardarlos en la base. El de recuperación incluye una huella de la
contraseña actual, así deja de servir apenas se usa (o si la contraseña cambia).
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import secrets
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy.orm import Session

from app.config import settings
from app.models import NotificationLog, User

VERIFY_HOURS = 48
RESET_MINUTES = 60
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")

PREFERENCIAS_DEFAULT = {
    "seguridad": {
        # Cierra la sesión si no se usa la app durante estos minutos (0 = nunca).
        "cerrar_por_inactividad_min": 0,
        # Pide confirmación antes de acciones críticas (descartar alertas, eliminar, cerrar sesión).
        "confirmar_acciones": True,
    },
    "notificaciones": {
        "en_app": True,  # aviso dentro de la app con sonido o vibración
        "sonido": True,
        "correo": False,  # el supervisor recibe por correo las alertas importantes
        "alta_severidad": True,
        "desvios": True,
        "velocidad": True,
        "incidencias": True,
    },
}
INACTIVIDAD_OPCIONES = {0, 15, 30, 60, 120}


def preferencias_de(user: User) -> dict:
    data = deepcopy(PREFERENCIAS_DEFAULT)
    guardado = user.preferencias or {}
    for grupo, valores in data.items():
        if isinstance(guardado.get(grupo), dict):
            for k in valores:
                if k in guardado[grupo]:
                    valores[k] = guardado[grupo][k]
    return data


def validar_preferencias(nuevas: dict, actuales: dict) -> dict:
    data = deepcopy(actuales)
    for grupo, valores in data.items():
        cambios = nuevas.get(grupo)
        if not isinstance(cambios, dict):
            continue
        for k, v in cambios.items():
            if k not in valores:
                continue
            if k == "cerrar_por_inactividad_min":
                try:
                    v = int(v)
                except (TypeError, ValueError) as exc:
                    raise ValueError("Tiempo de inactividad inválido") from exc
                if v not in INACTIVIDAD_OPCIONES:
                    raise ValueError("Elige 15, 30, 60 o 120 minutos, o «nunca»")
                valores[k] = v
            else:
                valores[k] = bool(v)
    return data


def validar_password(password: str) -> None:
    if len(password) < 8:
        raise ValueError("La contraseña debe tener al menos 8 caracteres")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        raise ValueError("La contraseña debe tener letras y números")


# ─────────────── tokens firmados ───────────────

def _huella(user: User) -> str:
    return hashlib.sha256(user.password_hash.encode()).hexdigest()[:16]


def token_verificacion(user: User) -> str:
    payload = {
        "sub": user.id,
        "email": user.email,
        "fin": "verificar",
        "exp": datetime.now(timezone.utc) + timedelta(hours=VERIFY_HOURS),
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def token_recuperacion(user: User) -> str:
    payload = {
        "sub": user.id,
        "email": user.email,
        "fin": "recuperar",
        "h": _huella(user),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=RESET_MINUTES),
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def leer_token(token: str, fin: str) -> dict:
    try:
        data = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise ValueError("El enlace venció. Pide uno nuevo.") from exc
    except jwt.InvalidTokenError as exc:
        raise ValueError("El enlace no es válido.") from exc
    if data.get("fin") != fin:
        raise ValueError("El enlace no es válido.")
    return data


def token_recuperacion_vigente(user: User, data: dict) -> bool:
    return data.get("h") == _huella(user)


def nuevo_codigo_empresa() -> str:
    alfabeto = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "TG-" + "".join(secrets.choice(alfabeto) for _ in range(6))


# ─────────────── correos de cuenta ───────────────

def _link(path: str, token: str) -> str:
    return f"{settings.app_url.rstrip('/')}{path}?token={token}"


def _html(titulo: str, parrafos: list[str], boton: str, url: str, pie: str) -> str:
    cuerpo = "".join(f'<p style="margin:8px 0;color:#334155;line-height:1.5">{html.escape(p)}</p>' for p in parrafos)
    return (
        '<div style="font-family:Arial,sans-serif;max-width:520px;margin:auto">'
        '<div style="padding:18px 20px;background:#f8fafc;border-radius:10px;border:1px solid #e2e8f0">'
        f'<h2 style="margin:0 0 6px;font-size:19px;color:#0b1220">{html.escape(titulo)}</h2>{cuerpo}'
        f'<a href="{html.escape(url)}" style="display:inline-block;margin-top:14px;padding:11px 18px;background:#0b1220;'
        f'color:#fff;border-radius:8px;text-decoration:none;font-weight:600">{html.escape(boton)}</a>'
        f'<p style="font-size:11.5px;color:#64748b;margin-top:16px">Si el botón no abre, copia este enlace en tu navegador:<br>{html.escape(url)}</p>'
        "</div>"
        f'<p style="font-size:11px;color:#94a3b8;margin-top:12px">{html.escape(pie)}</p></div>'
    )


def _enviar(db: Session, to: str, asunto: str, html_body: str, texto: str, tipo: str) -> tuple[bool, str | None]:
    """Envía un correo a una persona (no a la lista de supervisores). Devuelve (enviado, error)."""
    from app.notify import correo_configurado, enviar_correo

    if not correo_configurado():
        error = "El envío de correos no está configurado (faltan SMTP_USER/SMTP_PASSWORD o RESEND_API_KEY)."
        estado = "sin_configurar"
    else:
        try:
            enviar_correo([to], asunto, html_body, texto)
            error, estado = None, "enviado"
        except Exception as exc:  # noqa: BLE001 - el registro no debe romperse por el correo
            error, estado = str(exc)[:500], "fallido"
    db.add(NotificationLog(tipo=tipo, titulo=asunto, mensaje=texto, canal="email", destinatarios=to, estado=estado, error=error))
    db.flush()
    return estado == "enviado", error


def enviar_verificacion(db: Session, user: User) -> tuple[bool, str | None]:
    url = _link("/verificar", token_verificacion(user))
    nombre = user.full_name.split()[0] if user.full_name else ""
    parrafos = [
        f"Hola {nombre}, gracias por crear tu cuenta en TruckGPS.",
        "Confirma que este correo es tuyo para activar la cuenta. El enlace vence en 48 horas.",
    ]
    texto = "\n".join([*parrafos, f"Verificar correo: {url}"])
    return _enviar(db, user.email, "Verifica tu correo en TruckGPS", _html("Verifica tu correo", parrafos, "Verificar mi correo", url,
                   "Si no creaste esta cuenta, ignora este mensaje."), texto, "cuenta")


def enviar_recuperacion(db: Session, user: User) -> tuple[bool, str | None]:
    url = _link("/restablecer", token_recuperacion(user))
    parrafos = [
        "Recibimos un pedido para restablecer la contraseña de tu cuenta de TruckGPS.",
        "Usa el botón para crear una nueva. El enlace vence en 60 minutos y sirve una sola vez.",
    ]
    texto = "\n".join([*parrafos, f"Restablecer contraseña: {url}"])
    return _enviar(db, user.email, "Restablece tu contraseña de TruckGPS", _html("Restablecer contraseña", parrafos, "Crear nueva contraseña", url,
                   "Si no lo pediste, ignora este correo: tu contraseña no cambia."), texto, "cuenta")


def correos_con_aviso(db: Session) -> list[str]:
    """Supervisores y administradores que pidieron recibir las alertas por correo."""
    from sqlalchemy import select

    out = []
    for u in db.scalars(select(User).where(User.role.in_(("admin", "supervisor")))).all():
        if u.email_verificado and preferencias_de(u)["notificaciones"]["correo"]:
            out.append(u.email)
    return out

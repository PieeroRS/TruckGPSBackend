"""Envío de avisos al supervisor por correo (Resend) y SMS (Twilio).

Cada canal se activa solo si sus variables de entorno están definidas. Todo
envío queda registrado en `notification_logs`, también los que fallan o no
están configurados, para que el supervisor vea en la app qué pasó.
"""
from __future__ import annotations

import base64
import html
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.config import settings
from app.models import NotificationLog

TIMEOUT_S = 6


@dataclass
class Alert:
    tipo: str  # panico | incidente | zona | prueba
    titulo: str
    lineas: list[str] = field(default_factory=list)
    lat: float | None = None
    lng: float | None = None
    vehicle_id: str | None = None
    zone_id: str | None = None
    incident_id: str | None = None
    path: str = "/dashboard"

    @property
    def maps_url(self) -> str | None:
        if self.lat is None or self.lng is None:
            return None
        return f"https://www.google.com/maps?q={self.lat:.6f},{self.lng:.6f}"

    @property
    def app_link(self) -> str:
        return settings.app_url.rstrip("/") + self.path

    def text(self) -> str:
        partes = [self.titulo, *self.lineas]
        if self.maps_url:
            partes.append(f"Mapa: {self.maps_url}")
        partes.append(f"TruckGPS: {self.app_link}")
        return "\n".join(partes)

    def sms(self) -> str:
        partes = [f"TruckGPS - {self.titulo}", *self.lineas[:2]]
        if self.maps_url:
            partes.append(self.maps_url)
        return "\n".join(partes)[:600]

    def html(self) -> str:
        color = {"panico": "#dc2626", "incidente": "#d97706", "zona": "#ea580c"}.get(self.tipo, "#0b1220")
        filas = "".join(f'<p style="margin:4px 0;color:#334155">{html.escape(l)}</p>' for l in self.lineas)
        mapa = (
            f'<a href="{html.escape(self.maps_url)}" style="display:inline-block;margin:12px 8px 0 0;padding:10px 16px;'
            f'background:#0b1220;color:#fff;border-radius:8px;text-decoration:none;font-weight:600">Ver en el mapa</a>'
            if self.maps_url
            else ""
        )
        app = (
            f'<a href="{html.escape(self.app_link)}" style="display:inline-block;margin-top:12px;padding:10px 16px;'
            f'border:1px solid #cbd5e1;color:#0b1220;border-radius:8px;text-decoration:none;font-weight:600">Abrir TruckGPS</a>'
        )
        return (
            '<div style="font-family:Arial,sans-serif;max-width:520px;margin:auto">'
            f'<div style="border-left:4px solid {color};padding:12px 16px;background:#f8fafc;border-radius:6px">'
            f'<h2 style="margin:0 0 8px;font-size:18px;color:{color}">{html.escape(self.titulo)}</h2>'
            f"{filas}{mapa}{app}</div>"
            '<p style="font-size:11px;color:#94a3b8;margin-top:12px">Aviso automático de TruckGPS.</p></div>'
        )


def split_list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def email_configured() -> bool:
    return bool(settings.resend_api_key and split_list(settings.alert_emails))


def sms_configured() -> bool:
    return bool(
        settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_from and split_list(settings.alert_phones)
    )


def _post(url: str, data: bytes, headers: dict[str, str]) -> None:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        detalle = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {exc.code}: {detalle}") from exc


def _send_email(alert: Alert, to: list[str]) -> None:
    body = json.dumps(
        {
            "from": settings.alerts_from_email,
            "to": to,
            "subject": f"[TruckGPS] {alert.titulo}",
            "html": alert.html(),
            "text": alert.text(),
        }
    ).encode("utf-8")
    _post(
        "https://api.resend.com/emails",
        body,
        {"Authorization": f"Bearer {settings.resend_api_key}", "Content-Type": "application/json"},
    )


def _send_sms(alert: Alert, phone: str) -> None:
    sid = settings.twilio_account_sid
    token = base64.b64encode(f"{sid}:{settings.twilio_auth_token}".encode()).decode()
    data = urllib.parse.urlencode({"To": phone, "From": settings.twilio_from, "Body": alert.sms()}).encode()
    _post(
        f"https://api.twilio.com/2010-04-01/Accounts/{urllib.parse.quote(sid)}/Messages.json",
        data,
        {"Authorization": f"Basic {token}", "Content-Type": "application/x-www-form-urlencoded"},
    )


def _log(db: Session, alert: Alert, canal: str, destinatarios: list[str], estado: str, error: str | None) -> NotificationLog:
    row = NotificationLog(
        tipo=alert.tipo,
        titulo=alert.titulo,
        mensaje=alert.text(),
        canal=canal,
        destinatarios=", ".join(destinatarios),
        estado=estado,
        error=error,
        vehicle_id=alert.vehicle_id,
        zone_id=alert.zone_id,
        incident_id=alert.incident_id,
    )
    db.add(row)
    return row


def send_alert(db: Session, alert: Alert) -> list[NotificationLog]:
    """Envía el aviso por todos los canales configurados. Nunca lanza excepción."""
    logs: list[NotificationLog] = []

    emails = split_list(settings.alert_emails)
    if email_configured():
        try:
            _send_email(alert, emails)
            logs.append(_log(db, alert, "email", emails, "enviado", None))
        except Exception as exc:  # noqa: BLE001 - el aviso no debe romper la operación principal
            logs.append(_log(db, alert, "email", emails, "fallido", str(exc)[:500]))
    else:
        logs.append(_log(db, alert, "email", emails, "sin_configurar", "Falta RESEND_API_KEY o ALERT_EMAILS"))

    if sms_configured():
        for phone in split_list(settings.alert_phones):
            try:
                _send_sms(alert, phone)
                logs.append(_log(db, alert, "sms", [phone], "enviado", None))
            except Exception as exc:  # noqa: BLE001
                logs.append(_log(db, alert, "sms", [phone], "fallido", str(exc)[:500]))

    db.flush()
    return logs

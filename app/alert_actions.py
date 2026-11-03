"""Catálogo de acciones de respuesta de las alertas y recomendaciones por tipo."""
from __future__ import annotations

from app.models import AiAlert

ACTIONS: dict[str, tuple[str, str]] = {
    "atender": ("Atender", "Me asigno la alerta y queda en atención."),
    "contactar_conductor": ("Contactar al conductor", "Registra el contacto con el conductor de la unidad."),
    "crear_incidencia": ("Crear incidencia", "Abre una incidencia con la ubicación y el detalle de la alerta."),
    "notificar_supervisor": ("Notificar por correo", "Envía el detalle de la alerta a los correos de supervisión."),
    "recalcular_ruta": ("Recalcular ruta", "Calcula una nueva ruta desde la posición actual y la asigna a la unidad."),
    "aplicar_ruta_alternativa": ("Aplicar ruta alternativa", "Cambia la ruta de la unidad por la alternativa que evita el tráfico."),
    "mantener_ruta": ("Mantener ruta actual", "Se conserva la ruta y se deja constancia de la decisión."),
    "activar_protocolo": (
        "Activar protocolo de seguridad",
        "Pone la unidad en alerta, crea una incidencia de severidad alta y avisa por correo.",
    ),
    "ver_mapa": ("Ver en el mapa", "Abre el mapa en vivo centrado en la unidad."),
    "resolver": ("Marcar como resuelta", "Cierra la alerta."),
    "descartar": ("Descartar (falso positivo)", "Cierra la alerta como falsa alarma."),
}

RECOMMENDED: dict[str, list[str]] = {
    "desvio": ["recalcular_ruta", "contactar_conductor", "crear_incidencia", "notificar_supervisor"],
    "spoofing": ["activar_protocolo", "contactar_conductor", "crear_incidencia"],
    "jamming": ["activar_protocolo", "contactar_conductor", "crear_incidencia"],
    "salida_zona": ["contactar_conductor", "crear_incidencia", "notificar_supervisor"],
    "parada_riesgo": ["contactar_conductor", "crear_incidencia"],
    "trafico": ["aplicar_ruta_alternativa", "mantener_ruta", "contactar_conductor"],
    "sabotaje": ["activar_protocolo", "contactar_conductor", "crear_incidencia", "notificar_supervisor"],
}

# Acciones que solo tienen sentido una vez por alerta.
ONE_SHOT = {"crear_incidencia", "recalcular_ruta", "aplicar_ruta_alternativa", "mantener_ruta", "activar_protocolo"}
CLOSED = {"resuelta", "descartada"}


def _has_vehicle(alert: AiAlert) -> bool:
    return bool(alert.vehicle_id or (alert.placas_afectadas or []))


def available_actions(alert: AiAlert) -> list[dict]:
    """Acciones que se pueden ejecutar ahora sobre la alerta, con las recomendadas primero."""
    if (alert.estado or "activa") in CLOSED:
        return []
    tipo = alert.tipo or "manual"
    if tipo in RECOMMENDED:
        rec = list(RECOMMENDED[tipo])
    elif alert.severidad == "high":
        rec = ["crear_incidencia", "notificar_supervisor", "contactar_conductor"]
    else:
        rec = ["crear_incidencia", "contactar_conductor"]

    payload = alert.payload or {}
    if "aplicar_ruta_alternativa" in rec and not payload.get("alternativa"):
        rec.remove("aplicar_ruta_alternativa")
    if not _has_vehicle(alert):
        rec = [c for c in rec if c not in {"contactar_conductor", "recalcular_ruta", "activar_protocolo"}]
    if "recalcular_ruta" in rec and not alert.nav_route_id:
        rec.remove("recalcular_ruta")

    done = {h.get("codigo") for h in (alert.historial or [])}
    codes = rec[:]
    if (alert.estado or "activa") == "activa":
        codes.append("atender")
    if alert.lat is not None or alert.vehicle_id:
        codes.append("ver_mapa")
    codes += ["resolver", "descartar"]

    out = []
    for c in codes:
        label, desc = ACTIONS[c]
        out.append(
            {
                "codigo": c,
                "label": label,
                "descripcion": desc,
                "recomendada": c in rec,
                "ejecutada": c in done and c in ONE_SHOT,
            }
        )
    return out

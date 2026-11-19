"""Configuración que el supervisor o el administrador editan desde la app.

- umbrales: límites que usa el motor de detección (desvío, velocidad, señal, detención).
- interfaz: qué módulos ve cada rol y en qué pantalla entra.
- flota: datos generales de la empresa y su flota (Mi perfil).
"""
from __future__ import annotations

from copy import deepcopy

from sqlalchemy.orm import Session

from app.models import AppSetting, utcnow

UMBRALES_DEFAULT = {
    "desvio_m": 250,
    "desvio_alto_m": 1000,
    "velocidad_max_kmh": 90,
    "velocidad_margen_alta_kmh": 20,
    "sin_senal_s": 300,
    "parada_riesgo_min": 2,
}

# (mínimo, máximo) permitido para cada umbral.
UMBRALES_RANGO = {
    "desvio_m": (50, 5000),
    "desvio_alto_m": (100, 20000),
    "velocidad_max_kmh": (30, 140),
    "velocidad_margen_alta_kmh": (5, 60),
    "sin_senal_s": (60, 3600),
    "parada_riesgo_min": (1, 60),
}

# Módulos de la app. "fijo": siempre visible para ese rol (no se puede quitar).
MODULOS = [
    {"key": "dashboard", "label": "Dashboard"},
    {"key": "rutas", "label": "Rutas"},
    {"key": "mapa-vivo", "label": "Mapa en Vivo"},
    {"key": "incidencias", "label": "Incidencias"},
    {"key": "flota", "label": "Flota"},
    {"key": "consumo", "label": "Consumo"},
    {"key": "reportes", "label": "Reportes"},
    {"key": "mi-ubicacion", "label": "Mi ubicación en vivo"},
    {"key": "reportar", "label": "Reportar incidencia"},
    {"key": "zonas", "label": "Zonas y avisos", "solo_staff": True},
    {"key": "simulador", "label": "Simulador", "solo_staff": True},
    {"key": "equipo", "label": "Equipo", "solo_staff": True},
    {"key": "configuracion", "label": "Configuración", "solo_staff": True},
]
MODULE_KEYS = [m["key"] for m in MODULOS]
STAFF_ONLY = {m["key"] for m in MODULOS if m.get("solo_staff")}

INTERFAZ_DEFAULT = {
    "conductor": {
        "modulos": ["dashboard", "rutas", "mapa-vivo", "incidencias", "flota", "consumo", "reportes", "mi-ubicacion", "reportar"],
        "inicio": "dashboard",
    },
    "supervisor": {"modulos": list(MODULE_KEYS), "inicio": "dashboard"},
    "admin": {"modulos": list(MODULE_KEYS), "inicio": "dashboard"},
}
# Lo mínimo que cada rol necesita para no quedar sin salida.
INTERFAZ_FIJOS = {"conductor": {"reportar"}, "supervisor": {"configuracion"}, "admin": {"configuracion", "equipo"}}

FLOTA_DEFAULT = {
    "empresa": "",
    "ruc": "",
    "sede": "",
    "telefono_central": "",
    "correo_central": "",
}


def _get(db: Session, clave: str) -> dict | None:
    row = db.get(AppSetting, clave)
    return dict(row.valor) if row and row.valor else None


def _set(db: Session, clave: str, valor: dict, usuario: str | None) -> None:
    row = db.get(AppSetting, clave)
    if row is None:
        row = AppSetting(clave=clave)
        db.add(row)
    row.valor = valor
    row.updated_by = usuario
    row.updated_at = utcnow()
    db.flush()


def meta(db: Session, clave: str) -> dict:
    row = db.get(AppSetting, clave)
    if row is None:
        return {"actualizadoPor": None, "actualizado": None}
    from app.schemas import iso

    return {"actualizadoPor": row.updated_by, "actualizado": iso(row.updated_at)}


# ─────────────── umbrales ───────────────

def get_umbrales(db: Session) -> dict:
    data = deepcopy(UMBRALES_DEFAULT)
    data.update({k: v for k, v in (_get(db, "umbrales") or {}).items() if k in UMBRALES_DEFAULT})
    return data


def set_umbrales(db: Session, cambios: dict, usuario: str | None) -> dict:
    data = get_umbrales(db)
    for k, v in cambios.items():
        if k not in UMBRALES_DEFAULT or v is None:
            continue
        lo, hi = UMBRALES_RANGO[k]
        try:
            num = float(v)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Valor inválido para {k}") from exc
        if not lo <= num <= hi:
            raise ValueError(f"{k} debe estar entre {lo} y {hi}")
        data[k] = int(round(num))
    if data["desvio_alto_m"] <= data["desvio_m"]:
        raise ValueError("El desvío grave debe ser mayor que el desvío leve")
    _set(db, "umbrales", data, usuario)
    return data


# ─────────────── interfaz por rol ───────────────

def get_interfaz(db: Session) -> dict:
    data = deepcopy(INTERFAZ_DEFAULT)
    guardado = _get(db, "interfaz") or {}
    for rol, conf in guardado.items():
        if rol in data and isinstance(conf, dict):
            mods = [m for m in conf.get("modulos", []) if m in MODULE_KEYS]
            data[rol]["modulos"] = mods
            if conf.get("inicio") in MODULE_KEYS:
                data[rol]["inicio"] = conf["inicio"]
    return _normalize_interfaz(data)


def _normalize_interfaz(data: dict) -> dict:
    for rol, conf in data.items():
        mods = [m for m in MODULE_KEYS if m in set(conf["modulos"]) | INTERFAZ_FIJOS.get(rol, set())]
        if rol == "conductor":
            mods = [m for m in mods if m not in STAFF_ONLY]
        conf["modulos"] = mods
        if conf.get("inicio") not in mods:
            conf["inicio"] = mods[0]
    return data


def set_interfaz(db: Session, nueva: dict, usuario: str | None) -> dict:
    data = get_interfaz(db)
    for rol in ("conductor", "supervisor", "admin"):
        conf = nueva.get(rol)
        if not isinstance(conf, dict):
            continue
        mods = [m for m in conf.get("modulos", []) if m in MODULE_KEYS]
        data[rol]["modulos"] = mods
        if conf.get("inicio") in MODULE_KEYS:
            data[rol]["inicio"] = conf["inicio"]
    data = _normalize_interfaz(data)
    _set(db, "interfaz", data, usuario)
    return data


def modulos_de(db: Session, rol: str) -> list[str]:
    return get_interfaz(db).get(rol, INTERFAZ_DEFAULT["conductor"])["modulos"]


# ─────────────── datos de flota ───────────────

def get_flota(db: Session) -> dict:
    data = deepcopy(FLOTA_DEFAULT)
    data.update({k: v for k, v in (_get(db, "flota") or {}).items() if k in FLOTA_DEFAULT})
    return data


def set_flota(db: Session, cambios: dict, usuario: str | None) -> dict:
    data = get_flota(db)
    for k, v in cambios.items():
        if k in FLOTA_DEFAULT and v is not None:
            val = str(v).strip()[:200]
            if k == "ruc" and val and not (val.isdigit() and len(val) == 11):
                raise ValueError("El RUC debe tener 11 dígitos")
            data[k] = val
    _set(db, "flota", data, usuario)
    return data


# ─────────────── parámetros de detección (sensibilidad y sabotaje) ───────────────

DETECCION_DEFAULT = {
    "sensibilidad": "media",
    "umbral_sabotaje": 60,
    "peso_desvio": 25,
    "peso_spoofing": 35,
    "peso_jamming": 35,
    "peso_parada_riesgo": 20,
    "peso_salida_zona": 20,
    "spoofing_salto_m": 2000,
    "spoofing_kmh": 200,
}
DETECCION_RANGO = {
    "umbral_sabotaje": (20, 100),
    "peso_desvio": (0, 60),
    "peso_spoofing": (0, 60),
    "peso_jamming": (0, 60),
    "peso_parada_riesgo": (0, 60),
    "peso_salida_zona": (0, 60),
    "spoofing_salto_m": (500, 20000),
    "spoofing_kmh": (120, 500),
}
SENSIBILIDADES = ("baja", "media", "alta", "personalizada")
# Cada nivel ajusta a la vez los umbrales del motor y los parámetros de sabotaje.
# "alta" detecta antes (más alertas, más falsas alarmas); "baja" espera más evidencia.
PRESETS = {
    "alta": {
        "umbrales": {"desvio_m": 150, "desvio_alto_m": 700, "sin_senal_s": 180, "parada_riesgo_min": 1},
        "deteccion": {"umbral_sabotaje": 45, "spoofing_salto_m": 1000, "spoofing_kmh": 160},
    },
    "media": {
        "umbrales": {"desvio_m": 250, "desvio_alto_m": 1000, "sin_senal_s": 300, "parada_riesgo_min": 2},
        "deteccion": {"umbral_sabotaje": 60, "spoofing_salto_m": 2000, "spoofing_kmh": 200},
    },
    "baja": {
        "umbrales": {"desvio_m": 400, "desvio_alto_m": 1500, "sin_senal_s": 600, "parada_riesgo_min": 5},
        "deteccion": {"umbral_sabotaje": 75, "spoofing_salto_m": 3000, "spoofing_kmh": 250},
    },
}


def get_deteccion(db: Session) -> dict:
    data = deepcopy(DETECCION_DEFAULT)
    data.update({k: v for k, v in (_get(db, "deteccion") or {}).items() if k in DETECCION_DEFAULT})
    return data


def set_deteccion(db: Session, cambios: dict, usuario: str | None) -> dict:
    data = get_deteccion(db)
    for k, v in cambios.items():
        if k not in DETECCION_RANGO or v is None:
            continue
        lo, hi = DETECCION_RANGO[k]
        try:
            num = float(v)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Valor inválido para {k}") from exc
        if not lo <= num <= hi:
            raise ValueError(f"{k} debe estar entre {lo} y {hi}")
        data[k] = int(round(num))
    pesos = sum(data[k] for k in data if k.startswith("peso_"))
    if pesos * 2 < data["umbral_sabotaje"]:
        raise ValueError("Con esos pesos el puntaje nunca llegaría al umbral de sabotaje. Sube algún peso o baja el umbral.")
    data["sensibilidad"] = cambios.get("sensibilidad") if cambios.get("sensibilidad") in SENSIBILIDADES else "personalizada"
    _set(db, "deteccion", data, usuario)
    return data


def aplicar_sensibilidad(db: Session, nivel: str, usuario: str | None) -> tuple[dict, dict]:
    if nivel not in PRESETS:
        raise ValueError("Nivel de sensibilidad inválido")
    p = PRESETS[nivel]
    umbrales = set_umbrales(db, p["umbrales"], usuario)
    deteccion = set_deteccion(db, {**DETECCION_DEFAULT, **p["deteccion"], "sensibilidad": nivel}, usuario)
    return umbrales, deteccion


def pesos_sabotaje(db: Session) -> dict:
    d = get_deteccion(db)
    return {
        "desvio": d["peso_desvio"],
        "spoofing": d["peso_spoofing"],
        "jamming": d["peso_jamming"],
        "parada_riesgo": d["peso_parada_riesgo"],
        "salida_zona": d["peso_salida_zona"],
    }


# ─────────────── registro de cuentas ───────────────

def get_registro(db: Session) -> dict:
    data = _get(db, "registro")
    if not data or not data.get("codigo"):
        from app.accounts import nuevo_codigo_empresa

        data = {"codigo": nuevo_codigo_empresa(), "activo": True}
        _set(db, "registro", data, None)
    return {"codigo": data["codigo"], "activo": bool(data.get("activo", True))}


def set_registro(db: Session, cambios: dict, usuario: str | None) -> dict:
    data = get_registro(db)
    if cambios.get("regenerar"):
        from app.accounts import nuevo_codigo_empresa

        data["codigo"] = nuevo_codigo_empresa()
    if "activo" in cambios:
        data["activo"] = bool(cambios["activo"])
    _set(db, "registro", data, usuario)
    return data

"""Informes descargables: seguridad del día (CP008) y desempeño de rutas (CP025).

Cada informe se puede pedir en JSON (para la pantalla), en PDF o en Excel.
"""
from __future__ import annotations

import io
from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import app_settings as cfg
from app.auth import require_staff
from app.database import get_db
from app.detection import EVENT_LABELS, LIMA, SEV_LABEL, aware
from app.geo import haversine_m
from app.models import AiAlert, FuelLog, Incident, NavRoute, User, Vehicle, VehicleEvent, VehiclePosition

router = APIRouter(prefix="/reportes", tags=["reportes"])

TIPOS_SEGURIDAD = list(EVENT_LABELS)
ESTADO_ALERTA = {"activa": "Sin atender", "en_atencion": "En atención", "resuelta": "Resuelta", "descartada": "Descartada"}
ESTADO_INC = {"reportado": "Reportada", "asignado": "Asignada", "en-atencion": "En atención", "resuelto": "Resuelta"}
RENDIMIENTO_REF_KM_GAL = 10.0  # camión de carga sin dato propio
PRECIO_REF_GALON = 16.5  # S/ por galón de diésel si no hay vales registrados


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _hora(dt: datetime | None) -> str:
    return aware(dt).astimezone(LIMA).strftime("%H:%M") if dt else ""


def _fecha_hora(dt: datetime | None) -> str:
    return aware(dt).astimezone(LIMA).strftime("%d/%m/%Y %H:%M") if dt else ""


def _dia(fecha: str | None) -> date:
    if not fecha:
        return datetime.now(LIMA).date()
    try:
        return date.fromisoformat(fecha)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Fecha inválida (usa AAAA-MM-DD)") from exc


def _ventana(d0: date, d1: date) -> tuple[datetime, datetime]:
    return datetime.combine(d0, time.min, LIMA), datetime.combine(d1 + timedelta(days=1), time.min, LIMA)


def _placas(db: Session) -> dict[str, str]:
    return {v.id: v.placa for v in db.scalars(select(Vehicle)).all()}


# ─────────────── CP008: informe de seguridad del día ───────────────

def informe_seguridad(db: Session, d: date) -> dict:
    ini, fin = _ventana(d, d)
    placas = _placas(db)
    eventos = db.scalars(
        select(VehicleEvent)
        .where(VehicleEvent.created_at >= ini, VehicleEvent.created_at < fin, VehicleEvent.tipo.in_(TIPOS_SEGURIDAD))
        .order_by(VehicleEvent.created_at)
    ).all()
    alertas = db.scalars(
        select(AiAlert).where(AiAlert.created_at >= ini, AiAlert.created_at < fin).order_by(AiAlert.created_at)
    ).all()
    incidencias = db.scalars(
        select(Incident).where(Incident.created_at >= ini, Incident.created_at < fin).order_by(Incident.created_at)
    ).all()

    por_tipo: dict[str, int] = {}
    for e in eventos:
        por_tipo[e.tipo] = por_tipo.get(e.tipo, 0) + 1
    sev = {"high": 0, "medium": 0, "low": 0}
    for a in alertas:
        sev[a.severidad if a.severidad in sev else "medium"] += 1
    unidades = {e.vehicle_id for e in eventos} | {a.vehicle_id for a in alertas if a.vehicle_id} | {i.vehicle_id for i in incidencias}
    atendidas = sum(1 for a in alertas if a.estado in ("en_atencion", "resuelta", "descartada"))

    return {
        "fecha": d.isoformat(),
        "generado": datetime.now(LIMA).strftime("%d/%m/%Y %H:%M"),
        "empresa": cfg.get_flota(db).get("empresa") or "",
        "resumen": {
            "eventos": len(eventos),
            "alertas": len(alertas),
            "alertasAltas": sev["high"],
            "alertasMedias": sev["medium"],
            "alertasBajas": sev["low"],
            "alertasAtendidas": atendidas,
            "incidencias": len(incidencias),
            "panico": sum(1 for i in incidencias if i.es_panico),
            "unidades": len(unidades),
            "simulados": sum(1 for e in eventos if e.simulado),
        },
        "porTipo": [
            {"tipo": t, "label": _cap(EVENT_LABELS[t]), "cantidad": por_tipo.get(t, 0)} for t in TIPOS_SEGURIDAD
        ],
        "eventos": [
            {
                "hora": _hora(e.created_at),
                "placa": placas.get(e.vehicle_id, "—"),
                "tipo": e.tipo,
                "label": _cap(EVENT_LABELS.get(e.tipo, e.tipo)),
                "severidad": SEV_LABEL.get(e.severidad, e.severidad),
                "descripcion": e.descripcion,
                "simulado": bool(e.simulado),
                "lat": e.lat,
                "lng": e.lng,
            }
            for e in eventos
        ],
        "alertas": [
            {
                "hora": _hora(a.created_at),
                "placa": placas.get(a.vehicle_id, ", ".join(a.placas_afectadas or []) or "—"),
                "titulo": a.titulo,
                "severidad": SEV_LABEL.get(a.severidad, a.severidad),
                "estado": ESTADO_ALERTA.get(a.estado, a.estado),
                "acciones": len(a.historial or []),
            }
            for a in alertas
        ],
        "incidencias": [
            {
                "hora": _hora(i.created_at),
                "id": i.id,
                "placa": placas.get(i.vehicle_id, "—"),
                "titulo": i.titulo + (" (pánico)" if i.es_panico else ""),
                "severidad": i.severidad,
                "estado": ESTADO_INC.get(i.estado, i.estado),
            }
            for i in incidencias
        ],
    }


# ─────────────── CP025: desempeño de rutas ───────────────

def _recorrido_km(db: Session, r: NavRoute) -> float | None:
    if not r.vehicle_id or not r.started_at or not r.completed_at:
        return None
    pts = db.execute(
        select(VehiclePosition.lat, VehiclePosition.lng)
        .where(
            VehiclePosition.vehicle_id == r.vehicle_id,
            VehiclePosition.recorded_at >= r.started_at,
            VehiclePosition.recorded_at <= r.completed_at,
        )
        .order_by(VehiclePosition.recorded_at)
    ).all()
    if len(pts) < 5:
        return None
    total = sum(haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(pts, pts[1:]))
    return round(total / 1000, 1)


def informe_rutas(db: Session, d0: date, d1: date) -> dict:
    ini, fin = _ventana(d0, d1)
    rutas = db.scalars(
        select(NavRoute)
        .where(NavRoute.status == "completada", NavRoute.completed_at >= ini, NavRoute.completed_at < fin)
        .order_by(NavRoute.completed_at)
    ).all()
    logs = db.scalars(select(FuelLog)).all()
    gal = sum(l.galones for l in logs)
    precio = round(sum(l.monto for l in logs) / gal, 2) if gal else PRECIO_REF_GALON
    filas = []
    for r in rutas:
        v = r.vehicle
        real = None
        if r.started_at and r.completed_at:
            real = round((aware(r.completed_at) - aware(r.started_at)).total_seconds() / 60)
        est = r.duration_min
        dist = r.distance_km or 0
        rec = _recorrido_km(db, r)
        rend = (v.rendimiento_km_gal if v and v.rendimiento_km_gal else None) or RENDIMIENTO_REF_KM_GAL
        km_comb = rec if rec is not None else dist
        galones = round(km_comb / rend, 1) if km_comb else 0
        extra_km = round(rec - dist, 1) if rec is not None and dist else None
        filas.append(
            {
                "fecha": _fecha_hora(r.completed_at),
                "placa": v.placa if v else "—",
                "conductor": r.driver.full_name if r.driver else "—",
                "origen": r.origin_name.split(",")[0],
                "destino": r.dest_name.split(",")[0],
                "distanciaKm": round(dist, 1),
                "recorridoKm": rec,
                "kmExtra": extra_km,
                "estimadoMin": est,
                "realMin": real,
                "difMin": (real - est) if real is not None and est else None,
                "puntual": bool(real is not None and est and real <= est * 1.10),
                "velMediaKmh": round(dist / (real / 60), 1) if real and dist else None,
                "galones": galones,
                "costo": round(galones * precio, 2),
                "galonesExtra": round(extra_km / rend, 1) if extra_km is not None else None,
            }
        )
    con_tiempo = [f for f in filas if f["realMin"] is not None and f["estimadoMin"]]
    puntuales = sum(1 for f in con_tiempo if f["puntual"])
    ahorro_min = sum(-f["difMin"] for f in con_tiempo if f["difMin"] is not None and f["difMin"] < 0)
    extra_gal = sum(f["galonesExtra"] for f in filas if f["galonesExtra"] and f["galonesExtra"] > 0)
    return {
        "desde": d0.isoformat(),
        "hasta": d1.isoformat(),
        "generado": datetime.now(LIMA).strftime("%d/%m/%Y %H:%M"),
        "empresa": cfg.get_flota(db).get("empresa") or "",
        "precioGalon": precio,
        "precioReferencial": not gal,
        "resumen": {
            "rutas": len(filas),
            "km": round(sum(f["distanciaKm"] for f in filas), 1),
            "horas": round(sum(f["realMin"] or 0 for f in filas) / 60, 1),
            "puntualidad": round(puntuales / len(con_tiempo) * 100) if con_tiempo else None,
            "minAhorrados": ahorro_min,
            "galones": round(sum(f["galones"] for f in filas), 1),
            "costo": round(sum(f["costo"] for f in filas), 2),
            "galonesPorDesvio": round(extra_gal, 1),
        },
        "rutas": filas,
    }


# ─────────────── exportación ───────────────

def _xlsx(titulo: str, info: list[tuple[str, str]], hojas: list[tuple[str, list[str], list[list]]]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    wb.remove(wb.active)
    for nombre, cols, rows in hojas:
        ws = wb.create_sheet(nombre[:31])
        ws.append([titulo])
        ws["A1"].font = Font(bold=True, size=13)
        for k, v in info:
            ws.append([k, v])
        ws.append([])
        ws.append(cols)
        hdr = ws.max_row
        for c in ws[hdr]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="0B1220")
            c.alignment = Alignment(vertical="center", wrap_text=True)
        for r in rows:
            ws.append(r)
        for i, col in enumerate(cols, start=1):
            largo = max([len(str(col))] + [len(str(r[i - 1])) for r in rows if i - 1 < len(r) and r[i - 1] is not None])
            ws.column_dimensions[ws.cell(row=hdr, column=i).column_letter].width = min(60, max(10, largo + 2))
        ws.freeze_panes = ws.cell(row=hdr + 1, column=1)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _pdf(titulo: str, subtitulo: str, kpis: list[tuple[str, str]], secciones: list[tuple[str, list[str], list[list], list[float]]], nota: str) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=1.5 * cm, rightMargin=1.5 * cm, topMargin=1.4 * cm, bottomMargin=1.4 * cm,
                            title=titulo, author="TruckGPS")
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], alignment=0, fontSize=17, spaceAfter=2)
    sub = ParagraphStyle("sub", parent=ss["Normal"], fontSize=9.5, textColor=colors.HexColor("#475569"))
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=12, spaceBefore=10, spaceAfter=6)
    cell = ParagraphStyle("cell", parent=ss["Normal"], fontSize=8, leading=10)
    small = ParagraphStyle("small", parent=ss["Normal"], fontSize=8, textColor=colors.HexColor("#64748b"))
    story = [Paragraph(titulo, h1), Paragraph(subtitulo, sub), Spacer(1, 10)]
    if kpis:
        k = Table([[Paragraph(f"<b>{v}</b><br/><font size=7.5 color='#64748b'>{l}</font>", cell) for l, v in kpis]],
                  colWidths=[(26.7 * cm) / len(kpis)] * len(kpis))
        k.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#e2e8f0")),
                               ("INNERGRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#e2e8f0")),
                               ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
                               ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
        story += [k]
    for nombre, cols, rows, widths in secciones:
        story.append(Paragraph(nombre, h2))
        if not rows:
            story.append(Paragraph("Sin registros en el periodo.", small))
            continue
        data = [[Paragraph(f"<b>{c}</b>", cell) for c in cols]] + [[Paragraph("" if x is None else str(x), cell) for x in r] for r in rows]
        t = Table(data, colWidths=[w * cm for w in widths], repeatRows=1)
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
                               ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#e2e8f0")),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        story.append(t)
    story += [Spacer(1, 10), Paragraph(nota, small)]
    doc.build(story)
    return buf.getvalue()


def _descarga(data: bytes, nombre: str, formato: str) -> StreamingResponse:
    media = "application/pdf" if formato == "pdf" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return StreamingResponse(io.BytesIO(data), media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{nombre}.{formato}"',
        "Access-Control-Expose-Headers": "Content-Disposition",
    })


@router.get("/seguridad")
def seguridad(
    fecha: str | None = None,
    formato: str = Query(default="json", pattern="^(json|pdf|xlsx)$"),
    _user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    d = _dia(fecha)
    rep = informe_seguridad(db, d)
    if formato == "json":
        return rep
    r = rep["resumen"]
    titulo = f"Informe de seguridad del {d.strftime('%d/%m/%Y')}"
    sub = f"{rep['empresa'] + ' · ' if rep['empresa'] else ''}Generado el {rep['generado']} por TruckGPS"
    ev = [[e["hora"], e["placa"], e["label"], e["severidad"], e["descripcion"] + (" [prueba]" if e["simulado"] else "")] for e in rep["eventos"]]
    al = [[a["hora"], a["placa"], a["titulo"], a["severidad"], a["estado"], a["acciones"]] for a in rep["alertas"]]
    inc = [[i["hora"], i["id"], i["placa"], i["titulo"], i["severidad"], i["estado"]] for i in rep["incidencias"]]
    tipos = [[t["label"], t["cantidad"]] for t in rep["porTipo"]]
    nombre = f"informe-seguridad-{d.isoformat()}"
    if formato == "xlsx":
        info = [("Fecha", d.strftime("%d/%m/%Y")), ("Generado", rep["generado"]), ("Eventos", r["eventos"]), ("Alertas", r["alertas"]),
                ("Incidencias", r["incidencias"]), ("Unidades con eventos", r["unidades"])]
        return _descarga(_xlsx(titulo, info, [
            ("Eventos", ["Hora", "Placa", "Tipo", "Severidad", "Descripción"], ev),
            ("Alertas", ["Hora", "Placa", "Alerta", "Severidad", "Estado", "Acciones registradas"], al),
            ("Incidencias", ["Hora", "Código", "Placa", "Incidencia", "Severidad", "Estado"], inc),
            ("Resumen por tipo", ["Tipo de evento", "Cantidad"], tipos),
        ]), nombre, "xlsx")
    kpis = [("Eventos de seguridad", str(r["eventos"])), ("Alertas (altas)", f"{r['alertas']} ({r['alertasAltas']})"),
            ("Alertas atendidas", f"{r['alertasAtendidas']} de {r['alertas']}"), ("Incidencias", str(r["incidencias"])),
            ("Botón de pánico", str(r["panico"])), ("Unidades involucradas", str(r["unidades"]))]
    return _descarga(_pdf(titulo, sub, kpis, [
        ("Eventos por tipo", ["Tipo de evento", "Cantidad"], tipos, [8, 3]),
        ("Eventos del día (desvíos, manipulaciones y otros)", ["Hora", "Placa", "Tipo", "Severidad", "Descripción"], ev, [1.6, 2.2, 4.2, 2, 16.6]),
        ("Alertas generadas", ["Hora", "Placa", "Alerta", "Severidad", "Estado", "Acciones"], al, [1.6, 2.2, 13, 2, 3.4, 2.4]),
        ("Incidencias reportadas", ["Hora", "Código", "Placa", "Incidencia", "Severidad", "Estado"], inc, [1.6, 3.2, 2.2, 12.4, 2.4, 3.4]),
    ], "Los eventos marcados [prueba] vienen de la unidad simulada SIM-001. Horas en hora de Lima."), nombre, "pdf")


@router.get("/rutas")
def rutas(
    desde: str | None = None,
    hasta: str | None = None,
    formato: str = Query(default="json", pattern="^(json|pdf|xlsx)$"),
    _user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    d1 = _dia(hasta)
    d0 = _dia(desde) if desde else d1 - timedelta(days=29)
    if d0 > d1:
        raise HTTPException(status_code=400, detail="La fecha inicial es posterior a la final")
    if (d1 - d0).days > 366:
        raise HTTPException(status_code=400, detail="El periodo máximo es de un año")
    rep = informe_rutas(db, d0, d1)
    if formato == "json":
        return rep
    r = rep["resumen"]
    titulo = f"Desempeño de rutas: {d0.strftime('%d/%m/%Y')} al {d1.strftime('%d/%m/%Y')}"
    sub = f"{rep['empresa'] + ' · ' if rep['empresa'] else ''}Generado el {rep['generado']} por TruckGPS"
    filas = [[f["fecha"], f["placa"], f["conductor"], f"{f['origen']} → {f['destino']}", f["distanciaKm"],
              f["recorridoKm"] if f["recorridoKm"] is not None else "—", f["estimadoMin"], f["realMin"],
              f["difMin"] if f["difMin"] is not None else "—", "Sí" if f["puntual"] else "No", f["galones"], f["costo"]] for f in rep["rutas"]]
    cols = ["Llegada", "Placa", "Conductor", "Ruta", "Km plan", "Km recorridos", "Min estimados", "Min reales", "Diferencia (min)", "A tiempo", "Galones", "Costo (S/)"]
    nombre = f"desempeno-rutas-{d0.isoformat()}-{d1.isoformat()}"
    nota = (f"Combustible estimado con el rendimiento de cada unidad (o {RENDIMIENTO_REF_KM_GAL:g} km/gal si no tiene) y un precio de "
            f"S/ {rep['precioGalon']:.2f} por galón{' (referencial: aún no hay vales de combustible)' if rep['precioReferencial'] else ' (promedio de los vales registrados)'}. "
            "«A tiempo»: llegó en el tiempo estimado o hasta 10 % más. Km recorridos se calcula con las posiciones GPS del viaje.")
    if formato == "xlsx":
        info = [("Periodo", f"{d0.strftime('%d/%m/%Y')} al {d1.strftime('%d/%m/%Y')}"), ("Rutas completadas", r["rutas"]), ("Km", r["km"]),
                ("Puntualidad (%)", r["puntualidad"] if r["puntualidad"] is not None else "—"), ("Minutos ahorrados vs. estimado", r["minAhorrados"]),
                ("Galones estimados", r["galones"]), ("Costo estimado (S/)", r["costo"]), ("Nota", nota)]
        return _descarga(_xlsx(titulo, info, [("Rutas", cols, filas)]), nombre, "xlsx")
    kpis = [("Rutas completadas", str(r["rutas"])), ("Kilómetros", f"{r['km']:,.1f}"), ("Horas de viaje", f"{r['horas']:,.1f}"),
            ("Llegadas a tiempo", f"{r['puntualidad']} %" if r["puntualidad"] is not None else "—"),
            ("Minutos ahorrados", str(r["minAhorrados"])), ("Combustible", f"{r['galones']:,.1f} gal · S/ {r['costo']:,.2f}")]
    return _descarga(_pdf(titulo, sub, kpis, [("Detalle por ruta", cols, filas, [2.6, 1.7, 3, 5.4, 1.5, 1.8, 1.8, 1.6, 1.8, 1.4, 1.6, 1.9])], nota), nombre, "pdf")

"""Tráfico en vivo y búsqueda de lugares con TomTom (la clave queda en el servidor)."""
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app import tomtom
from app.auth import get_current_user
from app.models import User

router = APIRouter(prefix="/mapas", tags=["mapas"])


@router.get("/estado")
def estado():
    """Si TomTom está configurado y responde. No muestra la clave."""
    return tomtom.status()


@router.get("/capa-trafico")
def capa_trafico(user: User = Depends(get_current_user)):
    """Dirección de los cuadros de tráfico para el mapa (firmada, vence en unas horas)."""
    if not tomtom.configured():
        return {"disponible": False}
    sig, expira = tomtom.tile_signature()
    return {
        "disponible": True,
        "url": f"/mapas/trafico/{{z}}/{{x}}/{{y}}.png?s={sig}",
        "base": f"/mapas/base/{{estilo}}/{{z}}/{{x}}/{{y}}.png?s={sig}",
        "flujo": f"/mapas/flujo/{{z}}/{{x}}/{{y}}.pbf?s={sig}",
        "expira": expira,
    }


@router.get("/trafico/{z}/{x}/{y}.png")
def cuadro_trafico(z: int, x: int, y: int, s: str = Query(default="", max_length=40)):
    # Las imágenes del mapa no pueden mandar la sesión: se validan con la firma de /capa-trafico.
    if not tomtom.signature_ok(s):
        raise HTTPException(status_code=403, detail="Enlace de tráfico vencido")
    if not tomtom.tile_allowed(z, x, y):
        return Response(status_code=204)
    try:
        png = tomtom.flow_tile(z, x, y)
    except tomtom.TomTomError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from None
    # El CDN de Vercel guarda el cuadro 2 minutos: muchos usuarios mirando la misma zona gastan una sola consulta.
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "public, max-age=60, s-maxage=120"})


@router.get("/buscar")
def buscar(
    q: str = Query(min_length=1, max_length=120),
    lat: float | None = Query(default=None, ge=-90, le=90),
    lng: float | None = Query(default=None, ge=-180, le=180),
    user: User = Depends(get_current_user),
):
    if not tomtom.configured():
        raise HTTPException(status_code=503, detail="La búsqueda de TomTom no está configurada")
    try:
        return {"resultados": tomtom.search(q, lat, lng), "restantesHoy": tomtom.search_left_today()}
    except tomtom.TomTomError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from None


@router.get("/base/{estilo}/{z}/{x}/{y}.png")
def cuadro_base(estilo: str, z: int, x: int, y: int, s: str = Query(default="", max_length=40)):
    """Mapa base para la navegación (de día o de noche). Se guarda un día en el CDN: el mapa casi no cambia."""
    if not tomtom.signature_ok(s):
        raise HTTPException(status_code=403, detail="Enlace del mapa vencido")
    if estilo not in tomtom.BASE_STYLES or not tomtom.tile_allowed(z, x, y):
        return Response(status_code=204)
    try:
        png = tomtom.base_tile(estilo, z, x, y)
    except tomtom.TomTomError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from None
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "public, max-age=86400, s-maxage=86400"})


@router.get("/flujo/{z}/{x}/{y}.pbf")
def cuadro_flujo(z: int, x: int, y: int, s: str = Query(default="", max_length=40)):
    """Tráfico en vectores para el mapa de navegación (copia de 2 min en el CDN)."""
    if not tomtom.signature_ok(s):
        raise HTTPException(status_code=403, detail="Enlace de tráfico vencido")
    if not tomtom.tile_allowed(z, x, y):
        return Response(status_code=204)
    try:
        data = tomtom.flow_vector_tile(z, x, y)
    except tomtom.TomTomError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from None
    return Response(content=data, media_type="application/x-protobuf", headers={"Cache-Control": "public, max-age=60, s-maxage=120"})

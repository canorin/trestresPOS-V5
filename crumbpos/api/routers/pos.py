"""Endpoints del cliente POS — sincronización de CAFs y folios.

El POS de sucursal (cliente Windows offline-aware) usa estos endpoints
para mantener actualizada su cache local de tramos de folio asignados.

Protocolo de sync (polling):
  1. Al arrancar o reconectarse, el POS hace ``GET /api/pos/caf-sync``
     con ``desde_id=0`` para obtener el estado completo.
  2. Periódicamente (ej. cada 5 minutos), repite la llamada con
     ``desde_id=<ultimo_id_recibido>`` para traer solo los deltas.
  3. Aplica los eventos localmente y actualiza su cache de ``CafAsignacion``.

Tipos de evento (``tipo_evento``) que el POS debe procesar:

  ``asignacion_creada``
      Se asignó un tramo nuevo a esta sucursal.
      Payload: ``{rango_desde, rango_hasta, folio_actual, caf_id}``.
      Acción POS: insertar el tramo en la cache local.

  ``asignacion_modificada``
      Cambió el rango o el ``folio_actual`` de un tramo existente.
      Payload: ``{asignacion_id, rango_desde, rango_hasta, folio_actual}``.
      Acción POS: actualizar el tramo en cache.

  ``asignacion_eliminada``
      El tramo fue devuelto al pool (sucursal desactivada o reasignación).
      Payload: ``{asignacion_id}``.
      Acción POS: eliminar el tramo de cache y dejar de usar esos folios.

  ``folio_consumido_servidor``
      El servidor consumió un folio del slice de esta sucursal (flujo
      excepcional cuando pool estaba vacío y el operador confirmó continuar).
      Payload: ``{tipo_dte, folio}``.
      Acción POS: marcar ese folio como consumido en cache local para
      evitar colisión si el POS llegara a querer usarlo.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from crumbpos.db.models import CafAsignacion, CafEventoSync, CafFolio, Sucursal
from crumbpos.api.dependencies import get_tenant, TenantContext

router = APIRouter(prefix="/api/pos", tags=["pos"])


# ── Schemas ──

class EventoSyncOut(BaseModel):
    id: int
    tipo_evento: str
    caf_id: str | None = None
    asignacion_id: str | None = None
    payload: dict | None = None
    timestamp: str


class TramoActivoOut(BaseModel):
    asignacion_id: str
    caf_id: str
    tipo_dte: int
    rango_desde: int
    rango_hasta: int
    folio_actual: int
    estado: str


class CafSyncOut(BaseModel):
    ok: bool
    sucursal_id: str
    eventos: list[EventoSyncOut]
    ultimo_id: int
    tramos_actuales: list[TramoActivoOut]


# ── Endpoints ──

@router.get("/caf-sync", response_model=CafSyncOut)
def caf_sync(
    desde_id: int = Query(default=0, ge=0, description="Cursor de paginación: traer eventos con id > desde_id"),
    tenant: TenantContext = Depends(get_tenant),
):
    """Feed de eventos de CAF para el POS de la sucursal autenticada.

    El POS envía el ``desde_id`` del último evento que procesó.
    Si es 0 (arranque / reconexión), se devuelven todos los eventos
    disponibles y el estado completo de los tramos asignados.

    El campo ``tramos_actuales`` siempre refleja el estado en tiempo real
    de los tramos asignados a la sucursal — el POS puede compararlo con
    su cache y reconcilar diferencias sin tener que reprocesar todos los
    eventos.
    """
    try:
        sucursal_id = tenant.sucursal_id
        if not sucursal_id:
            raise HTTPException(
                400,
                "Este endpoint requiere un JWT con sucursal_id. "
                "El token de sesión de POS debe incluir la sucursal asignada.",
            )

        db = tenant.db

        # Verificar que la sucursal pertenece a la empresa del tenant
        suc = db.query(Sucursal).filter(
            Sucursal.id == sucursal_id,
            Sucursal.empresa_id == tenant.empresa_id,
        ).first()
        if not suc:
            raise HTTPException(404, "Sucursal no encontrada para este tenant")
        if not suc.activa:
            raise HTTPException(
                409,
                "La sucursal está desactivada. No se pueden sincronizar folios.",
            )

        # ── Eventos desde el cursor ──
        rows = (
            db.query(CafEventoSync)
            .filter(
                CafEventoSync.sucursal_id == sucursal_id,
                CafEventoSync.id > desde_id,
            )
            .order_by(CafEventoSync.id.asc())
            .limit(500)  # máx 500 eventos por llamada
            .all()
        )

        eventos = [
            EventoSyncOut(
                id=e.id,
                tipo_evento=e.tipo_evento,
                caf_id=e.caf_id,
                asignacion_id=e.asignacion_id,
                payload=e.payload,
                timestamp=e.timestamp.isoformat() if e.timestamp else "",
            )
            for e in rows
        ]
        ultimo_id = rows[-1].id if rows else desde_id

        # ── Tramos actuales asignados a la sucursal ──
        tramos_rows = (
            db.query(CafAsignacion, CafFolio)
            .join(CafFolio, CafAsignacion.caf_id == CafFolio.id)
            .filter(
                CafFolio.empresa_id == tenant.empresa_id,
                CafAsignacion.sucursal_id == sucursal_id,
                CafAsignacion.estado == "activo",
            )
            .order_by(CafFolio.tipo_dte, CafAsignacion.rango_desde)
            .all()
        )

        tramos = [
            TramoActivoOut(
                asignacion_id=a.id,
                caf_id=a.caf_id,
                tipo_dte=c.tipo_dte,
                rango_desde=a.rango_desde,
                rango_hasta=a.rango_hasta,
                folio_actual=a.folio_actual,
                estado=a.estado,
            )
            for a, c in tramos_rows
        ]

        return CafSyncOut(
            ok=True,
            sucursal_id=sucursal_id,
            eventos=eventos,
            ultimo_id=ultimo_id,
            tramos_actuales=tramos,
        )
    finally:
        tenant.close()

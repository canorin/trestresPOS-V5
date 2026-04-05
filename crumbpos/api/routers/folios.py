"""Endpoints de gestión de folios CAF — multi-tenant.

Los CAFs son por empresa (RUT emisor) y ambiente (certificacion/produccion).
"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel

from crumbpos.db.models import CafFolio, Empresa
from crumbpos.core.caf.caf_manager_db import CAFManagerDB
from crumbpos.api.dependencies import get_tenant, TenantContext

router = APIRouter(prefix="/api/folios", tags=["folios"])


# ── Schemas ──

class SetFolioIn(BaseModel):
    tipo_dte: int
    folio: int


class SetFolioOut(BaseModel):
    ok: bool
    tipo_dte: int
    folio_anterior: int | None = None
    folio_nuevo: int
    mensaje: str | None = None


class EstadoFolioOut(BaseModel):
    tipo_dte: int
    nombre: str
    folio_actual: int
    folio_min: int | None
    folio_max: int | None
    disponibles: int
    consumidos: int
    total_autorizados: int
    pct_usado: float
    alerta: str
    rangos: list[dict]


class ResumenOut(BaseModel):
    total_tipos: int
    alertas_criticas: int
    alertas_bajas: int
    empresa_rut: str | None = None
    empresa_razon: str | None = None
    ambiente: str | None = None
    folios: list[EstadoFolioOut]


# ── Endpoints ──

@router.get("/estado", response_model=ResumenOut)
def estado_folios(tenant: TenantContext = Depends(get_tenant)):
    """Estado completo de todos los folios con alertas."""
    try:
        db = tenant.db
        empresa = db.query(Empresa).filter(Empresa.rut == tenant.empresa_rut).first()
        if not empresa:
            raise HTTPException(404, "Empresa no encontrada")

        mgr = CAFManagerDB(db, empresa.id)
        folios = mgr.estado_folios()

        criticas = sum(1 for f in folios if f["alerta"] in ("agotado", "critico"))
        bajas = sum(1 for f in folios if f["alerta"] in ("bajo", "advertencia"))

        return ResumenOut(
            total_tipos=len(folios),
            alertas_criticas=criticas,
            alertas_bajas=bajas,
            empresa_rut=empresa.rut,
            empresa_razon=empresa.razon_social,
            ambiente=tenant.ambiente,
            folios=[EstadoFolioOut(**f) for f in folios],
        )
    finally:
        tenant.close()


@router.post("/set", response_model=SetFolioOut)
def set_folio(body: SetFolioIn, tenant: TenantContext = Depends(get_tenant)):
    """Establece manualmente el próximo folio para un tipo DTE."""
    try:
        db = tenant.db
        mgr = CAFManagerDB(db, tenant.empresa_id)

        estado = mgr.estado_folios()
        anterior = None
        for f in estado:
            if f["tipo_dte"] == body.tipo_dte:
                anterior = f["folio_actual"]
                break

        try:
            mgr.set_folio(body.tipo_dte, body.folio)
            db.commit()
        except ValueError as e:
            db.rollback()
            raise HTTPException(400, str(e))

        return SetFolioOut(
            ok=True,
            tipo_dte=body.tipo_dte,
            folio_anterior=anterior,
            folio_nuevo=body.folio,
            mensaje=f"Folio tipo {body.tipo_dte} actualizado: {anterior} → {body.folio}",
        )
    finally:
        tenant.close()


@router.post("/upload")
async def upload_caf(
    archivo: UploadFile = File(...),
    tenant: TenantContext = Depends(get_tenant),
):
    """Sube un nuevo archivo CAF XML."""
    try:
        if not archivo.filename or not archivo.filename.endswith(".xml"):
            raise HTTPException(400, "El archivo debe ser un XML de CAF")

        contenido = await archivo.read()
        if len(contenido) == 0:
            raise HTTPException(400, "Archivo vacío")
        if len(contenido) > 1_000_000:
            raise HTTPException(400, "Archivo demasiado grande (max 1MB)")

        db = tenant.db
        mgr = CAFManagerDB(db, tenant.empresa_id)

        try:
            info = mgr.registrar_caf(contenido)
            db.commit()
        except ValueError as e:
            db.rollback()
            raise HTTPException(400, str(e))

        return {
            "ok": True,
            "mensaje": f"CAF registrado: tipo {info['tipo_dte']}, folios {info['folio_desde']}-{info['folio_hasta']}",
            "ambiente": tenant.ambiente,
            **info,
        }
    finally:
        tenant.close()

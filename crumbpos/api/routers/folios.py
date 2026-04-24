"""Endpoints de gestión de folios CAF — multi-tenant.

Los CAFs son por empresa (RUT emisor) y ambiente (certificacion/produccion).
"""
from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, File
from pydantic import BaseModel

from crumbpos.db.models import CafFolio, Empresa, Sucursal
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


class AsignarSucursalIn(BaseModel):
    # `None` = mover al pool del server (sin dueño). Si se asigna a una
    # sucursal, el CAF pasa a ser propiedad exclusiva de ella.
    sucursal_id: str | None = None


class AsignarSucursalOut(BaseModel):
    ok: bool
    caf_id: str
    sucursal_id: str | None = None
    sucursal_nombre: str | None = None
    mensaje: str


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
    folio_inicial_override: int | None = Form(default=None),
    tenant: TenantContext = Depends(get_tenant),
):
    """Sube un nuevo archivo CAF XML.

    Form fields:
    - ``archivo``: XML del CAF (requerido).
    - ``folio_inicial_override``: si el CAF trae folios ya consumidos fuera
      del sistema (por ejemplo cuando se migra desde otro software o
      cuando se quemaron folios en intentos previos de certificación),
      permite arrancar a consumir desde un folio mayor al ``D`` del CAF.
      Debe estar dentro del rango ``[D, H]`` del archivo. Si se omite,
      arranca desde ``D`` (comportamiento estándar).
    """
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
            info = mgr.registrar_caf(
                contenido,
                folio_inicial_override=folio_inicial_override,
            )
            db.commit()
        except ValueError as e:
            db.rollback()
            raise HTTPException(400, str(e))

        folio_inicial = info.get("folio_inicial", info["folio_desde"])
        if folio_inicial != info["folio_desde"]:
            mensaje = (
                f"CAF registrado: tipo {info['tipo_dte']}, "
                f"rango {info['folio_desde']}-{info['folio_hasta']} "
                f"(arranca desde folio {folio_inicial})"
            )
        else:
            mensaje = (
                f"CAF registrado: tipo {info['tipo_dte']}, "
                f"folios {info['folio_desde']}-{info['folio_hasta']}"
            )

        return {
            "ok": True,
            "mensaje": mensaje,
            "ambiente": tenant.ambiente,
            **info,
        }
    finally:
        tenant.close()


@router.put("/caf/{caf_id}/sucursal", response_model=AsignarSucursalOut)
def asignar_sucursal(
    caf_id: str,
    body: AsignarSucursalIn,
    tenant: TenantContext = Depends(get_tenant),
):
    """Asigna un CAF a una sucursal o lo devuelve al pool del server.

    Regla: cada CAF tiene un único dueño a la vez. ``sucursal_id=None``
    lo libera al pool del server (lo puede consumir cualquier sucursal
    o una emisión sin sucursal). Cuando se asigna a una sucursal, el
    POS de esa sucursal debe respetar esa propiedad al pedir folio.

    Validaciones:
    - El CAF pertenece a la empresa del tenant (evita cross-tenant).
    - La sucursal (si se especifica) pertenece a la misma empresa y
      está activa.
    """
    try:
        db = tenant.db

        caf = db.query(CafFolio).filter(
            CafFolio.id == caf_id,
            CafFolio.empresa_id == tenant.empresa_id,
        ).first()
        if not caf:
            raise HTTPException(404, "CAF no encontrado")

        sucursal_nombre: str | None = None
        if body.sucursal_id is None:
            caf.sucursal_id = None
            mensaje = (
                f"CAF tipo {caf.tipo_dte} "
                f"({caf.rango_desde}-{caf.rango_hasta}) "
                f"liberado al pool del server."
            )
        else:
            sucursal = db.query(Sucursal).filter(
                Sucursal.id == body.sucursal_id,
                Sucursal.empresa_id == tenant.empresa_id,
            ).first()
            if not sucursal:
                raise HTTPException(
                    404,
                    "Sucursal no encontrada para esta empresa",
                )
            if not sucursal.activa:
                raise HTTPException(
                    400,
                    "Sucursal inactiva: reactivarla antes de asignarle CAFs",
                )
            caf.sucursal_id = sucursal.id
            sucursal_nombre = sucursal.nombre
            mensaje = (
                f"CAF tipo {caf.tipo_dte} "
                f"({caf.rango_desde}-{caf.rango_hasta}) "
                f"asignado a {sucursal.nombre}."
            )

        db.commit()

        return AsignarSucursalOut(
            ok=True,
            caf_id=caf.id,
            sucursal_id=caf.sucursal_id,
            sucursal_nombre=sucursal_nombre,
            mensaje=mensaje,
        )
    finally:
        tenant.close()

"""Endpoints de gestión de folios CAF — multi-tenant.

Los CAFs son por empresa (RUT emisor) y ambiente (certificacion/produccion).

Modelo de subdivisión por sucursal
==================================

Un CAF nunca se subdivide a nivel SII (al SII se le envía el rango original
autorizado). Internamente sí: cada CAF se descompone en uno o más **tramos**
(``CafAsignacion``) que el master cliente asigna manualmente a sucursales o
al **pool del server** (``sucursal_id IS NULL``).

Endpoints relevantes:

* ``GET  /api/folios/estado`` — estado completo, con tramos por CAF.
* ``POST /api/folios/upload`` — subir CAF nuevo (entra al pool).
* ``GET  /api/folios/caf/{id}/asignaciones`` — listar tramos de un CAF.
* ``PUT  /api/folios/caf/{id}/asignaciones`` — reescribir tramos de un CAF.
* ``PUT  /api/folios/caf/{id}/sucursal`` — *(deprecated)* legado de
  asignación 1-CAF → 1-sucursal. Mantenido como wrapper que invoca al
  endpoint nuevo con un único tramo cubriendo el rango completo.

``FoliosAgotadosError`` (levantada por ``siguiente_folio`` cuando el slice
solicitado se quedó sin folios) se traduce a 409 estructurado en los routers
de emisión, no aquí — este router solo gestiona inventario.
"""
from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, File
from pydantic import BaseModel, Field

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


class TramoIn(BaseModel):
    """Tramo de un CAF — input del modal Configurar destinos.

    El backend acepta huecos (rangos no cubiertos por la lista) y los
    rellena automáticamente con tramos pool. El frontend puede mandar
    solo los tramos "interesantes" (los que asigna a una sucursal) y
    confiar en que el resto cae al pool.
    """

    sucursal_id: str | None = None
    rango_desde: int = Field(..., gt=0)
    rango_hasta: int = Field(..., gt=0)


class ConfigurarAsignacionesIn(BaseModel):
    tramos: list[TramoIn] = Field(default_factory=list)


class TramoOut(BaseModel):
    id: str
    sucursal_id: str | None
    sucursal_nombre: str | None = None
    rango_desde: int
    rango_hasta: int
    folio_actual: int
    estado: str


class AsignacionesOut(BaseModel):
    ok: bool
    caf_id: str
    tipo_dte: int
    rango_desde: int
    rango_hasta: int
    tramos: list[TramoOut]
    mensaje: str | None = None


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


def _cargar_caf(db, caf_id: str, empresa_id: str) -> CafFolio:
    """Recupera un CAF asegurando que pertenece al tenant. 404 si no."""
    caf = db.query(CafFolio).filter(
        CafFolio.id == caf_id,
        CafFolio.empresa_id == empresa_id,
    ).first()
    if not caf:
        raise HTTPException(404, "CAF no encontrado")
    return caf


def _decorar_tramos(
    db, empresa_id: str, tramos: list[dict],
) -> list[TramoOut]:
    """Adjunta ``sucursal_nombre`` a cada tramo y devuelve TramoOut."""
    sucursales_map = {
        s.id: s.nombre
        for s in db.query(Sucursal).filter(
            Sucursal.empresa_id == empresa_id,
        ).all()
    }
    return [
        TramoOut(
            id=t["id"],
            sucursal_id=t["sucursal_id"],
            sucursal_nombre=(
                sucursales_map.get(t["sucursal_id"]) if t["sucursal_id"] else None
            ),
            rango_desde=t["rango_desde"],
            rango_hasta=t["rango_hasta"],
            folio_actual=t["folio_actual"],
            estado=t["estado"],
        )
        for t in tramos
    ]


@router.get("/caf/{caf_id}/asignaciones", response_model=AsignacionesOut)
def listar_asignaciones(
    caf_id: str,
    tenant: TenantContext = Depends(get_tenant),
):
    """Lista los tramos actuales de un CAF — feed del modal.

    El frontend usa este endpoint para hidratar el modal "Configurar
    destinos" con el estado actual antes de que el master edite. La
    respuesta incluye ``rango_desde``/``rango_hasta`` del CAF padre
    para que la UI valide rangos en cliente sin un round-trip extra.
    """
    try:
        db = tenant.db
        caf = _cargar_caf(db, caf_id, tenant.empresa_id)

        mgr = CAFManagerDB(db, tenant.empresa_id)
        try:
            tramos = mgr.listar_asignaciones(caf_id)
        except ValueError as e:
            raise HTTPException(404, str(e))

        return AsignacionesOut(
            ok=True,
            caf_id=caf.id,
            tipo_dte=caf.tipo_dte,
            rango_desde=caf.rango_desde,
            rango_hasta=caf.rango_hasta,
            tramos=_decorar_tramos(db, tenant.empresa_id, tramos),
        )
    finally:
        tenant.close()


@router.put("/caf/{caf_id}/asignaciones", response_model=AsignacionesOut)
def configurar_asignaciones(
    caf_id: str,
    body: ConfigurarAsignacionesIn,
    tenant: TenantContext = Depends(get_tenant),
):
    """Reescribe los tramos de un CAF — endpoint del modal Configurar destinos.

    Idempotente: cada llamada reemplaza completamente el conjunto de
    tramos. El backend valida solapes, rangos fuera del CAF y mover
    folios ya consumidos entre sucursales distintas. Los huecos no
    cubiertos se rellenan automáticamente con tramos pool.

    Mantiene en sincronía la columna legacy ``caf_folio.sucursal_id``:
    queda con el id de la sucursal cuando hay un único tramo asignado
    a ella, o ``NULL`` cuando hay múltiples tramos (CAF subdividido).
    """
    try:
        db = tenant.db
        caf = _cargar_caf(db, caf_id, tenant.empresa_id)

        mgr = CAFManagerDB(db, tenant.empresa_id)
        tramos_in = [t.model_dump() for t in body.tramos]

        try:
            tramos_out = mgr.configurar_asignaciones(caf_id, tramos_in)
        except ValueError as e:
            db.rollback()
            raise HTTPException(400, str(e))

        # Sync columna legacy. La fila ``caf_folio.sucursal_id`` queda
        # informativa: refleja el dueño cuando es un único tramo, NULL
        # cuando está subdividido.
        if len(tramos_out) == 1:
            caf.sucursal_id = tramos_out[0]["sucursal_id"]
        else:
            caf.sucursal_id = None

        db.commit()

        n_total = len(tramos_out)
        n_pool = sum(1 for t in tramos_out if t["sucursal_id"] is None)
        if n_total == 1 and n_pool == 1:
            mensaje = (
                f"CAF tipo {caf.tipo_dte} "
                f"({caf.rango_desde}-{caf.rango_hasta}) queda íntegro "
                f"en el pool del server."
            )
        elif n_total == 1:
            mensaje = (
                f"CAF tipo {caf.tipo_dte} "
                f"({caf.rango_desde}-{caf.rango_hasta}) asignado por "
                f"completo a una sucursal."
            )
        else:
            mensaje = (
                f"CAF tipo {caf.tipo_dte} subdividido en {n_total} tramo(s); "
                f"{n_pool} en el pool del server."
            )

        return AsignacionesOut(
            ok=True,
            caf_id=caf.id,
            tipo_dte=caf.tipo_dte,
            rango_desde=caf.rango_desde,
            rango_hasta=caf.rango_hasta,
            tramos=_decorar_tramos(db, tenant.empresa_id, tramos_out),
            mensaje=mensaje,
        )
    finally:
        tenant.close()


@router.put("/caf/{caf_id}/sucursal", response_model=AsignarSucursalOut, deprecated=True)
def asignar_sucursal_legacy(
    caf_id: str,
    body: AsignarSucursalIn,
    tenant: TenantContext = Depends(get_tenant),
):
    """**[DEPRECATED]** Asigna un CAF completo a una sucursal o al pool.

    Mantenido como wrapper para clientes legacy de la API que asignan
    el CAF entero a un solo dueño. Internamente delega en
    ``configurar_asignaciones`` con un tramo único cubriendo todo el
    rango. La UI nueva debe usar ``PUT /caf/{id}/asignaciones`` para
    aprovechar la subdivisión por tramos.
    """
    try:
        db = tenant.db
        caf = _cargar_caf(db, caf_id, tenant.empresa_id)

        sucursal_nombre: str | None = None
        if body.sucursal_id is not None:
            sucursal = db.query(Sucursal).filter(
                Sucursal.id == body.sucursal_id,
                Sucursal.empresa_id == tenant.empresa_id,
            ).first()
            if not sucursal:
                raise HTTPException(
                    404, "Sucursal no encontrada para esta empresa",
                )
            if not sucursal.activa:
                raise HTTPException(
                    400, "Sucursal inactiva: reactivarla antes de asignarle CAFs",
                )
            sucursal_nombre = sucursal.nombre

        mgr = CAFManagerDB(db, tenant.empresa_id)
        try:
            mgr.configurar_asignaciones(
                caf_id,
                [{
                    "sucursal_id": body.sucursal_id,
                    "rango_desde": caf.rango_desde,
                    "rango_hasta": caf.rango_hasta,
                }],
            )
            caf.sucursal_id = body.sucursal_id
            db.commit()
        except ValueError as e:
            db.rollback()
            raise HTTPException(400, str(e))

        if body.sucursal_id is None:
            mensaje = (
                f"CAF tipo {caf.tipo_dte} "
                f"({caf.rango_desde}-{caf.rango_hasta}) "
                f"liberado al pool del server."
            )
        else:
            mensaje = (
                f"CAF tipo {caf.tipo_dte} "
                f"({caf.rango_desde}-{caf.rango_hasta}) "
                f"asignado a {sucursal_nombre}."
            )

        return AsignarSucursalOut(
            ok=True,
            caf_id=caf.id,
            sucursal_id=body.sucursal_id,
            sucursal_nombre=sucursal_nombre,
            mensaje=mensaje,
        )
    finally:
        tenant.close()

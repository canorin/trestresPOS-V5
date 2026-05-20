"""Sincronización POS ↔ Servidor — offline-first.

El terminal POS (Tauri + SQLite local) opera sin internet.
Al recuperar conexión, sincroniza bidireccionalmente:

  PULL (servidor → terminal):
    - Config empresa (RUT, razón social, giro, certificado, etc.)
    - Sucursal asignada
    - Artículos + precios de la sucursal
    - Familias activas en la sucursal
    - CAFs (folios autorizados para emisión local)
    - Clientes frecuentes
    - Inventario/stock de la sucursal

  PUSH (terminal → servidor):
    - Ventas nuevas
    - DTEs emitidos (firmados localmente)
    - Sesiones de caja (aperturas/cierres)
    - Movimientos de stock (descontados por ventas)
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from crumbpos.db.models import (
    Empresa, Sucursal, Caja, Familia, FamiliaSucursal, Articulo,
    ArticuloSucursal, CafFolio, Cliente, Bodega, Stock,
    Venta, VentaItem, Pago, DteEmitido, SesionCaja, MovimientoStock,
)
from crumbpos.api.dependencies import get_tenant, TenantContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sync", tags=["sync"])


# ═══════════════════════════════════════════════════════════════
# SCHEMAS — PULL (servidor → terminal)
# ═══════════════════════════════════════════════════════════════

class EmpresaSync(BaseModel):
    id: str
    rut: str
    razon_social: str
    nombre_fantasia: Optional[str] = None
    giro: str
    acteco: Optional[int] = None
    direccion: str
    comuna: str
    ciudad: str
    tasa_iva: int
    ambiente_sii: str
    fecha_resolucion: Optional[str] = None
    numero_resolucion: int
    # Certificado digital en base64 (para firma local)
    cert_data: Optional[str] = None
    cert_password: Optional[str] = None
    cert_rut_firmante: Optional[str] = None
    logo_url: Optional[str] = None

    model_config = {"from_attributes": True}


class SucursalSync(BaseModel):
    id: str
    nombre: str
    codigo: Optional[str] = None
    direccion: str
    comuna: str
    ciudad: str
    sii_sucursal: str
    cdg_sii_sucursal: Optional[int] = None

    model_config = {"from_attributes": True}


class FamiliaSync(BaseModel):
    id: str
    nombre: str
    codigo: Optional[str] = None
    color: Optional[str] = None
    icono: Optional[str] = None
    parent_id: Optional[str] = None
    orden: int
    activa: bool

    model_config = {"from_attributes": True}


class ArticuloSync(BaseModel):
    id: str
    familia_id: Optional[str] = None
    sku: Optional[str] = None
    codigo_barras: Optional[str] = None
    nombre: str
    nombre_corto: Optional[str] = None
    unidad_medida: str
    precio_default: int
    costo_default: int
    es_exento: bool
    activo: bool
    # Precio específico de la sucursal (si existe)
    precio_sucursal: Optional[int] = None
    activo_sucursal: bool = True

    model_config = {"from_attributes": True}


class CafSync(BaseModel):
    id: str
    tipo_dte: int
    rango_desde: int
    rango_hasta: int
    folio_actual: int
    caf_xml_raw: str  # XML completo para firma local
    estado: str

    model_config = {"from_attributes": True}


class ClienteSync(BaseModel):
    id: str
    rut: str
    razon_social: str
    nombre_fantasia: Optional[str] = None
    giro: str
    direccion: str
    comuna: str
    ciudad: Optional[str] = None
    contacto_nombre: Optional[str] = None
    contacto_email: Optional[str] = None
    contacto_telefono: Optional[str] = None
    condicion_pago: Optional[int] = None
    activo: bool

    model_config = {"from_attributes": True}


class StockSync(BaseModel):
    articulo_id: str
    bodega_id: str
    cantidad: float
    stock_minimo: float

    model_config = {"from_attributes": True}


class PullCompletoOut(BaseModel):
    """Respuesta del pull completo — todo lo que el terminal necesita."""
    empresa: EmpresaSync
    sucursal: SucursalSync
    caja_id: str
    caja_nombre: str
    familias: list[FamiliaSync]
    articulos: list[ArticuloSync]
    cafs: list[CafSync]
    clientes: list[ClienteSync]
    stock: list[StockSync]
    timestamp: str  # ISO — para futuros pulls incrementales


class PullIncrementalIn(BaseModel):
    """Request de pull incremental — solo cambios desde una fecha."""
    caja_id: str
    desde: str  # ISO datetime del último sync


class PullIncrementalOut(BaseModel):
    """Cambios desde la última sincronización."""
    articulos_actualizados: list[ArticuloSync]
    familias_actualizadas: list[FamiliaSync]
    clientes_actualizados: list[ClienteSync]
    cafs_nuevos: list[CafSync]
    stock_actualizado: list[StockSync]
    timestamp: str


# ═══════════════════════════════════════════════════════════════
# SCHEMAS — PUSH (terminal → servidor)
# ═══════════════════════════════════════════════════════════════

class VentaItemPush(BaseModel):
    id: str
    articulo_id: Optional[str] = None
    nombre: str
    cantidad: float
    precio_unitario: int
    descuento_pct: Optional[float] = None
    descuento_monto: Optional[int] = None
    monto_linea: int
    es_exento: bool = False


class PagoPush(BaseModel):
    id: str
    medio: str
    monto: int
    referencia: Optional[str] = None


class VentaPush(BaseModel):
    id: str
    sucursal_id: str
    sesion_caja_id: Optional[str] = None
    usuario_id: str
    fecha: str  # ISO
    tipo_dte: int
    folio: Optional[int] = None
    receptor_rut: Optional[str] = None
    receptor_razon: Optional[str] = None
    receptor_giro: Optional[str] = None
    receptor_dir: Optional[str] = None
    receptor_comuna: Optional[str] = None
    monto_neto: Optional[int] = None
    monto_exento: Optional[int] = None
    iva: Optional[int] = None
    monto_total: int
    estado: str = "completada"
    ted_xml: Optional[str] = None
    xml_firmado: Optional[str] = None
    items: list[VentaItemPush]
    pagos: list[PagoPush]


class DtePush(BaseModel):
    id: str
    sucursal_id: Optional[str] = None
    venta_id: Optional[str] = None
    tipo_dte: int
    folio: int
    fecha_emision: str  # YYYY-MM-DD
    receptor_rut: Optional[str] = None
    receptor_razon: Optional[str] = None
    monto_neto: Optional[int] = None
    monto_exento: Optional[int] = None
    iva: Optional[int] = None
    monto_total: int
    xml_firmado: Optional[str] = None
    ted_xml: Optional[str] = None


class SesionCajaPush(BaseModel):
    id: str
    sucursal_id: str
    caja_id: str
    usuario_id: str
    apertura_at: str  # ISO
    cierre_at: Optional[str] = None
    monto_apertura: int = 0
    monto_cierre_esperado: Optional[int] = None
    monto_cierre_real: Optional[int] = None
    diferencia: Optional[int] = None
    observacion: Optional[str] = None
    estado: str = "abierta"


class MovimientoStockPush(BaseModel):
    id: str
    bodega_id: str
    articulo_id: str
    tipo: str
    cantidad: float
    referencia_id: Optional[str] = None
    referencia_tipo: Optional[str] = None
    usuario_id: Optional[str] = None
    fecha: str  # ISO


class CafFolioUpdate(BaseModel):
    """Actualización de folio_actual de un CAF tras usar folios localmente."""
    caf_id: str
    folio_actual: int  # nuevo folio_actual tras emisión local


class PushIn(BaseModel):
    """Datos que el terminal envía al servidor."""
    caja_id: str
    ventas: list[VentaPush] = []
    dtes: list[DtePush] = []
    sesiones_caja: list[SesionCajaPush] = []
    movimientos_stock: list[MovimientoStockPush] = []
    cafs_actualizados: list[CafFolioUpdate] = []


class PushOut(BaseModel):
    """Resultado del push."""
    ventas_recibidas: int
    dtes_recibidos: int
    sesiones_recibidas: int
    movimientos_recibidos: int
    cafs_actualizados: int
    errores: list[str]


# ═══════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@router.get("/pull-completo/{caja_id}", response_model=PullCompletoOut)
def pull_completo(caja_id: str, tenant: TenantContext = Depends(get_tenant)):
    """Descarga completa para instalación inicial o reinstalación del POS.

    Retorna TODO lo que el terminal necesita para operar offline:
    empresa, sucursal, artículos, precios, familias, CAFs, certificado,
    clientes e inventario.

    El terminal guarda esto en su SQLite local y puede operar sin conexión.
    """
    try:
        # Validar caja y obtener sucursal
        caja = tenant.db.query(Caja).join(Sucursal).filter(
            Caja.id == caja_id,
            Sucursal.empresa_id == tenant.empresa_id,
            Caja.activa == True,
        ).first()
        if not caja:
            raise HTTPException(404, "Caja no encontrada o no activa")

        sucursal = caja.sucursal

        # Empresa (con certificado para firma local)
        empresa = tenant.db.query(Empresa).filter(
            Empresa.id == tenant.empresa_id,
        ).first()

        # Familias activas en esta sucursal
        familia_ids = [
            fs.familia_id for fs in
            tenant.db.query(FamiliaSucursal).filter(
                FamiliaSucursal.sucursal_id == sucursal.id,
                FamiliaSucursal.activa == True,
            ).all()
        ]
        # Incluir también familias sin registro en familia_sucursal (por defecto activas)
        todas_familias = tenant.db.query(Familia).filter(
            Familia.empresa_id == tenant.empresa_id,
            Familia.activa == True,
        ).all()
        familias_out = []
        for fam in todas_familias:
            # Si hay registro explícito inactivo, excluir
            fs = tenant.db.query(FamiliaSucursal).filter(
                FamiliaSucursal.familia_id == fam.id,
                FamiliaSucursal.sucursal_id == sucursal.id,
            ).first()
            if fs and not fs.activa:
                continue
            familias_out.append(FamiliaSync.model_validate(fam))

        # Artículos con precio de sucursal
        articulos_empresa = tenant.db.query(Articulo).filter(
            Articulo.empresa_id == tenant.empresa_id,
            Articulo.activo == True,
        ).all()

        articulos_out = []
        for art in articulos_empresa:
            art_suc = tenant.db.query(ArticuloSucursal).filter(
                ArticuloSucursal.articulo_id == art.id,
                ArticuloSucursal.sucursal_id == sucursal.id,
            ).first()
            # Si desactivado explícitamente en esta sucursal, excluir
            if art_suc and not art_suc.activo:
                continue

            articulos_out.append(ArticuloSync(
                id=art.id,
                familia_id=art.familia_id,
                sku=art.sku,
                codigo_barras=art.codigo_barras,
                nombre=art.nombre,
                nombre_corto=art.nombre_corto,
                unidad_medida=art.unidad_medida,
                precio_default=art.precio_default,
                costo_default=art.costo_default,
                es_exento=art.es_exento,
                activo=True,
                precio_sucursal=art_suc.precio_venta if art_suc else None,
                activo_sucursal=True,
            ))

        # CAFs activos
        cafs = tenant.db.query(CafFolio).filter(
            CafFolio.empresa_id == tenant.empresa_id,
            CafFolio.estado == "activo",
        ).all()
        cafs_out = []
        for caf in cafs:
            raw = caf.caf_xml_raw
            if isinstance(raw, bytes):
                raw = raw.decode("iso-8859-1")
            cafs_out.append(CafSync(
                id=caf.id,
                tipo_dte=caf.tipo_dte,
                rango_desde=caf.rango_desde,
                rango_hasta=caf.rango_hasta,
                folio_actual=caf.folio_actual,
                caf_xml_raw=raw,
                estado=caf.estado,
            ))

        # Clientes
        clientes = tenant.db.query(Cliente).filter(
            Cliente.empresa_id == tenant.empresa_id,
            Cliente.activo == True,
        ).all()

        # Stock de la sucursal
        bodegas = tenant.db.query(Bodega).filter(
            Bodega.sucursal_id == sucursal.id,
        ).all()
        bodega_ids = [b.id for b in bodegas]
        stock_items = []
        if bodega_ids:
            stock_items = tenant.db.query(Stock).filter(
                Stock.bodega_id.in_(bodega_ids),
            ).all()

        return PullCompletoOut(
            empresa=EmpresaSync.model_validate(empresa),
            sucursal=SucursalSync.model_validate(sucursal),
            caja_id=caja.id,
            caja_nombre=caja.nombre,
            familias=familias_out,
            articulos=articulos_out,
            cafs=cafs_out,
            clientes=[ClienteSync.model_validate(c) for c in clientes],
            stock=[StockSync.model_validate(s) for s in stock_items],
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
    finally:
        tenant.close()


@router.post("/pull-incremental", response_model=PullIncrementalOut)
def pull_incremental(body: PullIncrementalIn, tenant: TenantContext = Depends(get_tenant)):
    """Descarga solo los cambios desde la última sincronización.

    El terminal envía el timestamp de su último sync y recibe
    solo lo que cambió: artículos modificados, precios nuevos,
    familias actualizadas, CAFs nuevos, stock actualizado.
    """
    try:
        desde = datetime.fromisoformat(body.desde)

        # Validar caja
        caja = tenant.db.query(Caja).join(Sucursal).filter(
            Caja.id == body.caja_id,
            Sucursal.empresa_id == tenant.empresa_id,
            Caja.activa == True,
        ).first()
        if not caja:
            raise HTTPException(404, "Caja no encontrada")

        sucursal = caja.sucursal

        # Artículos actualizados
        articulos_mod = tenant.db.query(Articulo).filter(
            Articulo.empresa_id == tenant.empresa_id,
            Articulo.updated_at > desde,
        ).all()

        articulos_out = []
        for art in articulos_mod:
            art_suc = tenant.db.query(ArticuloSucursal).filter(
                ArticuloSucursal.articulo_id == art.id,
                ArticuloSucursal.sucursal_id == sucursal.id,
            ).first()
            articulos_out.append(ArticuloSync(
                id=art.id,
                familia_id=art.familia_id,
                sku=art.sku,
                codigo_barras=art.codigo_barras,
                nombre=art.nombre,
                nombre_corto=art.nombre_corto,
                unidad_medida=art.unidad_medida,
                precio_default=art.precio_default,
                costo_default=art.costo_default,
                es_exento=art.es_exento,
                activo=art.activo,
                precio_sucursal=art_suc.precio_venta if art_suc else None,
                activo_sucursal=art_suc.activo if art_suc else True,
            ))

        # También revisar ArticuloSucursal actualizados
        art_suc_mod = tenant.db.query(ArticuloSucursal).filter(
            ArticuloSucursal.sucursal_id == sucursal.id,
            ArticuloSucursal.updated_at > desde,
        ).all()
        art_ids_ya = {a.id for a in articulos_mod}
        for art_suc in art_suc_mod:
            if art_suc.articulo_id not in art_ids_ya:
                art = tenant.db.query(Articulo).filter(
                    Articulo.id == art_suc.articulo_id,
                ).first()
                if art:
                    articulos_out.append(ArticuloSync(
                        id=art.id,
                        familia_id=art.familia_id,
                        sku=art.sku,
                        codigo_barras=art.codigo_barras,
                        nombre=art.nombre,
                        nombre_corto=art.nombre_corto,
                        unidad_medida=art.unidad_medida,
                        precio_default=art.precio_default,
                        costo_default=art.costo_default,
                        es_exento=art.es_exento,
                        activo=art.activo,
                        precio_sucursal=art_suc.precio_venta,
                        activo_sucursal=art_suc.activo,
                    ))

        # Familias (no tienen updated_at, mandamos todas si es incremental simple)
        # Para un incremental real necesitaríamos un campo updated_at en Familia
        familias_out: list[FamiliaSync] = []

        # Clientes actualizados
        clientes_mod = tenant.db.query(Cliente).filter(
            Cliente.empresa_id == tenant.empresa_id,
            Cliente.updated_at > desde,
        ).all()

        # CAFs nuevos (creados después del último sync)
        cafs_nuevos = tenant.db.query(CafFolio).filter(
            CafFolio.empresa_id == tenant.empresa_id,
            CafFolio.estado == "activo",
            CafFolio.created_at > desde,
        ).all()
        cafs_out = []
        for caf in cafs_nuevos:
            raw = caf.caf_xml_raw
            if isinstance(raw, bytes):
                raw = raw.decode("iso-8859-1")
            cafs_out.append(CafSync(
                id=caf.id,
                tipo_dte=caf.tipo_dte,
                rango_desde=caf.rango_desde,
                rango_hasta=caf.rango_hasta,
                folio_actual=caf.folio_actual,
                caf_xml_raw=raw,
                estado=caf.estado,
            ))

        # Stock actualizado
        bodegas = tenant.db.query(Bodega).filter(
            Bodega.sucursal_id == sucursal.id,
        ).all()
        bodega_ids = [b.id for b in bodegas]
        stock_mod = []
        if bodega_ids:
            stock_mod = tenant.db.query(Stock).filter(
                Stock.bodega_id.in_(bodega_ids),
                Stock.updated_at > desde,
            ).all()

        return PullIncrementalOut(
            articulos_actualizados=articulos_out,
            familias_actualizadas=familias_out,
            clientes_actualizados=[ClienteSync.model_validate(c) for c in clientes_mod],
            cafs_nuevos=cafs_out,
            stock_actualizado=[StockSync.model_validate(s) for s in stock_mod],
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
    finally:
        tenant.close()


@router.post("/push", response_model=PushOut)
def push(body: PushIn, tenant: TenantContext = Depends(get_tenant)):
    """Recibe datos generados offline por el terminal POS.

    El terminal envía ventas, DTEs firmados, sesiones de caja y
    movimientos de stock creados mientras operaba sin conexión.
    El servidor los persiste y luego se encarga de enviarlos al SII.

    Los registros usan los mismos IDs generados localmente (UUID)
    para evitar duplicados si el push se reintenta.
    """
    try:
        errores = []

        # Validar caja
        caja = tenant.db.query(Caja).join(Sucursal).filter(
            Caja.id == body.caja_id,
            Sucursal.empresa_id == tenant.empresa_id,
        ).first()
        if not caja:
            raise HTTPException(404, "Caja no encontrada")

        # ── Sesiones de caja ──
        sesiones_ok = 0
        for ses in body.sesiones_caja:
            existing = tenant.db.query(SesionCaja).filter(
                SesionCaja.id == ses.id,
            ).first()
            if existing:
                # Actualizar si se cerró localmente
                if ses.cierre_at and not existing.cierre_at:
                    existing.cierre_at = datetime.fromisoformat(ses.cierre_at)
                    existing.monto_cierre_esperado = ses.monto_cierre_esperado
                    existing.monto_cierre_real = ses.monto_cierre_real
                    existing.diferencia = ses.diferencia
                    existing.observacion = ses.observacion
                    existing.estado = ses.estado
                sesiones_ok += 1
                continue
            try:
                tenant.db.add(SesionCaja(
                    id=ses.id,
                    sucursal_id=ses.sucursal_id,
                    caja_id=ses.caja_id,
                    usuario_id=ses.usuario_id,
                    apertura_at=datetime.fromisoformat(ses.apertura_at),
                    cierre_at=datetime.fromisoformat(ses.cierre_at) if ses.cierre_at else None,
                    monto_apertura=ses.monto_apertura,
                    monto_cierre_esperado=ses.monto_cierre_esperado,
                    monto_cierre_real=ses.monto_cierre_real,
                    diferencia=ses.diferencia,
                    observacion=ses.observacion,
                    estado=ses.estado,
                ))
                sesiones_ok += 1
            except Exception as e:
                errores.append(f"Sesión {ses.id}: {e}")

        # ── Ventas ──
        ventas_ok = 0
        for venta_data in body.ventas:
            existing = tenant.db.query(Venta).filter(
                Venta.id == venta_data.id,
            ).first()
            if existing:
                # Ya sincronizada, skip
                ventas_ok += 1
                continue
            try:
                venta = Venta(
                    id=venta_data.id,
                    empresa_id=tenant.empresa_id,
                    sucursal_id=venta_data.sucursal_id,
                    sesion_caja_id=venta_data.sesion_caja_id,
                    usuario_id=venta_data.usuario_id,
                    fecha=datetime.fromisoformat(venta_data.fecha),
                    tipo_dte=venta_data.tipo_dte,
                    folio=venta_data.folio,
                    receptor_rut=venta_data.receptor_rut,
                    receptor_razon=venta_data.receptor_razon,
                    receptor_giro=venta_data.receptor_giro,
                    receptor_dir=venta_data.receptor_dir,
                    receptor_comuna=venta_data.receptor_comuna,
                    monto_neto=venta_data.monto_neto,
                    monto_exento=venta_data.monto_exento,
                    iva=venta_data.iva,
                    monto_total=venta_data.monto_total,
                    estado=venta_data.estado,
                    sync_status="confirmado",
                    ted_xml=venta_data.ted_xml,
                    xml_firmado=venta_data.xml_firmado,
                )
                tenant.db.add(venta)

                for item in venta_data.items:
                    tenant.db.add(VentaItem(
                        id=item.id,
                        venta_id=venta.id,
                        articulo_id=item.articulo_id,
                        nombre=item.nombre,
                        cantidad=item.cantidad,
                        precio_unitario=item.precio_unitario,
                        descuento_pct=item.descuento_pct,
                        descuento_monto=item.descuento_monto,
                        monto_linea=item.monto_linea,
                        es_exento=item.es_exento,
                    ))

                for pago in venta_data.pagos:
                    tenant.db.add(Pago(
                        id=pago.id,
                        venta_id=venta.id,
                        medio=pago.medio,
                        monto=pago.monto,
                        referencia=pago.referencia,
                    ))

                ventas_ok += 1
            except Exception as e:
                errores.append(f"Venta {venta_data.id}: {e}")

        # ── DTEs emitidos ──
        dtes_ok = 0
        for dte_data in body.dtes:
            existing = tenant.db.query(DteEmitido).filter(
                DteEmitido.id == dte_data.id,
            ).first()
            if existing:
                dtes_ok += 1
                continue
            try:
                tenant.db.add(DteEmitido(
                    id=dte_data.id,
                    empresa_id=tenant.empresa_id,
                    sucursal_id=dte_data.sucursal_id,
                    venta_id=dte_data.venta_id,
                    tipo_dte=dte_data.tipo_dte,
                    folio=dte_data.folio,
                    fecha_emision=datetime.strptime(dte_data.fecha_emision, "%Y-%m-%d").date(),
                    receptor_rut=dte_data.receptor_rut,
                    receptor_razon=dte_data.receptor_razon,
                    monto_neto=dte_data.monto_neto,
                    monto_exento=dte_data.monto_exento,
                    iva=dte_data.iva,
                    monto_total=dte_data.monto_total,
                    xml_firmado=dte_data.xml_firmado,
                    ted_xml=dte_data.ted_xml,
                    estado_sii="pendiente",  # El servidor se encarga de enviar al SII
                    sync_status="confirmado",
                ))
                dtes_ok += 1
            except Exception as e:
                errores.append(f"DTE {dte_data.tipo_dte}-{dte_data.folio}: {e}")

        # ── Movimientos de stock ──
        movs_ok = 0
        for mov in body.movimientos_stock:
            existing = tenant.db.query(MovimientoStock).filter(
                MovimientoStock.id == mov.id,
            ).first()
            if existing:
                movs_ok += 1
                continue
            try:
                tenant.db.add(MovimientoStock(
                    id=mov.id,
                    bodega_id=mov.bodega_id,
                    articulo_id=mov.articulo_id,
                    tipo=mov.tipo,
                    cantidad=mov.cantidad,
                    referencia_id=mov.referencia_id,
                    referencia_tipo=mov.referencia_tipo,
                    usuario_id=mov.usuario_id,
                    fecha=datetime.fromisoformat(mov.fecha),
                ))

                # Actualizar stock actual
                stock = tenant.db.query(Stock).filter(
                    Stock.articulo_id == mov.articulo_id,
                    Stock.bodega_id == mov.bodega_id,
                ).first()
                if stock:
                    stock.cantidad += mov.cantidad

                movs_ok += 1
            except Exception as e:
                errores.append(f"Mov stock {mov.id}: {e}")

        # ── Actualizar folios CAF ──
        cafs_ok = 0
        for caf_upd in body.cafs_actualizados:
            caf = tenant.db.query(CafFolio).filter(
                CafFolio.id == caf_upd.caf_id,
                CafFolio.empresa_id == tenant.empresa_id,
            ).first()
            if not caf:
                errores.append(f"CAF {caf_upd.caf_id} no encontrado")
                continue
            # Solo avanzar, nunca retroceder (el terminal pudo usar folios)
            if caf_upd.folio_actual > caf.folio_actual:
                caf.folio_actual = caf_upd.folio_actual
                if caf.folio_actual > caf.rango_hasta:
                    caf.estado = "agotado"
            cafs_ok += 1

        # Commit todo
        try:
            tenant.db.commit()
        except Exception as e:
            tenant.db.rollback()
            raise HTTPException(500, f"Error al guardar sync: {e}")

        return PushOut(
            ventas_recibidas=ventas_ok,
            dtes_recibidos=dtes_ok,
            sesiones_recibidas=sesiones_ok,
            movimientos_recibidos=movs_ok,
            cafs_actualizados=cafs_ok,
            errores=errores,
        )
    finally:
        tenant.close()

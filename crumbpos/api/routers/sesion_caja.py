"""Endpoints de Sesion de Caja (cash register shift) — multi-tenant."""
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, and_
from sqlalchemy.orm import Session

from crumbpos.core.roles import puede_gestionar_empresa
from crumbpos.db.models import (
    SesionCaja, ArqueoDetalle, Venta, Pago, Caja, Sucursal, UsuarioSucursal,
)
from crumbpos.api.dependencies import get_tenant, TenantContext

router = APIRouter(prefix="/api/sesion-caja", tags=["sesion-caja"])


# ══════════════════════════════════════════════════════════════════
# SCHEMAS
# ══════════════════════════════════════════════════════════════════

class AperturaIn(BaseModel):
    caja_id: str
    monto_apertura: int = 0


class ArqueoDetalleIn(BaseModel):
    denominacion: int  # 20000, 10000, 5000, 2000, 1000, 500, 100, 50, 10
    cantidad: int


class CierreIn(BaseModel):
    monto_cierre_real: int
    observacion: str | None = None
    arqueo: list[ArqueoDetalleIn] = []


class ArqueoDetalleOut(BaseModel):
    id: str
    denominacion: int
    cantidad: int
    subtotal: int
    model_config = {"from_attributes": True}


class SesionCajaOut(BaseModel):
    id: str
    sucursal_id: str
    caja_id: str
    usuario_id: str
    apertura_at: str
    cierre_at: str | None
    monto_apertura: int
    monto_cierre_esperado: int | None
    monto_cierre_real: int | None
    diferencia: int | None
    estado: str
    observacion: str | None
    reporte_z: dict | None
    model_config = {"from_attributes": True}


class SesionCajaDetailOut(SesionCajaOut):
    arqueo: list[ArqueoDetalleOut] = []


# ══════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════

def _validar_acceso_sucursal(db: Session, user_id: str, user_rol: str, sucursal_id: str):
    """Verifica que el usuario tenga acceso a la sucursal.

    Quien gestiona la empresa (super_admin / master_client / administrador)
    tiene acceso automático a todas las sucursales. El resto
    (administrador_tienda / cajero) debe tener una fila en
    ``UsuarioSucursal`` para la sucursal pedida.
    """
    if puede_gestionar_empresa(user_rol):
        return
    acceso = db.query(UsuarioSucursal).filter(
        UsuarioSucursal.usuario_id == user_id,
        UsuarioSucursal.sucursal_id == sucursal_id,
    ).first()
    if not acceso:
        raise HTTPException(403, "No tienes acceso a esta sucursal")


def _generar_reporte_z(db: Session, sesion: SesionCaja) -> dict:
    """Genera el reporte Z (resumen de cierre) para la sesion."""
    ventas = (
        db.query(Venta)
        .filter(
            Venta.sesion_caja_id == sesion.id,
            Venta.estado == "completada",
        )
        .all()
    )

    total_ventas = 0
    num_transacciones = len(ventas)
    por_tipo_dte: dict[str, dict] = {}
    por_medio_pago: dict[str, int] = {
        "efectivo": 0,
        "debito": 0,
        "credito": 0,
        "transferencia": 0,
    }
    folios_usados: dict[str, dict] = {}

    for v in ventas:
        total_ventas += v.monto_total

        # Por tipo DTE
        tipo_key = str(v.tipo_dte)
        if tipo_key not in por_tipo_dte:
            por_tipo_dte[tipo_key] = {"count": 0, "total": 0}
        por_tipo_dte[tipo_key]["count"] += 1
        por_tipo_dte[tipo_key]["total"] += v.monto_total

        # Folios
        if v.folio is not None:
            if tipo_key not in folios_usados:
                folios_usados[tipo_key] = {"desde": v.folio, "hasta": v.folio}
            else:
                folios_usados[tipo_key]["desde"] = min(
                    folios_usados[tipo_key]["desde"], v.folio
                )
                folios_usados[tipo_key]["hasta"] = max(
                    folios_usados[tipo_key]["hasta"], v.folio
                )

        # Pagos por medio
        for pago in v.pagos:
            medio = pago.medio
            if medio in por_medio_pago:
                por_medio_pago[medio] += pago.monto
            else:
                por_medio_pago[medio] = pago.monto

    ticket_promedio = total_ventas // num_transacciones if num_transacciones > 0 else 0

    return {
        "total_ventas": total_ventas,
        "num_transacciones": num_transacciones,
        "ticket_promedio": ticket_promedio,
        "por_tipo_dte": por_tipo_dte,
        "por_medio_pago": por_medio_pago,
        "folios_usados": folios_usados,
    }


# ══════════════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════════════

@router.post("/abrir", response_model=SesionCajaOut, status_code=201)
def abrir_sesion(
    body: AperturaIn,
    tenant: TenantContext = Depends(get_tenant),
):
    """Abre una nueva sesion de caja (turno)."""
    try:
        db = tenant.db

        # Validar que la caja existe y pertenece a la empresa (via Sucursal)
        caja = (
            db.query(Caja)
            .join(Sucursal, Caja.sucursal_id == Sucursal.id)
            .filter(
                Caja.id == body.caja_id,
                Sucursal.empresa_id == tenant.empresa_id,
                Caja.activa == True,
            )
            .first()
        )
        if not caja:
            raise HTTPException(404, "Caja no encontrada o inactiva")

        # Validar acceso del usuario a la sucursal
        _validar_acceso_sucursal(
            db, tenant.user.id, tenant.user.rol, caja.sucursal_id
        )

        # Verificar que no haya sesion abierta en esa caja
        sesion_existente = (
            db.query(SesionCaja)
            .filter(
                SesionCaja.caja_id == body.caja_id,
                SesionCaja.estado == "abierta",
            )
            .first()
        )
        if sesion_existente:
            raise HTTPException(
                409,
                f"Ya existe una sesion abierta en esta caja (id: {sesion_existente.id})",
            )

        sesion = SesionCaja(
            sucursal_id=caja.sucursal_id,
            caja_id=body.caja_id,
            usuario_id=tenant.user.id,
            monto_apertura=body.monto_apertura,
            estado="abierta",
        )
        db.add(sesion)
        db.commit()
        db.refresh(sesion)
        return sesion
    finally:
        tenant.close()


@router.post("/{sesion_id}/cerrar", response_model=SesionCajaOut)
def cerrar_sesion(
    sesion_id: str,
    body: CierreIn,
    tenant: TenantContext = Depends(get_tenant),
):
    """Cierra una sesion de caja con arqueo."""
    try:
        db = tenant.db

        sesion = db.query(SesionCaja).filter(SesionCaja.id == sesion_id).first()
        if not sesion:
            raise HTTPException(404, "Sesion de caja no encontrada")
        if sesion.estado != "abierta":
            raise HTTPException(409, "La sesion ya esta cerrada")

        # Solo el usuario que abrio o un admin de empresa puede cerrar
        es_admin = puede_gestionar_empresa(tenant.user.rol)
        if sesion.usuario_id != tenant.user.id and not es_admin:
            raise HTTPException(403, "Solo el usuario que abrio la sesion o un admin puede cerrarla")

        # Calcular monto_cierre_esperado:
        # apertura + total de pagos en efectivo de ventas completadas en esta sesion
        total_efectivo = (
            db.query(func.coalesce(func.sum(Pago.monto), 0))
            .join(Venta, Pago.venta_id == Venta.id)
            .filter(
                Venta.sesion_caja_id == sesion.id,
                Venta.estado == "completada",
                Pago.medio == "efectivo",
            )
            .scalar()
        )
        monto_cierre_esperado = sesion.monto_apertura + int(total_efectivo)

        # Guardar arqueo detalle
        for item in body.arqueo:
            detalle = ArqueoDetalle(
                sesion_caja_id=sesion.id,
                denominacion=item.denominacion,
                cantidad=item.cantidad,
                subtotal=item.denominacion * item.cantidad,
            )
            db.add(detalle)

        # Generar reporte Z
        reporte_z = _generar_reporte_z(db, sesion)

        # Actualizar sesion
        sesion.monto_cierre_esperado = monto_cierre_esperado
        sesion.monto_cierre_real = body.monto_cierre_real
        sesion.diferencia = body.monto_cierre_real - monto_cierre_esperado
        sesion.observacion = body.observacion
        sesion.estado = "cerrada"
        sesion.cierre_at = datetime.now(timezone.utc)
        sesion.reporte_z = reporte_z

        db.commit()
        db.refresh(sesion)
        return sesion
    finally:
        tenant.close()


@router.get("/activa", response_model=SesionCajaOut | None)
def sesion_activa(
    tenant: TenantContext = Depends(get_tenant),
):
    """Obtiene la sesion abierta del usuario actual, si existe."""
    try:
        sesion = (
            tenant.db.query(SesionCaja)
            .filter(
                SesionCaja.usuario_id == tenant.user.id,
                SesionCaja.estado == "abierta",
            )
            .first()
        )
        return sesion
    finally:
        tenant.close()


@router.get("/", response_model=list[SesionCajaOut])
def listar_sesiones(
    sucursal_id: str | None = None,
    fecha_desde: date | None = None,
    fecha_hasta: date | None = None,
    estado: str | None = None,
    limit: int = Query(50, le=200),
    tenant: TenantContext = Depends(get_tenant),
):
    """Lista sesiones de caja con filtros opcionales."""
    try:
        db = tenant.db
        query = db.query(SesionCaja).join(
            Sucursal, SesionCaja.sucursal_id == Sucursal.id
        ).filter(
            Sucursal.empresa_id == tenant.empresa_id,
        )

        if sucursal_id:
            query = query.filter(SesionCaja.sucursal_id == sucursal_id)
        if fecha_desde:
            query = query.filter(
                SesionCaja.apertura_at >= datetime.combine(fecha_desde, datetime.min.time())
            )
        if fecha_hasta:
            query = query.filter(
                SesionCaja.apertura_at <= datetime.combine(fecha_hasta, datetime.max.time())
            )
        if estado:
            query = query.filter(SesionCaja.estado == estado)

        return query.order_by(SesionCaja.apertura_at.desc()).limit(limit).all()
    finally:
        tenant.close()


@router.get("/{sesion_id}", response_model=SesionCajaDetailOut)
def detalle_sesion(
    sesion_id: str,
    tenant: TenantContext = Depends(get_tenant),
):
    """Obtiene el detalle de una sesion de caja con su arqueo."""
    try:
        db = tenant.db

        sesion = (
            db.query(SesionCaja)
            .join(Sucursal, SesionCaja.sucursal_id == Sucursal.id)
            .filter(
                SesionCaja.id == sesion_id,
                Sucursal.empresa_id == tenant.empresa_id,
            )
            .first()
        )
        if not sesion:
            raise HTTPException(404, "Sesion de caja no encontrada")

        # Cargar arqueo
        arqueo = (
            db.query(ArqueoDetalle)
            .filter(ArqueoDetalle.sesion_caja_id == sesion.id)
            .order_by(ArqueoDetalle.denominacion.desc())
            .all()
        )

        # Construir respuesta con arqueo
        out = SesionCajaDetailOut.model_validate(sesion)
        out.arqueo = [ArqueoDetalleOut.model_validate(a) for a in arqueo]
        return out
    finally:
        tenant.close()


@router.post("/{sesion_id}/forzar-cierre", response_model=SesionCajaOut)
def forzar_cierre(
    sesion_id: str,
    tenant: TenantContext = Depends(get_tenant),
):
    """Cierra forzosamente una sesion (solo admin). No requiere arqueo."""
    try:
        db = tenant.db

        # Solo admin de empresa puede forzar cierre
        if not puede_gestionar_empresa(tenant.user.rol):
            raise HTTPException(403, "Solo un administrador puede forzar el cierre")

        sesion = (
            db.query(SesionCaja)
            .join(Sucursal, SesionCaja.sucursal_id == Sucursal.id)
            .filter(
                SesionCaja.id == sesion_id,
                Sucursal.empresa_id == tenant.empresa_id,
            )
            .first()
        )
        if not sesion:
            raise HTTPException(404, "Sesion de caja no encontrada")
        if sesion.estado != "abierta":
            raise HTTPException(409, "La sesion ya esta cerrada")

        sesion.estado = "cerrada_forzada"
        sesion.cierre_at = datetime.now(timezone.utc)

        db.commit()
        db.refresh(sesion)
        return sesion
    finally:
        tenant.close()

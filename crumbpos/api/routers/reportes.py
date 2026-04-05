"""Endpoints de reportes — multi-tenant.

Reportes de ventas por periodo, sucursal, producto, medio de pago, hora,
y sesiones de caja. Compatible con SQLite (strftime en lugar de extract).
"""
from datetime import date, datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, and_
from sqlalchemy.orm import Session

from crumbpos.api.dependencies import get_tenant, TenantContext
from crumbpos.db.models import (
    Venta, VentaItem, Pago, SesionCaja, Sucursal, Articulo, Caja, Usuario,
)

router = APIRouter(prefix="/api/reportes", tags=["reportes"])


# ═══════════════════════════════════════════════════════════════
# SCHEMAS
# ═══════════════════════════════════════════════════════════════

class ReporteVentasPeriodo(BaseModel):
    fecha_desde: str
    fecha_hasta: str
    total_ventas: int
    num_transacciones: int
    ticket_promedio: int
    por_tipo_dte: dict  # {33: {count: N, total: N}, 39: {...}}
    por_medio_pago: dict  # {efectivo: N, debito: N, ...}


class ReporteProducto(BaseModel):
    articulo_id: str | None
    nombre: str
    cantidad_vendida: float
    monto_total: int


class ReporteSucursal(BaseModel):
    sucursal_id: str
    sucursal_nombre: str
    total_ventas: int
    num_transacciones: int
    ticket_promedio: int


class ReporteMedioPago(BaseModel):
    efectivo: int = 0
    debito: int = 0
    credito: int = 0
    transferencia: int = 0


class ReporteHora(BaseModel):
    hora: int
    total: int
    count: int


class ReporteSesionCaja(BaseModel):
    id: str
    sucursal_nombre: str
    caja_nombre: str
    cajero_nombre: str
    apertura_at: str
    cierre_at: str | None
    estado: str
    monto_apertura: int
    monto_cierre_esperado: int | None
    monto_cierre_real: int | None
    diferencia: int | None
    ventas_count: int
    total_ventas: int


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def _rango_datetime(fecha_desde: date, fecha_hasta: date):
    """Convierte fechas a rango de datetime para filtrar."""
    return (
        datetime.combine(fecha_desde, datetime.min.time()),
        datetime.combine(fecha_hasta, datetime.max.time()),
    )


def _base_ventas_query(db: Session, empresa_id: str, dt_desde, dt_hasta, sucursal_id=None):
    """Filtro base de ventas completadas en rango."""
    q = db.query(Venta).filter(
        Venta.empresa_id == empresa_id,
        Venta.estado == "completada",
        Venta.fecha >= dt_desde,
        Venta.fecha <= dt_hasta,
    )
    if sucursal_id:
        q = q.filter(Venta.sucursal_id == sucursal_id)
    return q


# ═══════════════════════════════════════════════════════════════
# 1. VENTAS POR PERIODO
# ═══════════════════════════════════════════════════════════════

@router.get("/ventas/periodo", response_model=ReporteVentasPeriodo)
def reporte_ventas_periodo(
    fecha_desde: date = Query(...),
    fecha_hasta: date = Query(...),
    sucursal_id: str | None = Query(None),
    tenant: TenantContext = Depends(get_tenant),
):
    """Resumen de ventas por periodo: totales, transacciones, ticket promedio,
    desglose por tipo DTE y medio de pago."""
    try:
        db = tenant.db
        dt_desde, dt_hasta = _rango_datetime(fecha_desde, fecha_hasta)

        # --- Totales generales ---
        base_filter = [
            Venta.empresa_id == tenant.empresa_id,
            Venta.estado == "completada",
            Venta.fecha >= dt_desde,
            Venta.fecha <= dt_hasta,
        ]
        if sucursal_id:
            base_filter.append(Venta.sucursal_id == sucursal_id)

        stats = db.query(
            func.coalesce(func.sum(Venta.monto_total), 0),
            func.count(Venta.id),
        ).filter(and_(*base_filter)).first()

        total_ventas = int(stats[0])
        num_transacciones = int(stats[1])
        ticket_promedio = int(total_ventas / num_transacciones) if num_transacciones > 0 else 0

        # --- Por tipo DTE ---
        dte_rows = db.query(
            Venta.tipo_dte,
            func.count(Venta.id).label("count"),
            func.coalesce(func.sum(Venta.monto_total), 0).label("total"),
        ).filter(and_(*base_filter)).group_by(Venta.tipo_dte).all()

        por_tipo_dte = {}
        for row in dte_rows:
            por_tipo_dte[str(row.tipo_dte)] = {
                "count": int(row.count),
                "total": int(row.total),
            }

        # --- Por medio de pago ---
        pago_filter = [
            Pago.venta_id == Venta.id,
            *base_filter,
        ]
        pago_rows = db.query(
            Pago.medio,
            func.coalesce(func.sum(Pago.monto), 0).label("total"),
        ).join(Venta, Pago.venta_id == Venta.id).filter(
            and_(*base_filter)
        ).group_by(Pago.medio).all()

        por_medio_pago = {}
        for row in pago_rows:
            por_medio_pago[row.medio] = int(row.total)

        return ReporteVentasPeriodo(
            fecha_desde=str(fecha_desde),
            fecha_hasta=str(fecha_hasta),
            total_ventas=total_ventas,
            num_transacciones=num_transacciones,
            ticket_promedio=ticket_promedio,
            por_tipo_dte=por_tipo_dte,
            por_medio_pago=por_medio_pago,
        )
    finally:
        tenant.close()


# ═══════════════════════════════════════════════════════════════
# 2. VENTAS POR SUCURSAL
# ═══════════════════════════════════════════════════════════════

@router.get("/ventas/por-sucursal", response_model=list[ReporteSucursal])
def reporte_ventas_por_sucursal(
    fecha_desde: date = Query(...),
    fecha_hasta: date = Query(...),
    tenant: TenantContext = Depends(get_tenant),
):
    """Ventas agrupadas por sucursal."""
    try:
        db = tenant.db
        dt_desde, dt_hasta = _rango_datetime(fecha_desde, fecha_hasta)

        rows = db.query(
            Venta.sucursal_id,
            Sucursal.nombre.label("sucursal_nombre"),
            func.coalesce(func.sum(Venta.monto_total), 0).label("total"),
            func.count(Venta.id).label("count"),
        ).join(Sucursal, Venta.sucursal_id == Sucursal.id).filter(
            Venta.empresa_id == tenant.empresa_id,
            Venta.estado == "completada",
            Venta.fecha >= dt_desde,
            Venta.fecha <= dt_hasta,
        ).group_by(Venta.sucursal_id, Sucursal.nombre).all()

        return [
            ReporteSucursal(
                sucursal_id=row.sucursal_id,
                sucursal_nombre=row.sucursal_nombre,
                total_ventas=int(row.total),
                num_transacciones=int(row.count),
                ticket_promedio=int(row.total / row.count) if row.count > 0 else 0,
            )
            for row in rows
        ]
    finally:
        tenant.close()


# ═══════════════════════════════════════════════════════════════
# 3. VENTAS POR PRODUCTO (TOP N)
# ═══════════════════════════════════════════════════════════════

@router.get("/ventas/por-producto", response_model=list[ReporteProducto])
def reporte_ventas_por_producto(
    fecha_desde: date = Query(...),
    fecha_hasta: date = Query(...),
    sucursal_id: str | None = Query(None),
    limit: int = Query(20, le=100),
    tenant: TenantContext = Depends(get_tenant),
):
    """Top productos por monto vendido."""
    try:
        db = tenant.db
        dt_desde, dt_hasta = _rango_datetime(fecha_desde, fecha_hasta)

        base_filter = [
            Venta.empresa_id == tenant.empresa_id,
            Venta.estado == "completada",
            Venta.fecha >= dt_desde,
            Venta.fecha <= dt_hasta,
        ]
        if sucursal_id:
            base_filter.append(Venta.sucursal_id == sucursal_id)

        rows = db.query(
            VentaItem.articulo_id,
            VentaItem.nombre,
            func.sum(VentaItem.cantidad).label("cantidad_vendida"),
            func.coalesce(func.sum(VentaItem.monto_linea), 0).label("monto_total"),
        ).join(Venta, VentaItem.venta_id == Venta.id).filter(
            and_(*base_filter)
        ).group_by(
            VentaItem.articulo_id, VentaItem.nombre,
        ).order_by(
            func.sum(VentaItem.monto_linea).desc()
        ).limit(limit).all()

        return [
            ReporteProducto(
                articulo_id=row.articulo_id,
                nombre=row.nombre,
                cantidad_vendida=float(row.cantidad_vendida),
                monto_total=int(row.monto_total),
            )
            for row in rows
        ]
    finally:
        tenant.close()


# ═══════════════════════════════════════════════════════════════
# 4. VENTAS POR MEDIO DE PAGO
# ═══════════════════════════════════════════════════════════════

@router.get("/ventas/por-medio-pago", response_model=ReporteMedioPago)
def reporte_ventas_por_medio_pago(
    fecha_desde: date = Query(...),
    fecha_hasta: date = Query(...),
    sucursal_id: str | None = Query(None),
    tenant: TenantContext = Depends(get_tenant),
):
    """Totales por medio de pago."""
    try:
        db = tenant.db
        dt_desde, dt_hasta = _rango_datetime(fecha_desde, fecha_hasta)

        base_filter = [
            Venta.empresa_id == tenant.empresa_id,
            Venta.estado == "completada",
            Venta.fecha >= dt_desde,
            Venta.fecha <= dt_hasta,
        ]
        if sucursal_id:
            base_filter.append(Venta.sucursal_id == sucursal_id)

        rows = db.query(
            Pago.medio,
            func.coalesce(func.sum(Pago.monto), 0).label("total"),
        ).join(Venta, Pago.venta_id == Venta.id).filter(
            and_(*base_filter)
        ).group_by(Pago.medio).all()

        result = {"efectivo": 0, "debito": 0, "credito": 0, "transferencia": 0}
        for row in rows:
            if row.medio in result:
                result[row.medio] = int(row.total)

        return ReporteMedioPago(**result)
    finally:
        tenant.close()


# ═══════════════════════════════════════════════════════════════
# 5. VENTAS POR HORA
# ═══════════════════════════════════════════════════════════════

@router.get("/ventas/por-hora", response_model=list[ReporteHora])
def reporte_ventas_por_hora(
    fecha_desde: date = Query(...),
    fecha_hasta: date = Query(...),
    sucursal_id: str | None = Query(None),
    tenant: TenantContext = Depends(get_tenant),
):
    """Distribución de ventas por hora del día (0-23). Usa strftime para SQLite."""
    try:
        db = tenant.db
        dt_desde, dt_hasta = _rango_datetime(fecha_desde, fecha_hasta)

        base_filter = [
            Venta.empresa_id == tenant.empresa_id,
            Venta.estado == "completada",
            Venta.fecha >= dt_desde,
            Venta.fecha <= dt_hasta,
        ]
        if sucursal_id:
            base_filter.append(Venta.sucursal_id == sucursal_id)

        hora_col = func.strftime('%H', Venta.fecha).label("hora")

        rows = db.query(
            hora_col,
            func.coalesce(func.sum(Venta.monto_total), 0).label("total"),
            func.count(Venta.id).label("count"),
        ).filter(and_(*base_filter)).group_by(hora_col).all()

        # Build map from query results
        hora_map = {}
        for row in rows:
            h = int(row.hora)
            hora_map[h] = {"total": int(row.total), "count": int(row.count)}

        # Fill all 24 hours
        return [
            ReporteHora(
                hora=h,
                total=hora_map.get(h, {}).get("total", 0),
                count=hora_map.get(h, {}).get("count", 0),
            )
            for h in range(24)
        ]
    finally:
        tenant.close()


# ═══════════════════════════════════════════════════════════════
# 6. SESIONES DE CAJA
# ═══════════════════════════════════════════════════════════════

@router.get("/caja/sesiones", response_model=list[ReporteSesionCaja])
def reporte_sesiones_caja(
    fecha_desde: date = Query(...),
    fecha_hasta: date = Query(...),
    sucursal_id: str | None = Query(None),
    tenant: TenantContext = Depends(get_tenant),
):
    """Sesiones de caja con totales y diferencias."""
    try:
        db = tenant.db
        dt_desde, dt_hasta = _rango_datetime(fecha_desde, fecha_hasta)

        q = db.query(SesionCaja).filter(
            SesionCaja.apertura_at >= dt_desde,
            SesionCaja.apertura_at <= dt_hasta,
        )
        if sucursal_id:
            q = q.filter(SesionCaja.sucursal_id == sucursal_id)

        sesiones = q.order_by(SesionCaja.apertura_at.desc()).all()

        resultados = []
        for s in sesiones:
            # Get sucursal, caja, and cajero names
            sucursal = db.query(Sucursal).filter(Sucursal.id == s.sucursal_id).first()
            caja = db.query(Caja).filter(Caja.id == s.caja_id).first()
            cajero = db.query(Usuario).filter(Usuario.id == s.usuario_id).first()

            # Count ventas and total in this session
            stats = db.query(
                func.count(Venta.id),
                func.coalesce(func.sum(Venta.monto_total), 0),
            ).filter(
                Venta.sesion_caja_id == s.id,
                Venta.estado == "completada",
            ).first()

            resultados.append(ReporteSesionCaja(
                id=s.id,
                sucursal_nombre=sucursal.nombre if sucursal else "—",
                caja_nombre=caja.nombre if caja else "—",
                cajero_nombre=cajero.nombre if cajero else "—",
                apertura_at=s.apertura_at.isoformat() if s.apertura_at else "",
                cierre_at=s.cierre_at.isoformat() if s.cierre_at else None,
                estado=s.estado,
                monto_apertura=s.monto_apertura or 0,
                monto_cierre_esperado=s.monto_cierre_esperado,
                monto_cierre_real=s.monto_cierre_real,
                diferencia=s.diferencia,
                ventas_count=int(stats[0]),
                total_ventas=int(stats[1]),
            ))

        return resultados
    finally:
        tenant.close()

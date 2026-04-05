"""Endpoints de dashboard — multi-tenant.

Dashboard en tiempo real para el panel administrativo.
Compatible con SQLite (strftime en lugar de extract).
"""
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, and_
from sqlalchemy.orm import Session

from crumbpos.api.dependencies import get_tenant, TenantContext
from crumbpos.db.models import (
    Venta, VentaItem, Pago, SesionCaja, Sucursal, Caja, Usuario,
)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


# ═══════════════════════════════════════════════════════════════
# SCHEMAS
# ═══════════════════════════════════════════════════════════════

class DashboardResumen(BaseModel):
    total_ventas_hoy: int
    num_transacciones: int
    ticket_promedio: int
    comparativo_ayer: float  # % change vs yesterday


class DashboardSucursalDetalle(BaseModel):
    sucursal_id: str
    sucursal_nombre: str
    ventas_hoy: int
    transacciones_hoy: int
    ticket_promedio: int
    cajero_activo: str | None
    ultima_venta: str | None  # ISO datetime


class DashboardHora(BaseModel):
    hora: int
    total: int
    count: int


class DashboardUltimaVenta(BaseModel):
    id: str
    fecha: str
    tipo_dte: int
    folio: int | None
    monto_total: int
    sucursal_nombre: str
    cajero_nombre: str


class DashboardCajaAbierta(BaseModel):
    caja_nombre: str
    sucursal_nombre: str
    cajero: str
    apertura: str  # ISO datetime
    ventas_count: int
    total_ventas: int


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def _hoy_rango():
    """Retorna (inicio_hoy, fin_hoy) como datetime."""
    hoy = date.today()
    return (
        datetime.combine(hoy, datetime.min.time()),
        datetime.combine(hoy, datetime.max.time()),
    )


def _ayer_rango():
    """Retorna (inicio_ayer, fin_ayer) como datetime."""
    ayer = date.today() - timedelta(days=1)
    return (
        datetime.combine(ayer, datetime.min.time()),
        datetime.combine(ayer, datetime.max.time()),
    )


# ═══════════════════════════════════════════════════════════════
# 1. RESUMEN DEL DÍA
# ═══════════════════════════════════════════════════════════════

@router.get("/resumen", response_model=DashboardResumen)
def dashboard_resumen(tenant: TenantContext = Depends(get_tenant)):
    """Resumen general del día: total ventas, transacciones, ticket promedio,
    y comparativo porcentual con ayer."""
    try:
        db = tenant.db
        hoy_desde, hoy_hasta = _hoy_rango()
        ayer_desde, ayer_hasta = _ayer_rango()

        # --- Hoy ---
        stats_hoy = db.query(
            func.coalesce(func.sum(Venta.monto_total), 0),
            func.count(Venta.id),
        ).filter(
            Venta.empresa_id == tenant.empresa_id,
            Venta.estado == "completada",
            Venta.fecha >= hoy_desde,
            Venta.fecha <= hoy_hasta,
        ).first()

        total_hoy = int(stats_hoy[0])
        num_hoy = int(stats_hoy[1])
        ticket_prom = int(total_hoy / num_hoy) if num_hoy > 0 else 0

        # --- Ayer ---
        stats_ayer = db.query(
            func.coalesce(func.sum(Venta.monto_total), 0),
        ).filter(
            Venta.empresa_id == tenant.empresa_id,
            Venta.estado == "completada",
            Venta.fecha >= ayer_desde,
            Venta.fecha <= ayer_hasta,
        ).first()

        total_ayer = int(stats_ayer[0])

        # Comparativo %
        if total_ayer > 0:
            comparativo = round(((total_hoy - total_ayer) / total_ayer) * 100, 1)
        else:
            comparativo = 100.0 if total_hoy > 0 else 0.0

        return DashboardResumen(
            total_ventas_hoy=total_hoy,
            num_transacciones=num_hoy,
            ticket_promedio=ticket_prom,
            comparativo_ayer=comparativo,
        )
    finally:
        tenant.close()


# ═══════════════════════════════════════════════════════════════
# 2. POR SUCURSAL (DETALLADO)
# ═══════════════════════════════════════════════════════════════

@router.get("/sucursales", response_model=list[DashboardSucursalDetalle])
def dashboard_sucursales(tenant: TenantContext = Depends(get_tenant)):
    """Estado actual por sucursal: ventas del día, cajero activo, última venta."""
    try:
        db = tenant.db
        hoy_desde, hoy_hasta = _hoy_rango()

        sucursales = db.query(Sucursal).filter(
            Sucursal.empresa_id == tenant.empresa_id,
            Sucursal.activa == True,
        ).all()

        resultados = []
        for suc in sucursales:
            # Ventas del día
            stats = db.query(
                func.coalesce(func.sum(Venta.monto_total), 0),
                func.count(Venta.id),
            ).filter(
                Venta.sucursal_id == suc.id,
                Venta.estado == "completada",
                Venta.fecha >= hoy_desde,
                Venta.fecha <= hoy_hasta,
            ).first()

            total = int(stats[0])
            count = int(stats[1])

            # Cajero activo: sesión abierta más reciente
            sesion_abierta = db.query(SesionCaja).filter(
                SesionCaja.sucursal_id == suc.id,
                SesionCaja.estado == "abierta",
            ).order_by(SesionCaja.apertura_at.desc()).first()

            cajero_activo = None
            if sesion_abierta:
                cajero = db.query(Usuario).filter(
                    Usuario.id == sesion_abierta.usuario_id,
                ).first()
                cajero_activo = cajero.nombre if cajero else None

            # Última venta
            ultima = db.query(Venta).filter(
                Venta.sucursal_id == suc.id,
                Venta.estado == "completada",
                Venta.fecha >= hoy_desde,
            ).order_by(Venta.fecha.desc()).first()

            resultados.append(DashboardSucursalDetalle(
                sucursal_id=suc.id,
                sucursal_nombre=suc.nombre,
                ventas_hoy=total,
                transacciones_hoy=count,
                ticket_promedio=int(total / count) if count > 0 else 0,
                cajero_activo=cajero_activo,
                ultima_venta=ultima.fecha.isoformat() if ultima else None,
            ))

        return resultados
    finally:
        tenant.close()


# ═══════════════════════════════════════════════════════════════
# 3. VENTAS POR HORA (GRÁFICO)
# ═══════════════════════════════════════════════════════════════

@router.get("/ventas-hora", response_model=list[DashboardHora])
def dashboard_ventas_hora(tenant: TenantContext = Depends(get_tenant)):
    """Distribución horaria de ventas del día (para gráfico).
    Retorna las 24 horas completas, incluso sin ventas."""
    try:
        db = tenant.db
        hoy_desde, hoy_hasta = _hoy_rango()

        hora_col = func.strftime('%H', Venta.fecha).label("hora")

        rows = db.query(
            hora_col,
            func.coalesce(func.sum(Venta.monto_total), 0).label("total"),
            func.count(Venta.id).label("count"),
        ).filter(
            Venta.empresa_id == tenant.empresa_id,
            Venta.estado == "completada",
            Venta.fecha >= hoy_desde,
            Venta.fecha <= hoy_hasta,
        ).group_by(hora_col).all()

        hora_map = {}
        for row in rows:
            h = int(row.hora)
            hora_map[h] = {"total": int(row.total), "count": int(row.count)}

        return [
            DashboardHora(
                hora=h,
                total=hora_map.get(h, {}).get("total", 0),
                count=hora_map.get(h, {}).get("count", 0),
            )
            for h in range(24)
        ]
    finally:
        tenant.close()


# ═══════════════════════════════════════════════════════════════
# 4. ÚLTIMAS VENTAS
# ═══════════════════════════════════════════════════════════════

@router.get("/ultimas-ventas", response_model=list[DashboardUltimaVenta])
def dashboard_ultimas_ventas(
    limit: int = Query(10, le=50),
    tenant: TenantContext = Depends(get_tenant),
):
    """Últimas N transacciones completadas."""
    try:
        db = tenant.db

        ventas = db.query(Venta).filter(
            Venta.empresa_id == tenant.empresa_id,
            Venta.estado == "completada",
        ).order_by(Venta.fecha.desc()).limit(limit).all()

        resultados = []
        for v in ventas:
            sucursal = db.query(Sucursal).filter(Sucursal.id == v.sucursal_id).first()
            cajero = db.query(Usuario).filter(Usuario.id == v.usuario_id).first()

            resultados.append(DashboardUltimaVenta(
                id=v.id,
                fecha=v.fecha.isoformat() if v.fecha else "",
                tipo_dte=v.tipo_dte,
                folio=v.folio,
                monto_total=v.monto_total,
                sucursal_nombre=sucursal.nombre if sucursal else "—",
                cajero_nombre=cajero.nombre if cajero else "—",
            ))

        return resultados
    finally:
        tenant.close()


# ═══════════════════════════════════════════════════════════════
# 5. CAJAS ABIERTAS
# ═══════════════════════════════════════════════════════════════

@router.get("/cajas-abiertas", response_model=list[DashboardCajaAbierta])
def dashboard_cajas_abiertas(tenant: TenantContext = Depends(get_tenant)):
    """Sesiones de caja actualmente abiertas con totales acumulados."""
    try:
        db = tenant.db

        sesiones = db.query(SesionCaja).filter(
            SesionCaja.estado == "abierta",
        ).order_by(SesionCaja.apertura_at.desc()).all()

        resultados = []
        for s in sesiones:
            # Verify session belongs to this empresa via sucursal
            sucursal = db.query(Sucursal).filter(
                Sucursal.id == s.sucursal_id,
                Sucursal.empresa_id == tenant.empresa_id,
            ).first()
            if not sucursal:
                continue  # skip sessions from other empresas

            caja = db.query(Caja).filter(Caja.id == s.caja_id).first()
            cajero = db.query(Usuario).filter(Usuario.id == s.usuario_id).first()

            stats = db.query(
                func.count(Venta.id),
                func.coalesce(func.sum(Venta.monto_total), 0),
            ).filter(
                Venta.sesion_caja_id == s.id,
                Venta.estado == "completada",
            ).first()

            resultados.append(DashboardCajaAbierta(
                caja_nombre=caja.nombre if caja else "—",
                sucursal_nombre=sucursal.nombre,
                cajero=cajero.nombre if cajero else "—",
                apertura=s.apertura_at.isoformat() if s.apertura_at else "",
                ventas_count=int(stats[0]),
                total_ventas=int(stats[1]),
            ))

        return resultados
    finally:
        tenant.close()

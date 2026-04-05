"""Endpoints de ventas y dashboard — multi-tenant."""
from datetime import datetime, date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, and_

from crumbpos.db.models import (
    Venta, VentaItem, Pago, Articulo, Sucursal,
)
from crumbpos.api.dependencies import get_tenant, TenantContext
from crumbpos.api.schemas import VentaCreate, VentaOut, DashboardSucursal

router = APIRouter(prefix="/api/ventas", tags=["ventas"])


@router.post("/", response_model=VentaOut, status_code=201)
def crear_venta(
    body: VentaCreate,
    tenant: TenantContext = Depends(get_tenant),
):
    """Registra una venta desde el POS."""
    try:
        db = tenant.db
        es_boleta = body.tipo_dte in (39, 41)
        es_exenta = body.tipo_dte in (34, 41)

        venta = Venta(
            empresa_id=tenant.empresa_id,
            sucursal_id=body.sucursal_id,
            sesion_caja_id=body.sesion_caja_id,
            usuario_id=tenant.user.id,
            tipo_dte=body.tipo_dte,
            receptor_rut=body.receptor_rut,
            receptor_razon=body.receptor_razon,
            receptor_giro=body.receptor_giro,
            receptor_dir=body.receptor_dir,
            receptor_comuna=body.receptor_comuna,
        )
        db.add(venta)

        total_afecto = 0
        total_exento = 0

        for item_in in body.items:
            art = db.query(Articulo).filter(Articulo.id == item_in.articulo_id).first()
            nombre = art.nombre_corto or art.nombre if art else "Artículo"

            monto = round(item_in.cantidad * item_in.precio_unitario)
            desc_monto = None
            if item_in.descuento_pct:
                desc_monto = round(monto * item_in.descuento_pct / 100)
                monto -= desc_monto

            vi = VentaItem(
                venta_id=venta.id,
                articulo_id=item_in.articulo_id,
                nombre=nombre,
                cantidad=item_in.cantidad,
                precio_unitario=item_in.precio_unitario,
                descuento_pct=item_in.descuento_pct,
                descuento_monto=desc_monto,
                monto_linea=monto,
                es_exento=item_in.es_exento,
            )
            db.add(vi)

            if item_in.es_exento or es_exenta:
                total_exento += monto
            else:
                total_afecto += monto

        if es_exenta:
            venta.monto_exento = total_exento
            venta.monto_total = total_exento
        elif es_boleta:
            if total_exento > 0:
                venta.monto_exento = total_exento
            if total_afecto > 0:
                venta.monto_neto = round(total_afecto / 1.19)
                venta.iva = total_afecto - venta.monto_neto
            venta.monto_total = total_afecto + total_exento
        else:
            if total_afecto > 0:
                venta.monto_neto = total_afecto
                venta.iva = (total_afecto * 19 + 50) // 100
            if total_exento > 0:
                venta.monto_exento = total_exento
            venta.monto_total = (venta.monto_neto or 0) + (venta.iva or 0) + (venta.monto_exento or 0)

        for pago_in in body.pagos:
            pago = Pago(
                venta_id=venta.id,
                medio=pago_in.medio,
                monto=pago_in.monto,
                referencia=pago_in.referencia,
            )
            db.add(pago)

        db.commit()
        db.refresh(venta)
        return venta
    finally:
        tenant.close()


@router.get("/", response_model=list[VentaOut])
def listar_ventas(
    sucursal_id: str | None = None,
    fecha_desde: date | None = None,
    fecha_hasta: date | None = None,
    tipo_dte: int | None = None,
    limit: int = Query(50, le=200),
    tenant: TenantContext = Depends(get_tenant),
):
    try:
        db = tenant.db
        query = db.query(Venta).filter(Venta.empresa_id == tenant.empresa_id)
        if sucursal_id:
            query = query.filter(Venta.sucursal_id == sucursal_id)
        if fecha_desde:
            query = query.filter(Venta.fecha >= datetime.combine(fecha_desde, datetime.min.time()))
        if fecha_hasta:
            query = query.filter(Venta.fecha <= datetime.combine(fecha_hasta, datetime.max.time()))
        if tipo_dte:
            query = query.filter(Venta.tipo_dte == tipo_dte)

        return query.order_by(Venta.fecha.desc()).limit(limit).all()
    finally:
        tenant.close()


@router.get("/{venta_id}", response_model=VentaOut)
def detalle_venta(
    venta_id: str,
    tenant: TenantContext = Depends(get_tenant),
):
    try:
        venta = tenant.db.query(Venta).filter(
            Venta.id == venta_id, Venta.empresa_id == tenant.empresa_id,
        ).first()
        if not venta:
            raise HTTPException(404, "Venta no encontrada")
        return venta
    finally:
        tenant.close()


@router.get("/dashboard/hoy", response_model=list[DashboardSucursal])
def dashboard_hoy(tenant: TenantContext = Depends(get_tenant)):
    """Resumen de ventas del día por sucursal."""
    try:
        db = tenant.db
        hoy_inicio = datetime.combine(date.today(), datetime.min.time())
        hoy_fin = datetime.combine(date.today(), datetime.max.time())

        sucursales = db.query(Sucursal).filter(
            Sucursal.empresa_id == tenant.empresa_id,
            Sucursal.activa == True,
        ).all()

        resultados = []
        for suc in sucursales:
            stats = db.query(
                func.coalesce(func.sum(Venta.monto_total), 0),
                func.count(Venta.id),
            ).filter(
                Venta.sucursal_id == suc.id,
                Venta.fecha >= hoy_inicio,
                Venta.fecha <= hoy_fin,
                Venta.estado == "completada",
            ).first()

            total, count = stats
            resultados.append(DashboardSucursal(
                sucursal_id=suc.id,
                sucursal_nombre=suc.nombre,
                ventas_hoy=int(total),
                transacciones_hoy=int(count),
                ticket_promedio=int(total / count) if count > 0 else 0,
            ))

        return resultados
    finally:
        tenant.close()

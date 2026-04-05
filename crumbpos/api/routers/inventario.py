"""Endpoints de inventario — bodegas, stock, movimientos."""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func

from crumbpos.db.models import Bodega, Stock, MovimientoStock, Articulo, Sucursal
from crumbpos.api.dependencies import get_tenant, TenantContext

router = APIRouter(prefix="/api/inventario", tags=["inventario"])


# ═══════════════════════════════════════════════════════════════
# SCHEMAS
# ═══════════════════════════════════════════════════════════════

class BodegaCreate(BaseModel):
    sucursal_id: str
    nombre: str
    tipo: str = "venta"
    es_default: bool = False


class BodegaUpdate(BaseModel):
    nombre: Optional[str] = None
    tipo: Optional[str] = None
    es_default: Optional[bool] = None


class BodegaOut(BaseModel):
    id: str
    sucursal_id: str
    nombre: str
    tipo: str
    es_default: bool

    class Config:
        from_attributes = True


class StockOut(BaseModel):
    articulo_id: str
    bodega_id: str
    cantidad: float
    stock_minimo: float
    articulo_nombre: str
    bodega_nombre: str


class AjusteStockIn(BaseModel):
    articulo_id: str
    bodega_id: str
    cantidad_nueva: float
    motivo: str


class TraspasoItem(BaseModel):
    articulo_id: str
    cantidad: float


class TraspasoIn(BaseModel):
    bodega_origen_id: str
    bodega_destino_id: str
    items: list[TraspasoItem]


class MovimientoOut(BaseModel):
    id: str
    bodega_id: str
    articulo_id: str
    tipo: str
    cantidad: float
    referencia_id: Optional[str] = None
    referencia_tipo: Optional[str] = None
    usuario_id: Optional[str] = None
    fecha: str


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def _validate_bodega(db, bodega_id: str, empresa_id: str) -> Bodega:
    """Valida que la bodega pertenece a la empresa via Sucursal."""
    bodega = db.query(Bodega).join(Sucursal).filter(
        Bodega.id == bodega_id,
        Sucursal.empresa_id == empresa_id,
    ).first()
    if not bodega:
        raise HTTPException(404, f"Bodega {bodega_id} no encontrada")
    return bodega


# ═══════════════════════════════════════════════════════════════
# BODEGAS
# ═══════════════════════════════════════════════════════════════

@router.get("/bodegas", response_model=list[BodegaOut])
def listar_bodegas(
    sucursal_id: Optional[str] = Query(None),
    tenant: TenantContext = Depends(get_tenant),
):
    """Lista bodegas de la empresa, opcionalmente filtradas por sucursal."""
    try:
        db = tenant.db
        q = db.query(Bodega).join(Sucursal).filter(
            Sucursal.empresa_id == tenant.empresa_id,
        )
        if sucursal_id:
            q = q.filter(Bodega.sucursal_id == sucursal_id)
        return q.all()
    finally:
        tenant.close()


@router.post("/bodegas", response_model=BodegaOut, status_code=201)
def crear_bodega(
    body: BodegaCreate,
    tenant: TenantContext = Depends(get_tenant),
):
    """Crea una bodega. Valida que la sucursal pertenezca a la empresa."""
    try:
        db = tenant.db

        # Validar sucursal pertenece a empresa
        sucursal = db.query(Sucursal).filter(
            Sucursal.id == body.sucursal_id,
            Sucursal.empresa_id == tenant.empresa_id,
        ).first()
        if not sucursal:
            raise HTTPException(404, "Sucursal no encontrada o no pertenece a la empresa")

        # Si es default, quitar default de otras bodegas de la misma sucursal
        if body.es_default:
            db.query(Bodega).filter(
                Bodega.sucursal_id == body.sucursal_id,
                Bodega.es_default == True,
            ).update({"es_default": False})

        bodega = Bodega(
            sucursal_id=body.sucursal_id,
            nombre=body.nombre,
            tipo=body.tipo,
            es_default=body.es_default,
        )
        db.add(bodega)
        db.commit()
        db.refresh(bodega)
        return bodega
    finally:
        tenant.close()


@router.put("/bodegas/{bodega_id}", response_model=BodegaOut)
def actualizar_bodega(
    bodega_id: str,
    body: BodegaUpdate,
    tenant: TenantContext = Depends(get_tenant),
):
    """Actualiza una bodega."""
    try:
        db = tenant.db
        bodega = _validate_bodega(db, bodega_id, tenant.empresa_id)

        if body.nombre is not None:
            bodega.nombre = body.nombre
        if body.tipo is not None:
            bodega.tipo = body.tipo
        if body.es_default is not None:
            if body.es_default:
                # Quitar default de otras bodegas de la misma sucursal
                db.query(Bodega).filter(
                    Bodega.sucursal_id == bodega.sucursal_id,
                    Bodega.es_default == True,
                    Bodega.id != bodega_id,
                ).update({"es_default": False})
            bodega.es_default = body.es_default

        db.commit()
        db.refresh(bodega)
        return bodega
    finally:
        tenant.close()


# ═══════════════════════════════════════════════════════════════
# STOCK
# ═══════════════════════════════════════════════════════════════

@router.get("/stock", response_model=list[StockOut])
def listar_stock(
    bodega_id: Optional[str] = Query(None),
    sucursal_id: Optional[str] = Query(None),
    q: Optional[str] = Query(None, description="Buscar por nombre de articulo"),
    tenant: TenantContext = Depends(get_tenant),
):
    """Niveles de stock. Filtra por bodega, sucursal y/o texto."""
    try:
        db = tenant.db
        query = (
            db.query(
                Stock.articulo_id,
                Stock.bodega_id,
                Stock.cantidad,
                Stock.stock_minimo,
                Articulo.nombre.label("articulo_nombre"),
                Bodega.nombre.label("bodega_nombre"),
            )
            .join(Articulo, Stock.articulo_id == Articulo.id)
            .join(Bodega, Stock.bodega_id == Bodega.id)
            .join(Sucursal, Bodega.sucursal_id == Sucursal.id)
            .filter(Sucursal.empresa_id == tenant.empresa_id)
        )

        if bodega_id:
            query = query.filter(Stock.bodega_id == bodega_id)
        if sucursal_id:
            query = query.filter(Bodega.sucursal_id == sucursal_id)
        if q:
            query = query.filter(Articulo.nombre.ilike(f"%{q}%"))

        rows = query.all()
        return [
            StockOut(
                articulo_id=r.articulo_id,
                bodega_id=r.bodega_id,
                cantidad=r.cantidad,
                stock_minimo=r.stock_minimo,
                articulo_nombre=r.articulo_nombre,
                bodega_nombre=r.bodega_nombre,
            )
            for r in rows
        ]
    finally:
        tenant.close()


@router.get("/stock/{articulo_id}", response_model=list[StockOut])
def stock_por_articulo(
    articulo_id: str,
    tenant: TenantContext = Depends(get_tenant),
):
    """Stock de un articulo en todas las bodegas de la empresa."""
    try:
        db = tenant.db
        rows = (
            db.query(
                Stock.articulo_id,
                Stock.bodega_id,
                Stock.cantidad,
                Stock.stock_minimo,
                Articulo.nombre.label("articulo_nombre"),
                Bodega.nombre.label("bodega_nombre"),
            )
            .join(Articulo, Stock.articulo_id == Articulo.id)
            .join(Bodega, Stock.bodega_id == Bodega.id)
            .join(Sucursal, Bodega.sucursal_id == Sucursal.id)
            .filter(
                Sucursal.empresa_id == tenant.empresa_id,
                Stock.articulo_id == articulo_id,
            )
            .all()
        )
        return [
            StockOut(
                articulo_id=r.articulo_id,
                bodega_id=r.bodega_id,
                cantidad=r.cantidad,
                stock_minimo=r.stock_minimo,
                articulo_nombre=r.articulo_nombre,
                bodega_nombre=r.bodega_nombre,
            )
            for r in rows
        ]
    finally:
        tenant.close()


@router.post("/ajuste", status_code=200)
def ajustar_stock(
    body: AjusteStockIn,
    tenant: TenantContext = Depends(get_tenant),
):
    """Ajuste manual de stock. Registra movimiento tipo 'ajuste'."""
    try:
        db = tenant.db

        # Validar bodega pertenece a empresa
        _validate_bodega(db, body.bodega_id, tenant.empresa_id)

        # Validar articulo pertenece a empresa
        art = db.query(Articulo).filter(
            Articulo.id == body.articulo_id,
            Articulo.empresa_id == tenant.empresa_id,
        ).first()
        if not art:
            raise HTTPException(404, "Articulo no encontrado")

        # Get or create stock record
        stock = db.query(Stock).filter_by(
            articulo_id=body.articulo_id,
            bodega_id=body.bodega_id,
        ).first()
        if not stock:
            stock = Stock(
                articulo_id=body.articulo_id,
                bodega_id=body.bodega_id,
                cantidad=0,
            )
            db.add(stock)

        diferencia = body.cantidad_nueva - stock.cantidad
        stock.cantidad = body.cantidad_nueva

        # Record movement
        mov = MovimientoStock(
            bodega_id=body.bodega_id,
            articulo_id=body.articulo_id,
            tipo="ajuste",
            cantidad=diferencia,
            referencia_tipo=body.motivo,
            usuario_id=tenant.user.id,
        )
        db.add(mov)
        db.commit()

        return {
            "ok": True,
            "articulo_id": body.articulo_id,
            "bodega_id": body.bodega_id,
            "cantidad_anterior": body.cantidad_nueva - diferencia,
            "cantidad_nueva": body.cantidad_nueva,
            "diferencia": diferencia,
        }
    finally:
        tenant.close()


@router.post("/traspaso", status_code=200)
def traspasar_stock(
    body: TraspasoIn,
    tenant: TenantContext = Depends(get_tenant),
):
    """Traspaso de stock entre bodegas. Crea movimientos en origen y destino."""
    try:
        db = tenant.db

        # Validar ambas bodegas pertenecen a la empresa
        bodega_origen = _validate_bodega(db, body.bodega_origen_id, tenant.empresa_id)
        bodega_destino = _validate_bodega(db, body.bodega_destino_id, tenant.empresa_id)

        if body.bodega_origen_id == body.bodega_destino_id:
            raise HTTPException(400, "Bodega origen y destino deben ser distintas")

        resultados = []

        for item in body.items:
            # Validar articulo
            art = db.query(Articulo).filter(
                Articulo.id == item.articulo_id,
                Articulo.empresa_id == tenant.empresa_id,
            ).first()
            if not art:
                raise HTTPException(404, f"Articulo {item.articulo_id} no encontrado")

            if item.cantidad <= 0:
                raise HTTPException(400, "Cantidad debe ser mayor a 0")

            # Stock en origen
            stock_origen = db.query(Stock).filter_by(
                articulo_id=item.articulo_id,
                bodega_id=body.bodega_origen_id,
            ).first()
            if not stock_origen or stock_origen.cantidad < item.cantidad:
                raise HTTPException(
                    400,
                    f"Stock insuficiente de {art.nombre} en bodega origen "
                    f"(disponible: {stock_origen.cantidad if stock_origen else 0})",
                )

            # Decrementar origen
            stock_origen.cantidad -= item.cantidad

            # Incrementar destino (get or create)
            stock_destino = db.query(Stock).filter_by(
                articulo_id=item.articulo_id,
                bodega_id=body.bodega_destino_id,
            ).first()
            if not stock_destino:
                stock_destino = Stock(
                    articulo_id=item.articulo_id,
                    bodega_id=body.bodega_destino_id,
                    cantidad=0,
                )
                db.add(stock_destino)
            stock_destino.cantidad += item.cantidad

            # Movimiento salida (negativo)
            mov_salida = MovimientoStock(
                bodega_id=body.bodega_origen_id,
                articulo_id=item.articulo_id,
                tipo="traspaso",
                cantidad=-item.cantidad,
                referencia_id=body.bodega_destino_id,
                referencia_tipo="traspaso_salida",
                usuario_id=tenant.user.id,
            )
            db.add(mov_salida)

            # Movimiento entrada (positivo)
            mov_entrada = MovimientoStock(
                bodega_id=body.bodega_destino_id,
                articulo_id=item.articulo_id,
                tipo="traspaso",
                cantidad=item.cantidad,
                referencia_id=body.bodega_origen_id,
                referencia_tipo="traspaso_entrada",
                usuario_id=tenant.user.id,
            )
            db.add(mov_entrada)

            resultados.append({
                "articulo_id": item.articulo_id,
                "articulo_nombre": art.nombre,
                "cantidad": item.cantidad,
            })

        db.commit()

        return {
            "ok": True,
            "bodega_origen_id": body.bodega_origen_id,
            "bodega_destino_id": body.bodega_destino_id,
            "items": resultados,
        }
    finally:
        tenant.close()


# ═══════════════════════════════════════════════════════════════
# MOVIMIENTOS
# ═══════════════════════════════════════════════════════════════

@router.get("/movimientos", response_model=list[MovimientoOut])
def listar_movimientos(
    bodega_id: Optional[str] = Query(None),
    articulo_id: Optional[str] = Query(None),
    tipo: Optional[str] = Query(None),
    fecha_desde: Optional[str] = Query(None, description="YYYY-MM-DD"),
    fecha_hasta: Optional[str] = Query(None, description="YYYY-MM-DD"),
    tenant: TenantContext = Depends(get_tenant),
):
    """Historial de movimientos de stock."""
    try:
        db = tenant.db
        query = (
            db.query(MovimientoStock)
            .join(Bodega, MovimientoStock.bodega_id == Bodega.id)
            .join(Sucursal, Bodega.sucursal_id == Sucursal.id)
            .filter(Sucursal.empresa_id == tenant.empresa_id)
        )

        if bodega_id:
            query = query.filter(MovimientoStock.bodega_id == bodega_id)
        if articulo_id:
            query = query.filter(MovimientoStock.articulo_id == articulo_id)
        if tipo:
            query = query.filter(MovimientoStock.tipo == tipo)
        if fecha_desde:
            query = query.filter(
                MovimientoStock.fecha >= datetime.strptime(fecha_desde, "%Y-%m-%d")
            )
        if fecha_hasta:
            dt_hasta = datetime.strptime(fecha_hasta, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59
            )
            query = query.filter(MovimientoStock.fecha <= dt_hasta)

        query = query.order_by(MovimientoStock.fecha.desc()).limit(500)
        rows = query.all()

        return [
            MovimientoOut(
                id=m.id,
                bodega_id=m.bodega_id,
                articulo_id=m.articulo_id,
                tipo=m.tipo,
                cantidad=m.cantidad,
                referencia_id=m.referencia_id,
                referencia_tipo=m.referencia_tipo,
                usuario_id=m.usuario_id,
                fecha=m.fecha.isoformat() if m.fecha else "",
            )
            for m in rows
        ]
    finally:
        tenant.close()

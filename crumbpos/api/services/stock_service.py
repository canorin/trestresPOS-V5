"""Servicio de stock — helpers reutilizables desde otros modulos (ventas, compras)."""
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from crumbpos.db.models import Bodega, Stock, MovimientoStock, Articulo, Sucursal

logger = logging.getLogger(__name__)


def descontar_stock_venta(
    db: Session,
    sucursal_id: str,
    items: list,
    usuario_id: str,
):
    """Descuenta stock al registrar una venta.

    Busca la bodega default de la sucursal, y para cada item
    crea un MovimientoStock(tipo='venta') y actualiza Stock.
    Si no hay bodega default o stock, silently skips (stock tracking is optional).

    Args:
        db: SQLAlchemy session (already open).
        sucursal_id: ID de la sucursal donde se realizo la venta.
        items: Lista de objetos con .articulo_id y .cantidad.
        usuario_id: ID del usuario que registra la venta.
    """
    try:
        bodega = db.query(Bodega).filter(
            Bodega.sucursal_id == sucursal_id,
            Bodega.es_default == True,
        ).first()

        if not bodega:
            # No hay bodega default — stock tracking no configurado para esta sucursal
            return

        for item in items:
            if not item.articulo_id:
                continue

            stock = db.query(Stock).filter_by(
                articulo_id=item.articulo_id,
                bodega_id=bodega.id,
            ).first()

            if not stock:
                # No hay registro de stock para este articulo — skip
                continue

            stock.cantidad -= item.cantidad

            mov = MovimientoStock(
                bodega_id=bodega.id,
                articulo_id=item.articulo_id,
                tipo="venta",
                cantidad=-item.cantidad,
                usuario_id=usuario_id,
            )
            db.add(mov)

    except Exception:
        # Stock tracking debe ser fault-tolerant: no falla la venta
        logger.exception("Error al descontar stock de venta (sucursal=%s)", sucursal_id)


def incrementar_stock_compra(
    db: Session,
    bodega_id: str,
    articulo_id: str,
    cantidad: float,
    usuario_id: str,
    referencia_id: str | None = None,
):
    """Incrementa stock por compra.

    Args:
        db: SQLAlchemy session.
        bodega_id: Bodega donde se recibe la mercaderia.
        articulo_id: Articulo recibido.
        cantidad: Cantidad recibida (positiva).
        usuario_id: Usuario que registra.
        referencia_id: ID de la orden de compra u otro documento.
    """
    stock = db.query(Stock).filter_by(
        articulo_id=articulo_id,
        bodega_id=bodega_id,
    ).first()

    if not stock:
        stock = Stock(
            articulo_id=articulo_id,
            bodega_id=bodega_id,
            cantidad=0,
        )
        db.add(stock)

    stock.cantidad += cantidad

    mov = MovimientoStock(
        bodega_id=bodega_id,
        articulo_id=articulo_id,
        tipo="compra",
        cantidad=cantidad,
        referencia_id=referencia_id,
        referencia_tipo="compra",
        usuario_id=usuario_id,
    )
    db.add(mov)


def get_stock_alertas(db: Session, empresa_id: str) -> list[dict]:
    """Returns articles with stock below minimum threshold.

    Args:
        db: SQLAlchemy session.
        empresa_id: ID de la empresa.

    Returns:
        Lista de dicts con info del articulo y stock actual vs minimo.
    """
    rows = (
        db.query(
            Stock.articulo_id,
            Stock.bodega_id,
            Stock.cantidad,
            Stock.stock_minimo,
            Articulo.nombre.label("articulo_nombre"),
            Articulo.sku,
            Bodega.nombre.label("bodega_nombre"),
        )
        .join(Articulo, Stock.articulo_id == Articulo.id)
        .join(Bodega, Stock.bodega_id == Bodega.id)
        .join(Sucursal, Bodega.sucursal_id == Sucursal.id)
        .filter(
            Sucursal.empresa_id == empresa_id,
            Stock.stock_minimo > 0,
            Stock.cantidad <= Stock.stock_minimo,
        )
        .all()
    )

    return [
        {
            "articulo_id": r.articulo_id,
            "articulo_nombre": r.articulo_nombre,
            "sku": r.sku,
            "bodega_id": r.bodega_id,
            "bodega_nombre": r.bodega_nombre,
            "cantidad": r.cantidad,
            "stock_minimo": r.stock_minimo,
        }
        for r in rows
    ]

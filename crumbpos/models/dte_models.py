"""Modelos de datos para DTEs."""
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional


def _round_sii(value) -> int:
    """Redondeo HALF-UP a entero, estándar SII Chile."""
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass
class ItemDetalle:
    """Línea de detalle de un DTE."""
    nro_linea: int
    nombre: str
    cantidad: Optional[float] = None
    unidad_medida: Optional[str] = None
    precio_unitario: Optional[int] = None
    descuento_pct: Optional[float] = None
    descuento_monto: Optional[int] = None
    monto_item: Optional[int] = None
    exento: bool = False


@dataclass
class Referencia:
    """Referencia a otro documento."""
    nro_linea: int
    tipo_doc_ref: str  # "33", "61", "SET", etc.
    folio_ref: str
    fecha_ref: Optional[str] = None
    razon_ref: Optional[str] = None
    codigo_ref: Optional[int | str] = None  # 1=anula, 2=corrige texto, 3=corrige monto, "SET"=set pruebas


@dataclass
class DescuentoGlobal:
    """Descuento o recargo global."""
    nro_linea: int
    tipo: str  # "D" descuento, "R" recargo
    descripcion: str
    tipo_valor: str  # "%" porcentaje, "$" monto
    valor: float
    indicador_exento: Optional[int] = None  # 1=exento, None=afecto


@dataclass
class DTE:
    """Documento Tributario Electrónico."""
    tipo_dte: int
    folio: int
    fecha_emision: str  # YYYY-MM-DD
    emisor: dict
    receptor: dict
    items: list[ItemDetalle] = field(default_factory=list)
    referencias: list[Referencia] = field(default_factory=list)
    descuentos_globales: list[DescuentoGlobal] = field(default_factory=list)
    # Totales calculados
    monto_neto: Optional[int] = None
    monto_exento: Optional[int] = None
    tasa_iva: Optional[int] = None
    iva: Optional[int] = None
    monto_total: Optional[int] = None
    # Guía de despacho
    tipo_traslado: Optional[int] = None  # 1=op.const.venta, 2=ventas, 5=traslado interno, 6=otros
    tipo_despacho: Optional[int] = None  # 1=por cuenta del receptor, 2=emisor a instalaciones del cliente, 3=emisor a otras instalaciones del emisor
    # Indicadores
    indicador_servicio: Optional[int] = None  # 3=factura de servicios
    # Boleta
    indicador_montos_brutos: Optional[int] = None  # 1=montos brutos (IVA incluido)
    # Condiciones de pago
    fma_pago: Optional[int] = None  # 1=Contado, 2=Crédito, 3=Sin costo
    fecha_vencimiento: Optional[str] = None  # YYYY-MM-DD fecha vencimiento pago
    # MntPagos (para crédito)
    fecha_pago: Optional[str] = None  # YYYY-MM-DD
    monto_pago: Optional[int] = None  # Monto a pagar en esa fecha

    def calcular_totales(self):
        """Calcula los montos totales del documento."""
        es_boleta = self.tipo_dte in (39, 41)
        es_exenta = self.tipo_dte in (34, 41)

        # Calcular monto de cada item
        for item in self.items:
            if item.cantidad is not None and item.precio_unitario is not None:
                monto = _round_sii(item.cantidad * item.precio_unitario)
                if item.descuento_pct:
                    desc = _round_sii(monto * item.descuento_pct / 100)
                    item.descuento_monto = desc
                    monto = monto - desc
                item.monto_item = monto
            elif item.monto_item is None and item.precio_unitario is not None:
                item.monto_item = item.precio_unitario

        if es_exenta:
            # Factura exenta: todo es exento
            self.monto_exento = sum(i.monto_item for i in self.items if i.monto_item)
            self.monto_total = self.monto_exento
            self.monto_neto = None
            self.tasa_iva = None
            self.iva = None
            return

        # Separar items afectos y exentos
        suma_afecto = sum(i.monto_item for i in self.items if i.monto_item and not i.exento)
        suma_exento = sum(i.monto_item for i in self.items if i.monto_item and i.exento)

        # Aplicar descuentos y recargos globales
        for dr in self.descuentos_globales:
            if dr.indicador_exento == 1:
                target = suma_exento
            else:
                target = suma_afecto

            if dr.tipo_valor == "%":
                monto_dr = _round_sii(target * dr.valor / 100)
            else:
                monto_dr = _round_sii(dr.valor)

            if dr.tipo == "D":
                if dr.indicador_exento == 1:
                    suma_exento -= monto_dr
                else:
                    suma_afecto -= monto_dr
            elif dr.tipo == "R":
                if dr.indicador_exento == 1:
                    suma_exento += monto_dr
                else:
                    suma_afecto += monto_dr

        if es_boleta:
            # Boleta: precios incluyen IVA
            self.indicador_montos_brutos = 1
            if suma_exento > 0:
                self.monto_exento = suma_exento
            if suma_afecto > 0:
                self.monto_neto = _round_sii(suma_afecto / 1.19)
                self.iva = suma_afecto - self.monto_neto
                self.tasa_iva = 19
            self.monto_total = suma_afecto + suma_exento
        else:
            # Factura/NC/ND: precios netos
            if suma_afecto > 0:
                self.monto_neto = suma_afecto
                self.tasa_iva = 19
                self.iva = _round_sii(suma_afecto * 19 / 100)
            if suma_exento > 0:
                self.monto_exento = suma_exento
            self.monto_total = (self.monto_neto or 0) + (self.iva or 0) + (self.monto_exento or 0)

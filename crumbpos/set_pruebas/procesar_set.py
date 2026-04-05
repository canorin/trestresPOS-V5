"""
Procesador del Set de Pruebas del SII para certificación.

Genera todos los XMLs necesarios para pasar la certificación:
- Set Básico (4752464): Facturas + NC + ND
- Set Factura Exenta (4752469): Facturas exentas + NC + ND
- Set Boleta (39): Boletas electrónicas
- Set Guía de Despacho (4752467): Guías
- Set Libro de Ventas (4752465)
- Set Libro de Compras (4752466)
- Set Libro de Guías (4752468)
"""
import os
import sys
from datetime import datetime
from pathlib import Path

# Agregar el directorio raíz al path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from crumbpos.config import settings
from crumbpos.core.caf.caf_manager import CAFManager
from crumbpos.core.dte.generador_xml import (
    generar_documento_xml,
    generar_dte_xml,
    generar_envio_dte,
    generar_envio_boleta,
    xml_to_string,
)
from crumbpos.models.dte_models import DTE, ItemDetalle, Referencia, DescuentoGlobal


FECHA_EMISION = datetime.now().strftime("%Y-%m-%d")
TIMESTAMP = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

# Datos de TRESTRES PUBLICIDAD SPA (empresa de certificación original)
EMISOR = {
    "RUTEmisor": "77051056-2",
    "RznSoc": "TRESTRES PUBLICIDAD SPA",
    "GiroEmis": "PASTELERIA Y PANADERIA",
    "Acteco": 472105,
    "DirOrigen": "CAMINO DEL ALBA 11969 LT",
    "CmnaOrigen": "LAS CONDES",
    "CiudadOrigen": "SANTIAGO",
}
RECEPTOR = {
    "RUTRecep": "77051056-2",
    "RznSocRecep": "TRESTRES PUBLICIDAD SPA",
    "GiroRecep": "PASTELERIA Y PANADERIA",
    "DirRecep": "CAMINO DEL ALBA 11969 LT",
    "CmnaRecep": "LAS CONDES",
    "CiudadRecep": "SANTIAGO",
}
FECHA_RESOLUCION = "2026-03-26"
NUMERO_RESOLUCION = 0
RUT_FIRMANTE = "17586255-2"


def crear_caso_4752464_1(folio: int) -> DTE:
    """CASO 1: Factura electrónica simple."""
    dte = DTE(
        tipo_dte=33,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Cajon AFECTO", cantidad=185, precio_unitario=4469),
            ItemDetalle(nro_linea=2, nombre="Relleno AFECTO", cantidad=77, precio_unitario=7468),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4752464-1",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4752464_2(folio: int) -> DTE:
    """CASO 2: Factura electrónica con descuentos por ítem."""
    dte = DTE(
        tipo_dte=33,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=RECEPTOR,
        items=[
            ItemDetalle(
                nro_linea=1, nombre="Panuelo AFECTO",
                cantidad=959, precio_unitario=7380, descuento_pct=12,
            ),
            ItemDetalle(
                nro_linea=2, nombre="ITEM 2 AFECTO",
                cantidad=910, precio_unitario=6427, descuento_pct=30,
            ),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4752464-2",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4752464_3(folio: int) -> DTE:
    """CASO 3: Factura electrónica con ítem exento."""
    dte = DTE(
        tipo_dte=33,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Pintura B&W AFECTO", cantidad=92, precio_unitario=8492),
            ItemDetalle(nro_linea=2, nombre="ITEM 2 AFECTO", cantidad=270, precio_unitario=4555),
            ItemDetalle(
                nro_linea=3, nombre="ITEM 3 SERVICIO EXENTO",
                cantidad=1, precio_unitario=35516, exento=True,
            ),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4752464-3",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4752464_4(folio: int) -> DTE:
    """CASO 4: Factura electrónica con descuento global."""
    dte = DTE(
        tipo_dte=33,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="ITEM 1 AFECTO", cantidad=541, precio_unitario=7539),
            ItemDetalle(nro_linea=2, nombre="ITEM 2 AFECTO", cantidad=229, precio_unitario=9416),
            ItemDetalle(
                nro_linea=3, nombre="ITEM 3 SERVICIO EXENTO",
                cantidad=2, precio_unitario=6858, exento=True,
            ),
        ],
        descuentos_globales=[
            DescuentoGlobal(
                nro_linea=1,
                tipo="D",
                descripcion="Descuento global items afectos 28%",
                tipo_valor="%",
                valor=28,
            ),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4752464-4",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4752464_5(folio: int, folio_ref_caso1: int) -> DTE:
    """CASO 5: NC - Corrige giro del receptor (ref caso 1).
    CodRef=2 (corrige texto): NO debe llevar montos."""
    dte = DTE(
        tipo_dte=61,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="CORRIGE GIRO DEL RECEPTOR", cantidad=0, precio_unitario=0),
        ],
        monto_neto=0,
        tasa_iva=19,
        iva=0,
        monto_total=0,
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4752464-5",
            ),
            Referencia(
                nro_linea=2,
                tipo_doc_ref="33",
                folio_ref=str(folio_ref_caso1),
                fecha_ref=FECHA_EMISION,
                codigo_ref=2,
                razon_ref="CORRIGE GIRO DEL RECEPTOR",
            ),
        ],
    )
    return dte


def crear_caso_4752464_6(folio: int, folio_ref_caso2: int) -> DTE:
    """CASO 6: NC - Devolución de mercaderías (ref caso 2)."""
    # Devolución parcial: precios del caso 2 original
    caso2 = crear_caso_4752464_2(folio_ref_caso2)
    precio_panuelo = 7380
    precio_item2 = 6427
    desc_panuelo_pct = 12
    desc_item2_pct = 30

    dte = DTE(
        tipo_dte=61,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=RECEPTOR,
        items=[
            ItemDetalle(
                nro_linea=1, nombre="Panuelo AFECTO",
                cantidad=352, precio_unitario=precio_panuelo, descuento_pct=desc_panuelo_pct,
            ),
            ItemDetalle(
                nro_linea=2, nombre="ITEM 2 AFECTO",
                cantidad=617, precio_unitario=precio_item2, descuento_pct=desc_item2_pct,
            ),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4752464-6",
            ),
            Referencia(
                nro_linea=2,
                tipo_doc_ref="33",
                folio_ref=str(folio_ref_caso2),
                fecha_ref=FECHA_EMISION,
                codigo_ref=3,
                razon_ref="DEVOLUCION DE MERCADERIAS",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4752464_7(folio: int, folio_ref_caso3: int) -> DTE:
    """CASO 7: NC - Anula factura (ref caso 3)."""
    caso3 = crear_caso_4752464_3(folio_ref_caso3)

    dte = DTE(
        tipo_dte=61,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=RECEPTOR,
        items=caso3.items,
        monto_neto=caso3.monto_neto,
        monto_exento=caso3.monto_exento,
        tasa_iva=caso3.tasa_iva,
        iva=caso3.iva,
        monto_total=caso3.monto_total,
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4752464-7",
            ),
            Referencia(
                nro_linea=2,
                tipo_doc_ref="33",
                folio_ref=str(folio_ref_caso3),
                fecha_ref=FECHA_EMISION,
                codigo_ref=1,
                razon_ref="ANULA FACTURA",
            ),
        ],
    )
    return dte


def crear_caso_4752464_8(folio: int, folio_ref_caso5: int) -> DTE:
    """CASO 8: ND - Anula nota de crédito (ref caso 5).
    Caso 5 es CodRef=2 (corrige texto, MntTotal=0), la ND lo anula con MntTotal=0."""
    dte = DTE(
        tipo_dte=56,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="ANULA NOTA DE CREDITO", cantidad=0, precio_unitario=0),
        ],
        monto_neto=0,
        tasa_iva=19,
        iva=0,
        monto_total=0,
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4752464-8",
            ),
            Referencia(
                nro_linea=2,
                tipo_doc_ref="61",
                folio_ref=str(folio_ref_caso5),
                fecha_ref=FECHA_EMISION,
                codigo_ref=1,
                razon_ref="ANULA NOTA DE CREDITO ELECTRONICA",
            ),
        ],
    )
    return dte


# ==================== SET GUÍA DE DESPACHO (4752467) ====================

def crear_caso_4752467_1(folio: int) -> DTE:
    """CASO 1: Guía de despacho - Traslado interno (sin precios)."""
    dte = DTE(
        tipo_dte=52,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=EMISOR.copy(),  # Traslado interno: receptor = emisor
        tipo_traslado=5,  # Traslado interno
        items=[
            ItemDetalle(nro_linea=1, nombre="ITEM 1", cantidad=51, exento=True),
            ItemDetalle(nro_linea=2, nombre="ITEM 2", cantidad=43, exento=True),
            ItemDetalle(nro_linea=3, nombre="ITEM 3", cantidad=13, exento=True),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4752467-1",
            ),
        ],
    )
    # Receptor debe tener los mismos campos que el receptor
    dte.receptor = {
        "RUTRecep": EMISOR["RUTEmisor"],
        "RznSocRecep": EMISOR["RznSoc"],
        "GiroRecep": EMISOR["GiroEmis"],
        "DirRecep": EMISOR["DirOrigen"],
        "CmnaRecep": EMISOR["CmnaOrigen"],
        "CiudadRecep": EMISOR["CiudadOrigen"],
    }
    # Sin montos (no constituye venta)
    dte.monto_total = 0
    return dte


def crear_caso_4752467_2(folio: int) -> DTE:
    """CASO 2: Guía de despacho - Venta, traslado por emisor."""
    dte = DTE(
        tipo_dte=52,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=RECEPTOR,
        tipo_traslado=2,  # Venta
        tipo_despacho=3,  # Emisor al local del cliente
        items=[
            ItemDetalle(nro_linea=1, nombre="ITEM 1", cantidad=24, precio_unitario=1133),
            ItemDetalle(nro_linea=2, nombre="ITEM 2", cantidad=32, precio_unitario=818),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4752467-2",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4752467_3(folio: int) -> DTE:
    """CASO 3: Guía de despacho - Venta, traslado por cliente."""
    dte = DTE(
        tipo_dte=52,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=RECEPTOR,
        tipo_traslado=2,  # Venta
        tipo_despacho=1,  # Por cuenta del receptor (cliente)
        items=[
            ItemDetalle(nro_linea=1, nombre="ITEM 1", cantidad=70, precio_unitario=957),
            ItemDetalle(nro_linea=2, nombre="ITEM 2", cantidad=45, precio_unitario=953),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4752467-3",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


# ==================== SET FACTURA EXENTA (4752469) ====================

def crear_caso_4752469_1(folio: int) -> DTE:
    """CASO 1: Factura exenta."""
    dte = DTE(
        tipo_dte=34,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=RECEPTOR,
        items=[
            ItemDetalle(
                nro_linea=1, nombre="HORAS PROGRAMADOR",
                cantidad=11, precio_unitario=6402, unidad_medida="Hora",
            ),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4752469-1",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4752469_2(folio: int, folio_ref_exenta1: int) -> DTE:
    """CASO 2: NC - Modifica monto (ref factura exenta caso 1).
    Items deben ser exentos porque referencian factura exenta."""
    dte = DTE(
        tipo_dte=61,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=RECEPTOR,
        items=[
            ItemDetalle(
                nro_linea=1, nombre="HORAS PROGRAMADOR",
                cantidad=11, precio_unitario=800, unidad_medida="Hora",
                exento=True,
            ),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4752469-2",
            ),
            Referencia(
                nro_linea=2,
                tipo_doc_ref="34",
                folio_ref=str(folio_ref_exenta1),
                fecha_ref=FECHA_EMISION,
                codigo_ref=3,
                razon_ref="MODIFICA MONTO",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4752469_3(folio: int) -> DTE:
    """CASO 3: Factura exenta."""
    dte = DTE(
        tipo_dte=34,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=RECEPTOR,
        items=[
            ItemDetalle(
                nro_linea=1, nombre="SERV CONSULTORIA FACT ELECTRONICA",
                cantidad=1, precio_unitario=336375,
            ),
            ItemDetalle(
                nro_linea=2, nombre="SERV CONSULTORIA GUIA DESPACHO ELECT",
                cantidad=1, precio_unitario=253250,
            ),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4752469-3",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4752469_4(folio: int, folio_ref_exenta3: int) -> DTE:
    """CASO 4: NC - Corrige giro (ref factura exenta caso 3).
    CodRef=2 (corrige texto): NO debe llevar montos."""
    dte = DTE(
        tipo_dte=61,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="CORRIGE GIRO", cantidad=0, precio_unitario=0, exento=True),
        ],
        monto_exento=0,
        monto_total=0,
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4752469-4",
            ),
            Referencia(
                nro_linea=2,
                tipo_doc_ref="34",
                folio_ref=str(folio_ref_exenta3),
                fecha_ref=FECHA_EMISION,
                codigo_ref=2,
                razon_ref="CORRIGE GIRO",
            ),
        ],
    )
    return dte


def crear_caso_4752469_5(folio: int, folio_ref_nc4: int) -> DTE:
    """CASO 5: ND - Anula NC (ref NC caso 4).
    NC caso 4 es CodRef=2 (corrige texto, MntTotal=0), la ND lo anula con MntTotal=0."""
    dte = DTE(
        tipo_dte=56,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="ANULA NOTA DE CREDITO", cantidad=0, precio_unitario=0, exento=True),
        ],
        monto_exento=0,
        monto_total=0,
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4752469-5",
            ),
            Referencia(
                nro_linea=2,
                tipo_doc_ref="61",
                folio_ref=str(folio_ref_nc4),
                fecha_ref=FECHA_EMISION,
                codigo_ref=1,
                razon_ref="ANULA NOTA DE CREDITO ELECTRONICA",
            ),
        ],
    )
    return dte


def crear_caso_4752469_6(folio: int) -> DTE:
    """CASO 6: Factura exenta."""
    dte = DTE(
        tipo_dte=34,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=RECEPTOR,
        items=[
            ItemDetalle(
                nro_linea=1, nombre="CAPACITACION USO CIGUEÑALES",
                cantidad=1, precio_unitario=340123,
            ),
            ItemDetalle(
                nro_linea=2, nombre="CAPACITACION USO PLC's CNC",
                cantidad=1, precio_unitario=230554,
            ),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4752469-6",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4752469_7(folio: int, folio_ref_exenta6: int) -> DTE:
    """CASO 7: NC - Modifica monto (ref factura exenta caso 6).
    Items deben ser exentos porque referencian factura exenta."""
    dte = DTE(
        tipo_dte=61,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=RECEPTOR,
        items=[
            ItemDetalle(
                nro_linea=1, nombre="CAPACITACION USO CIGUEÑALES",
                cantidad=1, precio_unitario=170062,
                exento=True,
            ),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4752469-7",
            ),
            Referencia(
                nro_linea=2,
                tipo_doc_ref="34",
                folio_ref=str(folio_ref_exenta6),
                fecha_ref=FECHA_EMISION,
                codigo_ref=3,
                razon_ref="MODIFICA MONTO",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4752469_8(folio: int, folio_ref_exenta6: int) -> DTE:
    """CASO 8: ND - Modifica monto (ref factura exenta caso 6).
    Items deben ser exentos porque referencian factura exenta."""
    dte = DTE(
        tipo_dte=56,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=RECEPTOR,
        items=[
            ItemDetalle(
                nro_linea=1, nombre="CAPACITACION USO PLC's CNC",
                cantidad=1, precio_unitario=46111,
                exento=True,
            ),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4752469-8",
            ),
            Referencia(
                nro_linea=2,
                tipo_doc_ref="34",
                folio_ref=str(folio_ref_exenta6),
                fecha_ref=FECHA_EMISION,
                codigo_ref=3,
                razon_ref="MODIFICA MONTO",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


# ==================== SET BOLETA ELECTRÓNICA ====================

def crear_boleta_caso_1(folio: int) -> DTE:
    """CASO-1: Boleta electrónica."""
    receptor_boleta = {"RUTRecep": "66666666-6"}
    dte = DTE(
        tipo_dte=39,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=receptor_boleta,
        items=[
            ItemDetalle(nro_linea=1, nombre="Cambio de aceite", cantidad=1, precio_unitario=19900),
            ItemDetalle(nro_linea=2, nombre="Alineacion y balanceo", cantidad=1, precio_unitario=9900),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO-1",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_boleta_caso_2(folio: int) -> DTE:
    """CASO-2: Boleta electrónica."""
    receptor_boleta = {"RUTRecep": "66666666-6"}
    dte = DTE(
        tipo_dte=39,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=receptor_boleta,
        items=[
            ItemDetalle(nro_linea=1, nombre="Papel de regalo", cantidad=17, precio_unitario=120),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO-2",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_boleta_caso_3(folio: int) -> DTE:
    """CASO-3: Boleta electrónica."""
    receptor_boleta = {"RUTRecep": "66666666-6"}
    dte = DTE(
        tipo_dte=39,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=receptor_boleta,
        items=[
            ItemDetalle(nro_linea=1, nombre="Sandwic", cantidad=2, precio_unitario=1500),
            ItemDetalle(nro_linea=2, nombre="Bebida", cantidad=2, precio_unitario=550),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO-3",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_boleta_caso_4(folio: int) -> DTE:
    """CASO-4: Boleta con item afecto y exento."""
    receptor_boleta = {"RUTRecep": "66666666-6"}
    dte = DTE(
        tipo_dte=39,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=receptor_boleta,
        items=[
            ItemDetalle(nro_linea=1, nombre="item afecto 1", cantidad=8, precio_unitario=1590),
            ItemDetalle(nro_linea=2, nombre="item exento 2", cantidad=2, precio_unitario=1000, exento=True),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO-4",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_boleta_caso_5(folio: int) -> DTE:
    """CASO-5: Boleta con unidad de medida Kg."""
    receptor_boleta = {"RUTRecep": "66666666-6"}
    dte = DTE(
        tipo_dte=39,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=receptor_boleta,
        items=[
            ItemDetalle(
                nro_linea=1, nombre="Arroz",
                cantidad=5, precio_unitario=700, unidad_medida="Kg",
            ),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO-5",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


# ==================== PROCESADOR PRINCIPAL ====================

def procesar_set_completo():
    """Procesa todos los sets de prueba y genera los XMLs."""
    output_dir = settings.OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    caf_manager = CAFManager(settings.CAF_DIR)
    print("CAFs cargados:")
    caf_manager.info()
    print()

    # Saltar folios ya usados en envíos anteriores para evitar duplicados.
    # CAFs nuevos re-obtenidos del SII para T33, T56, T61 (en subcarpetas /CAF/33/ etc.)
    # T33: nuevos CAFs F26-30 (reutilizar con nuevas claves)
    # T56: nuevos CAFs F16-18 (reutilizar con nuevas claves)
    # T61: nuevo CAF F22-27 (folios genuinamente nuevos, los viejos F16-21 están agotados)
    # T34: CAFs viejos F11-20 (F11-13 ya aceptados)
    # T39: F11-15 aceptados vía REST API (boletas OK)
    # T52: F41-43 aceptados (guías OK)
    FOLIOS_USADOS = {
        33: 0,   # Nuevos CAFs F26-30 del SII
        34: 3,   # F11-13 usados, F14-16 disponibles
        39: 5,   # F11-15 usados (boletas ya aceptadas)
        52: 3,   # F41-43 usados (guías ya aceptadas)
        56: 0,   # Nuevos CAFs F16-18 del SII
        61: 0,   # Nuevo CAF F22-27 (folio_actual empieza en 22)
    }
    for tipo_dte, cantidad in FOLIOS_USADOS.items():
        for _ in range(cantidad):
            try:
                caf_manager.siguiente_folio(tipo_dte)
            except ValueError:
                pass
    print("Folios disponibles:")
    for tipo_dte in sorted(caf_manager._folio_actual.keys()):
        folio_actual = caf_manager._folio_actual.get(tipo_dte, "N/A")
        print(f"  Tipo {tipo_dte}: siguiente folio = {folio_actual}")
    print()

    # ==================== SET BÁSICO (4752464) ====================
    print("=" * 60)
    print("SET BASICO - 4752464")
    print("=" * 60)

    dtes_basico = []

    # Facturas (tipo 33)
    folio_f1 = caf_manager.siguiente_folio(33)
    caso1 = crear_caso_4752464_1(folio_f1)
    print(f"  Caso 1: Factura {folio_f1} - Neto: {caso1.monto_neto}, IVA: {caso1.iva}, Total: {caso1.monto_total}")

    folio_f2 = caf_manager.siguiente_folio(33)
    caso2 = crear_caso_4752464_2(folio_f2)
    print(f"  Caso 2: Factura {folio_f2} - Neto: {caso2.monto_neto}, IVA: {caso2.iva}, Total: {caso2.monto_total}")

    folio_f3 = caf_manager.siguiente_folio(33)
    caso3 = crear_caso_4752464_3(folio_f3)
    print(f"  Caso 3: Factura {folio_f3} - Neto: {caso3.monto_neto}, Exento: {caso3.monto_exento}, IVA: {caso3.iva}, Total: {caso3.monto_total}")

    folio_f4 = caf_manager.siguiente_folio(33)
    caso4 = crear_caso_4752464_4(folio_f4)
    print(f"  Caso 4: Factura {folio_f4} - Neto: {caso4.monto_neto}, Exento: {caso4.monto_exento}, IVA: {caso4.iva}, Total: {caso4.monto_total}")

    # Notas de crédito (tipo 61)
    folio_nc5 = caf_manager.siguiente_folio(61)
    caso5 = crear_caso_4752464_5(folio_nc5, folio_f1)
    print(f"  Caso 5: NC {folio_nc5} ref F{folio_f1} - Total: {caso5.monto_total}")

    folio_nc6 = caf_manager.siguiente_folio(61)
    caso6 = crear_caso_4752464_6(folio_nc6, folio_f2)
    print(f"  Caso 6: NC {folio_nc6} ref F{folio_f2} - Total: {caso6.monto_total}")

    folio_nc7 = caf_manager.siguiente_folio(61)
    caso7 = crear_caso_4752464_7(folio_nc7, folio_f3)
    print(f"  Caso 7: NC {folio_nc7} ref F{folio_f3} - Total: {caso7.monto_total}")

    # Nota de débito (tipo 56)
    folio_nd8 = caf_manager.siguiente_folio(56)
    caso8 = crear_caso_4752464_8(folio_nd8, folio_nc5)
    print(f"  Caso 8: ND {folio_nd8} ref NC{folio_nc5} - Total: {caso8.monto_total}")

    # Generar XMLs del set básico
    for caso in [caso1, caso2, caso3, caso4, caso5, caso6, caso7, caso8]:
        caf = caf_manager.obtener_caf(caso.tipo_dte, caso.folio)
        if caf is None:
            print(f"  ERROR: No hay CAF para tipo {caso.tipo_dte} folio {caso.folio}")
            continue
        doc_xml = generar_documento_xml(caso, caf, timestamp=TIMESTAMP)
        dte_xml = generar_dte_xml(doc_xml)
        dtes_basico.append(dte_xml)

        # Guardar DTE individual
        filename = f"DTE_T{caso.tipo_dte}_F{caso.folio}.xml"
        filepath = output_dir / "basico" / filename
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "wb") as f:
            f.write(xml_to_string(dte_xml))
        print(f"  -> {filename}")

    # Generar EnvioDTE del set básico
    envio_basico = generar_envio_dte(
        dtes=dtes_basico,
        rut_emisor=EMISOR["RUTEmisor"],
        rut_envia=RUT_FIRMANTE,
        rut_receptor=settings.RUT_SII,
        fecha_resolucion=FECHA_RESOLUCION,
        nro_resolucion=NUMERO_RESOLUCION,
        timestamp=TIMESTAMP,
    )
    envio_path = output_dir / "EnvioDTE_SetBasico.xml"
    with open(envio_path, "wb") as f:
        f.write(xml_to_string(envio_basico))
    print(f"\n  Sobre: {envio_path.name}")

    # ==================== SET GUÍA DE DESPACHO (4752467) ====================
    print(f"\n{'=' * 60}")
    print("SET GUIA DE DESPACHO - 4752467")
    print("=" * 60)

    dtes_guias = []

    folio_g1 = caf_manager.siguiente_folio(52)
    guia1 = crear_caso_4752467_1(folio_g1)
    print(f"  Caso 1: Guia {folio_g1} - Traslado interno - Total: {guia1.monto_total}")

    folio_g2 = caf_manager.siguiente_folio(52)
    guia2 = crear_caso_4752467_2(folio_g2)
    print(f"  Caso 2: Guia {folio_g2} - Venta emisor - Neto: {guia2.monto_neto}, Total: {guia2.monto_total}")

    folio_g3 = caf_manager.siguiente_folio(52)
    guia3 = crear_caso_4752467_3(folio_g3)
    print(f"  Caso 3: Guia {folio_g3} - Venta cliente - Neto: {guia3.monto_neto}, Total: {guia3.monto_total}")

    for guia in [guia1, guia2, guia3]:
        caf = caf_manager.obtener_caf(guia.tipo_dte, guia.folio)
        doc_xml = generar_documento_xml(guia, caf, timestamp=TIMESTAMP)
        dte_xml = generar_dte_xml(doc_xml)
        dtes_guias.append(dte_xml)

        filename = f"DTE_T{guia.tipo_dte}_F{guia.folio}.xml"
        filepath = output_dir / "guias" / filename
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "wb") as f:
            f.write(xml_to_string(dte_xml))
        print(f"  -> {filename}")

    envio_guias = generar_envio_dte(
        dtes=dtes_guias,
        rut_emisor=EMISOR["RUTEmisor"],
        rut_envia=RUT_FIRMANTE,
        rut_receptor=settings.RUT_SII,
        fecha_resolucion=FECHA_RESOLUCION,
        nro_resolucion=NUMERO_RESOLUCION,
        timestamp=TIMESTAMP,
    )
    envio_path = output_dir / "EnvioDTE_SetGuias.xml"
    with open(envio_path, "wb") as f:
        f.write(xml_to_string(envio_guias))
    print(f"\n  Sobre: {envio_path.name}")

    # ==================== SET FACTURA EXENTA (4752469) ====================
    print(f"\n{'=' * 60}")
    print("SET FACTURA EXENTA - 4752469")
    print("=" * 60)

    dtes_exentas = []

    folio_fe1 = caf_manager.siguiente_folio(34)
    ex1 = crear_caso_4752469_1(folio_fe1)
    print(f"  Caso 1: FExenta {folio_fe1} - Exento: {ex1.monto_exento}, Total: {ex1.monto_total}")

    folio_nc_ex2 = caf_manager.siguiente_folio(61)
    ex2 = crear_caso_4752469_2(folio_nc_ex2, folio_fe1)
    print(f"  Caso 2: NC {folio_nc_ex2} ref FE{folio_fe1} - Total: {ex2.monto_total}")

    folio_fe3 = caf_manager.siguiente_folio(34)
    ex3 = crear_caso_4752469_3(folio_fe3)
    print(f"  Caso 3: FExenta {folio_fe3} - Exento: {ex3.monto_exento}, Total: {ex3.monto_total}")

    folio_nc_ex4 = caf_manager.siguiente_folio(61)
    ex4 = crear_caso_4752469_4(folio_nc_ex4, folio_fe3)
    print(f"  Caso 4: NC {folio_nc_ex4} ref FE{folio_fe3} - Total: {ex4.monto_total}")

    folio_nd_ex5 = caf_manager.siguiente_folio(56)
    ex5 = crear_caso_4752469_5(folio_nd_ex5, folio_nc_ex4)
    print(f"  Caso 5: ND {folio_nd_ex5} ref NC{folio_nc_ex4} - Total: {ex5.monto_total}")

    folio_fe6 = caf_manager.siguiente_folio(34)
    ex6 = crear_caso_4752469_6(folio_fe6)
    print(f"  Caso 6: FExenta {folio_fe6} - Exento: {ex6.monto_exento}, Total: {ex6.monto_total}")

    folio_nc_ex7 = caf_manager.siguiente_folio(61)
    ex7 = crear_caso_4752469_7(folio_nc_ex7, folio_fe6)
    print(f"  Caso 7: NC {folio_nc_ex7} ref FE{folio_fe6} - Total: {ex7.monto_total}")

    folio_nd_ex8 = caf_manager.siguiente_folio(56)
    ex8 = crear_caso_4752469_8(folio_nd_ex8, folio_fe6)
    print(f"  Caso 8: ND {folio_nd_ex8} ref FE{folio_fe6} - Total: {ex8.monto_total}")

    for caso in [ex1, ex2, ex3, ex4, ex5, ex6, ex7, ex8]:
        caf = caf_manager.obtener_caf(caso.tipo_dte, caso.folio)
        if caf is None:
            print(f"  ERROR: No hay CAF para tipo {caso.tipo_dte} folio {caso.folio}")
            continue
        doc_xml = generar_documento_xml(caso, caf, timestamp=TIMESTAMP)
        dte_xml = generar_dte_xml(doc_xml)
        dtes_exentas.append(dte_xml)

        filename = f"DTE_T{caso.tipo_dte}_F{caso.folio}.xml"
        filepath = output_dir / "exentas" / filename
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "wb") as f:
            f.write(xml_to_string(dte_xml))
        print(f"  -> {filename}")

    envio_exentas = generar_envio_dte(
        dtes=dtes_exentas,
        rut_emisor=EMISOR["RUTEmisor"],
        rut_envia=RUT_FIRMANTE,
        rut_receptor=settings.RUT_SII,
        fecha_resolucion=FECHA_RESOLUCION,
        nro_resolucion=NUMERO_RESOLUCION,
        timestamp=TIMESTAMP,
    )
    envio_path = output_dir / "EnvioDTE_SetExentas.xml"
    with open(envio_path, "wb") as f:
        f.write(xml_to_string(envio_exentas))
    print(f"\n  Sobre: {envio_path.name}")

    # ==================== SET BOLETA ELECTRÓNICA ====================
    print(f"\n{'=' * 60}")
    print("SET BOLETA ELECTRONICA")
    print("=" * 60)

    dtes_boletas = []
    boleta_creators = [
        crear_boleta_caso_1,
        crear_boleta_caso_2,
        crear_boleta_caso_3,
        crear_boleta_caso_4,
        crear_boleta_caso_5,
    ]

    for i, creator in enumerate(boleta_creators, 1):
        folio_b = caf_manager.siguiente_folio(39)
        boleta = creator(folio_b)
        print(f"  Caso {i}: Boleta {folio_b} - Neto: {boleta.monto_neto}, Exento: {boleta.monto_exento}, IVA: {boleta.iva}, Total: {boleta.monto_total}")

        caf = caf_manager.obtener_caf(39, folio_b)
        doc_xml = generar_documento_xml(boleta, caf, timestamp=TIMESTAMP)
        dte_xml = generar_dte_xml(doc_xml)
        dtes_boletas.append(dte_xml)

        filename = f"DTE_T39_F{folio_b}.xml"
        filepath = output_dir / "boletas" / filename
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "wb") as f:
            f.write(xml_to_string(dte_xml))
        print(f"  -> {filename}")

    envio_boletas = generar_envio_boleta(
        dtes=dtes_boletas,
        rut_emisor=EMISOR["RUTEmisor"],
        rut_envia=RUT_FIRMANTE,
        fecha_resolucion=FECHA_RESOLUCION,
        nro_resolucion=NUMERO_RESOLUCION,
        timestamp=TIMESTAMP,
    )
    envio_path = output_dir / "EnvioBOLETA_Set.xml"
    with open(envio_path, "wb") as f:
        f.write(xml_to_string(envio_boletas))
    print(f"\n  Sobre: {envio_path.name}")

    # ==================== RESUMEN ====================
    print(f"\n{'=' * 60}")
    print("RESUMEN")
    print("=" * 60)
    print(f"  Directorio de salida: {output_dir}")
    print(f"  Total DTEs generados: {len(dtes_basico) + len(dtes_guias) + len(dtes_exentas) + len(dtes_boletas)}")
    print()
    print("  NOTA: Los XMLs generados NO están firmados con certificado digital (.pfx).")
    print("  Para firmarlos, ejecutar el script de firma con el archivo .pfx exportado de Windows.")
    print()
    print("  Folios utilizados:")
    print(f"    Facturas (33): {folio_f1}-{folio_f4}")
    print(f"    Fact. Exentas (34): {folio_fe1}, {folio_fe3}, {folio_fe6}")
    print(f"    Boletas (39): 1-5")
    print(f"    Guías (52): {folio_g1}-{folio_g3}")
    print(f"    N. Débito (56): {folio_nd8}, {folio_nd_ex5}, {folio_nd_ex8}")
    print(f"    N. Crédito (61): {folio_nc5}-{folio_nc7}, {folio_nc_ex2}, {folio_nc_ex4}, {folio_nc_ex7}")


if __name__ == "__main__":
    procesar_set_completo()

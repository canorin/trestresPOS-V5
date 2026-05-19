"""
Procesador del Set de Pruebas — Nueva Postulación Crumb.
Fecha Resolución: 2026-03-26, Resolución: 0

Números de atención (NUEVO SET - 2026-03-31):
- Set Básico: 4758672
- Libro de Ventas: 4758673
- Libro de Compras: 4758674
- Set Guía de Despacho: 4758675 (SOK ✅)
- Libro de Guías: 4758676
- Set Factura Exenta: 4758677
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

COMPANY_DIR = Path(__file__).resolve().parent.parent.parent.parent / "nuevapostulacion"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

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

# Directorio de CAFs y output para nueva postulación
CAF_DIR = COMPANY_DIR / "CAF"
OUTPUT_DIR = COMPANY_DIR / "output"


# ==================== SET BÁSICO (4758672) ====================

def crear_caso_4758672_1(folio: int) -> DTE:
    """CASO 1: Factura electrónica simple."""
    dte = DTE(
        tipo_dte=33, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Cajón AFECTO", cantidad=145, precio_unitario=2179),
            ItemDetalle(nro_linea=2, nombre="Relleno AFECTO", cantidad=62, precio_unitario=3596),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4758672-1"),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4758672_2(folio: int) -> DTE:
    """CASO 2: Factura electrónica con descuentos por ítem."""
    dte = DTE(
        tipo_dte=33, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Pañuelo AFECTO",
                        cantidad=491, precio_unitario=3857, descuento_pct=7),
            ItemDetalle(nro_linea=2, nombre="ITEM 2 AFECTO",
                        cantidad=426, precio_unitario=2915, descuento_pct=14),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4758672-2"),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4758672_3(folio: int) -> DTE:
    """CASO 3: Factura electrónica con ítem exento (servicio)."""
    dte = DTE(
        tipo_dte=33, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Pintura B&W AFECTO", cantidad=37, precio_unitario=4482),
            ItemDetalle(nro_linea=2, nombre="ITEM 2 AFECTO", cantidad=191, precio_unitario=3397),
            ItemDetalle(nro_linea=3, nombre="ITEM 3 SERVICIO EXENTO", cantidad=1, precio_unitario=34992, exento=True),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4758672-3"),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4758672_4(folio: int) -> DTE:
    """CASO 4: Factura electrónica con descuento global 14% sobre afectos."""
    dte = DTE(
        tipo_dte=33, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="ITEM 1 AFECTO", cantidad=244, precio_unitario=3749),
            ItemDetalle(nro_linea=2, nombre="ITEM 2 AFECTO", cantidad=104, precio_unitario=4241),
            ItemDetalle(nro_linea=3, nombre="ITEM 3 SERVICIO EXENTO", cantidad=2, precio_unitario=6799, exento=True),
        ],
        descuentos_globales=[
            DescuentoGlobal(
                nro_linea=1, tipo="D",
                descripcion="Descuento global 14%",
                tipo_valor="%", valor=14,
            ),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4758672-4"),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4758672_5(folio: int, folio_ref_f1: int) -> DTE:
    """CASO 5: NC - Corrige giro del receptor (ref factura caso 1).
    CodRef=2 (corrige texto): NO debe llevar montos."""
    dte = DTE(
        tipo_dte=61, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="CORRIGE GIRO DEL RECEPTOR", cantidad=0, precio_unitario=0),
        ],
        monto_neto=0, monto_total=0,
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4758672-5"),
            Referencia(nro_linea=2, tipo_doc_ref="33", folio_ref=str(folio_ref_f1),
                       fecha_ref=FECHA_EMISION, codigo_ref=2,
                       razon_ref="CORRIGE GIRO DEL RECEPTOR"),
        ],
    )
    return dte


def crear_caso_4758672_6(folio: int, folio_ref_f2: int) -> DTE:
    """CASO 6: NC - Devolución de mercaderías (ref factura caso 2).
    Devuelve 180 de Pañuelo y 289 de ITEM 2 con mismos precios y descuentos."""
    dte = DTE(
        tipo_dte=61, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Pañuelo AFECTO",
                        cantidad=180, precio_unitario=3857, descuento_pct=7),
            ItemDetalle(nro_linea=2, nombre="ITEM 2 AFECTO",
                        cantidad=289, precio_unitario=2915, descuento_pct=14),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4758672-6"),
            Referencia(nro_linea=2, tipo_doc_ref="33", folio_ref=str(folio_ref_f2),
                       fecha_ref=FECHA_EMISION, codigo_ref=3,
                       razon_ref="DEVOLUCION DE MERCADERIAS"),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4758672_7(folio: int, folio_ref_f3: int) -> DTE:
    """CASO 7: NC - Anula factura completa (ref factura caso 3).
    CodRef=1: anula el documento completo. Montos = mismos que factura original."""
    dte = DTE(
        tipo_dte=61, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Pintura B&W AFECTO", cantidad=37, precio_unitario=4482),
            ItemDetalle(nro_linea=2, nombre="ITEM 2 AFECTO", cantidad=191, precio_unitario=3397),
            ItemDetalle(nro_linea=3, nombre="ITEM 3 SERVICIO EXENTO", cantidad=1, precio_unitario=34992, exento=True),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4758672-7"),
            Referencia(nro_linea=2, tipo_doc_ref="33", folio_ref=str(folio_ref_f3),
                       fecha_ref=FECHA_EMISION, codigo_ref=1,
                       razon_ref="ANULA FACTURA"),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4758672_8(folio: int, folio_ref_nc5: int) -> DTE:
    """CASO 8: ND - Anula NC (ref NC caso 5).
    NC caso 5 es CodRef=2 (corrige texto, MntTotal=0), la ND lo anula con MntTotal=0."""
    dte = DTE(
        tipo_dte=56, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="ANULA NOTA DE CREDITO ELECTRONICA", cantidad=0, precio_unitario=0),
        ],
        monto_neto=0, monto_total=0,
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4758672-8"),
            Referencia(nro_linea=2, tipo_doc_ref="61", folio_ref=str(folio_ref_nc5),
                       fecha_ref=FECHA_EMISION, codigo_ref=1,
                       razon_ref="ANULA NOTA DE CREDITO ELECTRONICA"),
        ],
    )
    return dte


# ==================== SET GUÍA DE DESPACHO (4758675) — SOK ✅ ====================
# NOTA: El SET GUIA ya fue aprobado. Estas funciones se actualizan con datos
# del nuevo set por consistencia, pero NO deben re-enviarse.

def crear_caso_4758675_1(folio: int) -> DTE:
    """CASO 1: Guía de despacho - Traslado interno entre bodegas."""
    dte = DTE(
        tipo_dte=52, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR,
        receptor={
            "RUTRecep": EMISOR["RUTEmisor"],
            "RznSocRecep": EMISOR["RznSoc"],
            "GiroRecep": EMISOR["GiroEmis"],
            "DirRecep": EMISOR["DirOrigen"],
            "CmnaRecep": EMISOR["CmnaOrigen"],
            "CiudadRecep": EMISOR["CiudadOrigen"],
        },
        tipo_traslado=5,  # Traslado interno
        items=[
            ItemDetalle(nro_linea=1, nombre="ITEM 1", cantidad=63),
            ItemDetalle(nro_linea=2, nombre="ITEM 2", cantidad=78),
            ItemDetalle(nro_linea=3, nombre="ITEM 3", cantidad=44),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4758675-1"),
        ],
    )
    dte.monto_total = 0
    return dte


def crear_caso_4758675_2(folio: int) -> DTE:
    """CASO 2: Guía de despacho - Venta, traslado por emisor al local del cliente."""
    dte = DTE(
        tipo_dte=52, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        tipo_traslado=1,  # Operación constituye venta
        tipo_despacho=2,  # Por cuenta del emisor a instalaciones del receptor
        items=[
            ItemDetalle(nro_linea=1, nombre="ITEM 1", cantidad=167, precio_unitario=3771),
            ItemDetalle(nro_linea=2, nombre="ITEM 2", cantidad=314, precio_unitario=1175),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4758675-2"),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4758675_3(folio: int) -> DTE:
    """CASO 3: Guía de despacho - Venta, traslado por cliente."""
    dte = DTE(
        tipo_dte=52, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        tipo_traslado=1,  # Operación constituye venta
        tipo_despacho=1,  # Por cuenta del receptor (cliente retira)
        items=[
            ItemDetalle(nro_linea=1, nombre="ITEM 1", cantidad=114, precio_unitario=1398),
            ItemDetalle(nro_linea=2, nombre="ITEM 2", cantidad=208, precio_unitario=2984),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4758675-3"),
        ],
    )
    dte.calcular_totales()
    return dte


# ==================== SET FACTURA EXENTA (4758677) ====================

def crear_caso_4758677_1(folio: int) -> DTE:
    """CASO 1: Factura exenta — 8 horas programador a 4951."""
    dte = DTE(
        tipo_dte=34, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="HORAS PROGRAMADOR",
                        cantidad=8, precio_unitario=4951, unidad_medida="Hora"),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4758677-1"),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4758677_2(folio: int, folio_ref_exenta1: int) -> DTE:
    """CASO 2: NC - Modifica monto (ref factura exenta caso 1).
    Nuevo valor unitario: 619. Cantidad original: 8."""
    dte = DTE(
        tipo_dte=61, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="HORAS PROGRAMADOR",
                        cantidad=8, precio_unitario=619, unidad_medida="Hora", exento=True),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4758677-2"),
            Referencia(nro_linea=2, tipo_doc_ref="34", folio_ref=str(folio_ref_exenta1),
                       fecha_ref=FECHA_EMISION, codigo_ref=3,
                       razon_ref="MODIFICA MONTO"),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4758677_3(folio: int) -> DTE:
    """CASO 3: Factura exenta — consultorías."""
    dte = DTE(
        tipo_dte=34, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="SERV CONSULTORIA FACT ELECTRONICA",
                        cantidad=1, precio_unitario=277222),
            ItemDetalle(nro_linea=2, nombre="SERV CONSULTORIA GUIA DESPACHO ELECT",
                        cantidad=1, precio_unitario=231676),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4758677-3"),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4758677_4(folio: int, folio_ref_exenta3: int) -> DTE:
    """CASO 4: NC - Corrige giro (ref factura exenta caso 3).
    CodRef=2 (corrige texto): NO debe llevar montos."""
    dte = DTE(
        tipo_dte=61, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="CORRIGE GIRO", cantidad=0, precio_unitario=0, exento=True),
        ],
        monto_exento=0, monto_total=0,
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4758677-4"),
            Referencia(nro_linea=2, tipo_doc_ref="34", folio_ref=str(folio_ref_exenta3),
                       fecha_ref=FECHA_EMISION, codigo_ref=2,
                       razon_ref="CORRIGE GIRO"),
        ],
    )
    return dte


def crear_caso_4758677_5(folio: int, folio_ref_nc4: int) -> DTE:
    """CASO 5: ND - Anula NC (ref NC caso 4).
    NC caso 4 es CodRef=2 (MntTotal=0), la ND lo anula con MntTotal=0."""
    dte = DTE(
        tipo_dte=56, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="ANULA NOTA DE CREDITO", cantidad=0, precio_unitario=0, exento=True),
        ],
        monto_exento=0, monto_total=0,
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4758677-5"),
            Referencia(nro_linea=2, tipo_doc_ref="61", folio_ref=str(folio_ref_nc4),
                       fecha_ref=FECHA_EMISION, codigo_ref=1,
                       razon_ref="ANULA NOTA DE CREDITO ELECTRONICA"),
        ],
    )
    return dte


def crear_caso_4758677_6(folio: int) -> DTE:
    """CASO 6: Factura exenta — capacitaciones."""
    dte = DTE(
        tipo_dte=34, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="CAPACITACION USO CIGUEÑALES",
                        cantidad=1, precio_unitario=314752),
            ItemDetalle(nro_linea=2, nombre="CAPACITACION USO PLC's CNC",
                        cantidad=1, precio_unitario=208306),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4758677-6"),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4758677_7(folio: int, folio_ref_exenta6: int) -> DTE:
    """CASO 7: NC - Modifica monto (ref factura exenta caso 6).
    Nuevo valor unitario CIGUEÑALES: 157376. Cantidad original: 1."""
    dte = DTE(
        tipo_dte=61, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="CAPACITACION USO CIGUEÑALES",
                        cantidad=1, precio_unitario=157376, exento=True),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4758677-7"),
            Referencia(nro_linea=2, tipo_doc_ref="34", folio_ref=str(folio_ref_exenta6),
                       fecha_ref=FECHA_EMISION, codigo_ref=3,
                       razon_ref="MODIFICA MONTO"),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4758677_8(folio: int, folio_ref_exenta6: int) -> DTE:
    """CASO 8: ND - Modifica monto (ref factura exenta caso 6).
    Nuevo valor unitario PLC's CNC: 41661. Cantidad original: 1."""
    dte = DTE(
        tipo_dte=56, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="CAPACITACION USO PLC's CNC",
                        cantidad=1, precio_unitario=41661, exento=True),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4758677-8"),
            Referencia(nro_linea=2, tipo_doc_ref="34", folio_ref=str(folio_ref_exenta6),
                       fecha_ref=FECHA_EMISION, codigo_ref=3,
                       razon_ref="MODIFICA MONTO"),
        ],
    )
    dte.calcular_totales()
    return dte


# ==================== SET BOLETA ELECTRÓNICA ====================

def crear_boleta_caso_1(folio: int) -> DTE:
    """CASO-1: Boleta con servicios automotriz."""
    dte = DTE(
        tipo_dte=39, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR,
        receptor={"RUTRecep": "66666666-6"},
        indicador_servicio=3,
        items=[
            ItemDetalle(nro_linea=1, nombre="Cambio de aceite", cantidad=1, precio_unitario=19900),
            ItemDetalle(nro_linea=2, nombre="Alineacion y balanceo", cantidad=1, precio_unitario=9900),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       razon_ref="CASO-1"),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_boleta_caso_2(folio: int) -> DTE:
    """CASO-2: Boleta simple."""
    dte = DTE(
        tipo_dte=39, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR,
        receptor={"RUTRecep": "66666666-6"},
        indicador_servicio=3,
        items=[
            ItemDetalle(nro_linea=1, nombre="Papel de regalo", cantidad=17, precio_unitario=120),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       razon_ref="CASO-2"),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_boleta_caso_3(folio: int) -> DTE:
    """CASO-3: Boleta comida."""
    dte = DTE(
        tipo_dte=39, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR,
        receptor={"RUTRecep": "66666666-6"},
        indicador_servicio=3,
        items=[
            ItemDetalle(nro_linea=1, nombre="Sandwic", cantidad=2, precio_unitario=1500),
            ItemDetalle(nro_linea=2, nombre="Bebida", cantidad=2, precio_unitario=550),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       razon_ref="CASO-3"),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_boleta_caso_4(folio: int) -> DTE:
    """CASO-4: Boleta con item afecto y exento."""
    dte = DTE(
        tipo_dte=39, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR,
        receptor={"RUTRecep": "66666666-6"},
        indicador_servicio=3,
        items=[
            ItemDetalle(nro_linea=1, nombre="item afecto 1", cantidad=8, precio_unitario=1590),
            ItemDetalle(nro_linea=2, nombre="item exento 2", cantidad=2, precio_unitario=1000, exento=True),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       razon_ref="CASO-4"),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_boleta_caso_5(folio: int) -> DTE:
    """CASO-5: Boleta con unidad de medida Kg."""
    dte = DTE(
        tipo_dte=39, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR,
        receptor={"RUTRecep": "66666666-6"},
        indicador_servicio=3,
        items=[
            ItemDetalle(nro_linea=1, nombre="Arroz", cantidad=5, precio_unitario=700, unidad_medida="Kg"),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       razon_ref="CASO-5"),
        ],
    )
    dte.calcular_totales()
    return dte


# ==================== PROCESADORES ====================

def _generar_y_guardar(casos: list[DTE], caf_manager, subdir: str, sobre_nombre: str,
                       es_boleta: bool = False) -> list:
    """Genera XMLs individuales y el sobre EnvioDTE/EnvioBOLETA."""
    out = OUTPUT_DIR / subdir
    out.mkdir(parents=True, exist_ok=True)

    dtes_xml = []
    for caso in casos:
        caf = caf_manager.obtener_caf(caso.tipo_dte, caso.folio)
        if caf is None:
            print(f"  ERROR: No hay CAF para T{caso.tipo_dte} F{caso.folio}")
            return []
        doc_xml = generar_documento_xml(caso, caf, timestamp=TIMESTAMP)
        dte_xml = generar_dte_xml(doc_xml)
        dtes_xml.append(dte_xml)

        filename = f"DTE_T{caso.tipo_dte}_F{caso.folio}.xml"
        filepath = out / filename
        with open(filepath, "wb") as f:
            f.write(xml_to_string(dte_xml))
        print(f"    -> {filename}")

    if es_boleta:
        envio = generar_envio_boleta(
            dtes=dtes_xml,
            rut_emisor=EMISOR["RUTEmisor"],
            rut_envia=RUT_FIRMANTE,
            fecha_resolucion=FECHA_RESOLUCION,
            nro_resolucion=NUMERO_RESOLUCION,
            timestamp=TIMESTAMP,
        )
    else:
        envio = generar_envio_dte(
            dtes=dtes_xml,
            rut_emisor=EMISOR["RUTEmisor"],
            rut_envia=RUT_FIRMANTE,
            rut_receptor=settings.RUT_SII,
            fecha_resolucion=FECHA_RESOLUCION,
            nro_resolucion=NUMERO_RESOLUCION,
            timestamp=TIMESTAMP,
        )

    envio_path = OUTPUT_DIR / sobre_nombre
    with open(envio_path, "wb") as f:
        f.write(xml_to_string(envio))
    print(f"  Sobre: {envio_path.name}")

    return dtes_xml


def procesar_basico(caf_manager):
    """Procesa el Set Básico (4758672)."""
    print("=" * 60)
    print("SET BÁSICO - 4758672")
    print("=" * 60)

    f1 = caf_manager.siguiente_folio(33)
    caso1 = crear_caso_4758672_1(f1)
    print(f"  Caso 1: T33 F{f1} - Neto: {caso1.monto_neto}, Total: {caso1.monto_total}")

    f2 = caf_manager.siguiente_folio(33)
    caso2 = crear_caso_4758672_2(f2)
    print(f"  Caso 2: T33 F{f2} - Neto: {caso2.monto_neto}, Total: {caso2.monto_total}")

    f3 = caf_manager.siguiente_folio(33)
    caso3 = crear_caso_4758672_3(f3)
    print(f"  Caso 3: T33 F{f3} - Neto: {caso3.monto_neto}, Exe: {caso3.monto_exento}, Total: {caso3.monto_total}")

    f4 = caf_manager.siguiente_folio(33)
    caso4 = crear_caso_4758672_4(f4)
    print(f"  Caso 4: T33 F{f4} - Neto: {caso4.monto_neto}, Exe: {caso4.monto_exento}, Total: {caso4.monto_total}")

    f5 = caf_manager.siguiente_folio(61)
    caso5 = crear_caso_4758672_5(f5, f1)
    print(f"  Caso 5: T61 F{f5} - NC corrige giro - Total: {caso5.monto_total}")

    f6 = caf_manager.siguiente_folio(61)
    caso6 = crear_caso_4758672_6(f6, f2)
    print(f"  Caso 6: T61 F{f6} - NC devolución - Neto: {caso6.monto_neto}, Total: {caso6.monto_total}")

    f7 = caf_manager.siguiente_folio(61)
    caso7 = crear_caso_4758672_7(f7, f3)
    print(f"  Caso 7: T61 F{f7} - NC anula factura - Neto: {caso7.monto_neto}, Exe: {caso7.monto_exento}, Total: {caso7.monto_total}")

    f8 = caf_manager.siguiente_folio(56)
    caso8 = crear_caso_4758672_8(f8, f5)
    print(f"  Caso 8: T56 F{f8} - ND anula NC - Total: {caso8.monto_total}")

    casos = [caso1, caso2, caso3, caso4, caso5, caso6, caso7, caso8]
    _generar_y_guardar(casos, caf_manager, "basico", "EnvioDTE_SetBasico.xml")

    print(f"  Folios: T33 F{f1}-F{f4} | T61 F{f5}-F{f7} | T56 F{f8}")
    return {"casos": casos, "folios": {"f1": f1, "f2": f2, "f3": f3, "f4": f4,
                                        "f5": f5, "f6": f6, "f7": f7, "f8": f8}}


def procesar_guias(caf_manager):
    """Procesa el Set Guía de Despacho (4758675) — YA APROBADO SOK."""
    print(f"\n{'=' * 60}")
    print("SET GUÍA DE DESPACHO - 4758675 (SOK ✅)")
    print("=" * 60)

    g1 = caf_manager.siguiente_folio(52)
    guia1 = crear_caso_4758675_1(g1)
    print(f"  Caso 1: T52 F{g1} - Traslado interno - Total: {guia1.monto_total}")

    g2 = caf_manager.siguiente_folio(52)
    guia2 = crear_caso_4758675_2(g2)
    print(f"  Caso 2: T52 F{g2} - Venta emisor - Neto: {guia2.monto_neto}, Total: {guia2.monto_total}")

    g3 = caf_manager.siguiente_folio(52)
    guia3 = crear_caso_4758675_3(g3)
    print(f"  Caso 3: T52 F{g3} - Venta cliente - Neto: {guia3.monto_neto}, Total: {guia3.monto_total}")

    casos = [guia1, guia2, guia3]
    _generar_y_guardar(casos, caf_manager, "guias", "EnvioDTE_SetGuias.xml")

    print(f"  Folios: T52 F{g1}-F{g3}")
    return {"guias": casos, "folios": {"g1": g1, "g2": g2, "g3": g3}}


def procesar_exentas(caf_manager):
    """Procesa el Set Factura Exenta (4758677)."""
    print(f"\n{'=' * 60}")
    print("SET FACTURA EXENTA - 4758677")
    print("=" * 60)

    fe1 = caf_manager.siguiente_folio(34)
    caso1 = crear_caso_4758677_1(fe1)
    print(f"  Caso 1: T34 F{fe1} - Exe: {caso1.monto_exento}, Total: {caso1.monto_total}")

    nc2 = caf_manager.siguiente_folio(61)
    caso2 = crear_caso_4758677_2(nc2, fe1)
    print(f"  Caso 2: T61 F{nc2} - NC modifica monto - Exe: {caso2.monto_exento}, Total: {caso2.monto_total}")

    fe3 = caf_manager.siguiente_folio(34)
    caso3 = crear_caso_4758677_3(fe3)
    print(f"  Caso 3: T34 F{fe3} - Exe: {caso3.monto_exento}, Total: {caso3.monto_total}")

    nc4 = caf_manager.siguiente_folio(61)
    caso4 = crear_caso_4758677_4(nc4, fe3)
    print(f"  Caso 4: T61 F{nc4} - NC corrige giro - Total: {caso4.monto_total}")

    nd5 = caf_manager.siguiente_folio(56)
    caso5 = crear_caso_4758677_5(nd5, nc4)
    print(f"  Caso 5: T56 F{nd5} - ND anula NC - Total: {caso5.monto_total}")

    fe6 = caf_manager.siguiente_folio(34)
    caso6 = crear_caso_4758677_6(fe6)
    print(f"  Caso 6: T34 F{fe6} - Exe: {caso6.monto_exento}, Total: {caso6.monto_total}")

    nc7 = caf_manager.siguiente_folio(61)
    caso7 = crear_caso_4758677_7(nc7, fe6)
    print(f"  Caso 7: T61 F{nc7} - NC modifica monto - Exe: {caso7.monto_exento}, Total: {caso7.monto_total}")

    nd8 = caf_manager.siguiente_folio(56)
    caso8 = crear_caso_4758677_8(nd8, fe6)
    print(f"  Caso 8: T56 F{nd8} - ND modifica monto - Exe: {caso8.monto_exento}, Total: {caso8.monto_total}")

    casos = [caso1, caso2, caso3, caso4, caso5, caso6, caso7, caso8]
    _generar_y_guardar(casos, caf_manager, "exentas", "EnvioDTE_SetExentas.xml")

    print(f"  Folios: T34 F{fe1},F{fe3},F{fe6} | T61 F{nc2},F{nc4},F{nc7} | T56 F{nd5},F{nd8}")
    return {"casos": casos}


def procesar_boletas(caf_manager):
    """Procesa el Set Boleta Electrónica."""
    print(f"\n{'=' * 60}")
    print("SET BOLETA ELECTRÓNICA")
    print("=" * 60)

    b1 = caf_manager.siguiente_folio(39)
    caso1 = crear_boleta_caso_1(b1)
    print(f"  Caso 1: T39 F{b1} - Total: {caso1.monto_total}")

    b2 = caf_manager.siguiente_folio(39)
    caso2 = crear_boleta_caso_2(b2)
    print(f"  Caso 2: T39 F{b2} - Total: {caso2.monto_total}")

    b3 = caf_manager.siguiente_folio(39)
    caso3 = crear_boleta_caso_3(b3)
    print(f"  Caso 3: T39 F{b3} - Total: {caso3.monto_total}")

    b4 = caf_manager.siguiente_folio(39)
    caso4 = crear_boleta_caso_4(b4)
    print(f"  Caso 4: T39 F{b4} - Neto: {caso4.monto_neto}, Exe: {caso4.monto_exento}, Total: {caso4.monto_total}")

    b5 = caf_manager.siguiente_folio(39)
    caso5 = crear_boleta_caso_5(b5)
    print(f"  Caso 5: T39 F{b5} - Total: {caso5.monto_total}")

    casos = [caso1, caso2, caso3, caso4, caso5]
    _generar_y_guardar(casos, caf_manager, "boletas", "EnvioBOLETA_Set.xml", es_boleta=True)

    print(f"  Folios: T39 F{b1}-F{b5}")
    return {"casos": casos}


def procesar_todos():
    """Procesa todos los sets de prueba.

    IMPORTANTE: El SET GUÍA (4758675) YA FUE APROBADO (SOK, track 0246379780)
    con folios F56-F58 y datos del set 4754277. NO se regenera.
    Los datos aprobados se inyectan directamente en set_results.json para que
    el Libro de Guías use los folios/montos EXACTOS que el SII ya aceptó.
    """
    caf_manager = CAFManager(CAF_DIR)
    print("CAFs cargados:")
    caf_manager.info()
    print()

    print("Folios iniciales:")
    for t in [33, 34, 39, 46, 52, 56, 61]:
        folio = caf_manager._folio_actual.get(t, "N/A")
        print(f"  T{t}: siguiente = F{folio}")
    print()

    basico_data = procesar_basico(caf_manager)

    # ---- SET GUÍA: NO REGENERAR — YA APROBADO SOK ----
    print(f"\n{'=' * 60}")
    print("SET GUÍA DE DESPACHO - 4758675 (SOK ✅ — NO SE REGENERA)")
    print("=" * 60)
    print("  Usando datos aprobados: F56 (traslado), F57 (venta), F58 (venta)")
    print("  Track: 0246379780 — Nro Atención: 4758675")
    # Datos EXACTOS de los XMLs aprobados (output/guias/DTE_T52_F56/57/58.xml)
    guias_data_aprobadas = {
        "folios": {"g1": 56, "g2": 57, "g3": 58},
        "casos_info": [
            {"tipo_dte": 52, "folio": 56, "monto_neto": 0, "monto_exento": 0, "iva": 0, "monto_total": 0},
            {"tipo_dte": 52, "folio": 57, "monto_neto": 4777086, "monto_exento": 0, "iva": 907646, "monto_total": 5684732},
            {"tipo_dte": 52, "folio": 58, "monto_neto": 3522671, "monto_exento": 0, "iva": 669307, "monto_total": 4191978},
        ],
    }

    exentas_data = procesar_exentas(caf_manager)
    boletas_data = procesar_boletas(caf_manager)

    # Guardar resultados para generar_libros.py
    _guardar_resultados_set(basico_data, guias_data_aprobadas)

    print(f"\n{'=' * 60}")
    print("RESUMEN")
    print("=" * 60)
    print(f"  Set Básico (4758672): 8 DTEs — REGENERADO")
    print(f"  Set Guías (4758675): 3 DTEs — SOK ✅ (datos aprobados, no regenerado)")
    print(f"  Set Exentas (4758677): 8 DTEs — REGENERADO")
    print(f"  Set Boletas: 5 DTEs — REGENERADO")
    print(f"  TOTAL: 24 DTEs (21 nuevos + 3 aprobados)")
    print(f"\n  Output: {OUTPUT_DIR}")
    print(f"  set_results.json guardado para generar_libros.py")
    print(f"  NOTA: Ejecutar firmar_set.py para firmar los sobres antes de enviar")

    return basico_data, guias_data_aprobadas, exentas_data, boletas_data


def _guardar_resultados_set(basico_data, guias_data):
    """Guarda folios y montos de los sets para que generar_libros.py los use.

    basico_data: {"casos": [DTE, ...], "folios": {...}}
    guias_data: puede ser:
      - {"guias": [DTE, ...], "folios": {...}} (si se generaron guías nuevas)
      - {"casos_info": [dict, ...], "folios": {...}} (datos aprobados ya como dicts)
    """
    def _dte_info(dte):
        return {
            "tipo_dte": dte.tipo_dte,
            "folio": dte.folio,
            "monto_neto": dte.monto_neto or 0,
            "monto_exento": dte.monto_exento or 0,
            "iva": dte.iva or 0,
            "monto_total": dte.monto_total or 0,
        }

    # Guías: usar datos pre-preparados si están disponibles, sino convertir DTEs
    if "casos_info" in guias_data:
        guias_casos = guias_data["casos_info"]
    else:
        guias_casos = [_dte_info(g) for g in guias_data["guias"]]

    results = {
        "fecha_emision": FECHA_EMISION,
        "basico": {
            "folios": basico_data["folios"],
            "casos": [_dte_info(c) for c in basico_data["casos"]],
        },
        "guias": {
            "folios": guias_data["folios"],
            "casos": guias_casos,
        },
    }

    path = OUTPUT_DIR / "set_results.json"
    with open(path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Resultados guardados: {path}")


if __name__ == "__main__":
    procesar_todos()

"""
Procesador del NUEVO Set de Pruebas del SII para certificación.
Set generado el 2026-03-26 (reemplaza sets anteriores).

Números de atención:
- Set Básico: 4753440
- Set Guía de Despacho: 4753441
- Set Factura Exenta: 4753442
- Libro de Ventas: 4753443
- Libro de Compras: 4753444
- Libro de Guías: 4753445
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


# ==================== SET BÁSICO (4753440) ====================

def crear_caso_4753440_1(folio: int) -> DTE:
    """CASO 1: Factura electrónica simple."""
    dte = DTE(
        tipo_dte=33,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Cajón AFECTO", cantidad=155, precio_unitario=2732),
            ItemDetalle(nro_linea=2, nombre="Relleno AFECTO", cantidad=66, precio_unitario=4531),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4753440-1",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4753440_2(folio: int) -> DTE:
    """CASO 2: Factura electrónica con descuentos por ítem."""
    dte = DTE(
        tipo_dte=33,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=RECEPTOR,
        items=[
            ItemDetalle(
                nro_linea=1, nombre="Pañuelo AFECTO",
                cantidad=604, precio_unitario=4708, descuento_pct=8,
            ),
            ItemDetalle(
                nro_linea=2, nombre="ITEM 2 AFECTO",
                cantidad=543, precio_unitario=3763, descuento_pct=18,
            ),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4753440-2",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4753440_3(folio: int) -> DTE:
    """CASO 3: Factura electrónica con ítem exento (servicio)."""
    dte = DTE(
        tipo_dte=33,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Pintura B&W AFECTO", cantidad=46, precio_unitario=5520),
            ItemDetalle(nro_linea=2, nombre="ITEM 2 AFECTO", cantidad=210, precio_unitario=3643),
            ItemDetalle(nro_linea=3, nombre="ITEM 3 SERVICIO EXENTO", cantidad=1, precio_unitario=35119, exento=True),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4753440-3",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4753440_4(folio: int) -> DTE:
    """CASO 4: Factura electrónica con descuento global 18% sobre afectos."""
    dte = DTE(
        tipo_dte=33,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="ITEM 1 AFECTO", cantidad=316, precio_unitario=4664),
            ItemDetalle(nro_linea=2, nombre="ITEM 2 AFECTO", cantidad=134, precio_unitario=5491),
            ItemDetalle(nro_linea=3, nombre="ITEM 3 SERVICIO EXENTO", cantidad=2, precio_unitario=6814, exento=True),
        ],
        descuentos_globales=[
            DescuentoGlobal(
                nro_linea=1,
                tipo="D",
                descripcion="Descuento global 18%",
                tipo_valor="%",
                valor=18,
            ),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4753440-4",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4753440_5(folio: int, folio_ref_f1: int) -> DTE:
    """CASO 5: NC - Corrige giro del receptor (ref factura caso 1).
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
        monto_total=0,
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4753440-5",
            ),
            Referencia(
                nro_linea=2,
                tipo_doc_ref="33",
                folio_ref=str(folio_ref_f1),
                fecha_ref=FECHA_EMISION,
                codigo_ref=2,
                razon_ref="CORRIGE GIRO DEL RECEPTOR",
            ),
        ],
    )
    return dte


def crear_caso_4753440_6(folio: int, folio_ref_f2: int, caso2_dte: DTE) -> DTE:
    """CASO 6: NC - Devolución de mercaderías (ref factura caso 2).
    Devuelve 222 de Panuelo y 368 de ITEM 2 con mismos precios y descuentos."""
    # Recalcular con cantidades devueltas usando mismos precios/descuentos del caso 2
    dte = DTE(
        tipo_dte=61,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=RECEPTOR,
        items=[
            ItemDetalle(
                nro_linea=1, nombre="Pañuelo AFECTO",
                cantidad=222, precio_unitario=4708, descuento_pct=8,
            ),
            ItemDetalle(
                nro_linea=2, nombre="ITEM 2 AFECTO",
                cantidad=368, precio_unitario=3763, descuento_pct=18,
            ),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4753440-6",
            ),
            Referencia(
                nro_linea=2,
                tipo_doc_ref="33",
                folio_ref=str(folio_ref_f2),
                fecha_ref=FECHA_EMISION,
                codigo_ref=3,
                razon_ref="DEVOLUCION DE MERCADERIAS",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4753440_7(folio: int, folio_ref_f3: int, caso3_dte: DTE) -> DTE:
    """CASO 7: NC - Anula factura completa (ref factura caso 3).
    CodRef=1: anula el documento completo. Montos = mismos que factura original."""
    dte = DTE(
        tipo_dte=61,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Pintura B&W AFECTO", cantidad=46, precio_unitario=5520),
            ItemDetalle(nro_linea=2, nombre="ITEM 2 AFECTO", cantidad=210, precio_unitario=3643),
            ItemDetalle(nro_linea=3, nombre="ITEM 3 SERVICIO EXENTO", cantidad=1, precio_unitario=35119, exento=True),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4753440-7",
            ),
            Referencia(
                nro_linea=2,
                tipo_doc_ref="33",
                folio_ref=str(folio_ref_f3),
                fecha_ref=FECHA_EMISION,
                codigo_ref=1,
                razon_ref="ANULA FACTURA",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4753440_8(folio: int, folio_ref_nc5: int) -> DTE:
    """CASO 8: ND - Anula NC (ref NC caso 5).
    NC caso 5 es CodRef=2 (corrige texto, MntTotal=0), la ND lo anula con MntTotal=0."""
    dte = DTE(
        tipo_dte=56,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="ANULA NOTA DE CREDITO ELECTRONICA", cantidad=0, precio_unitario=0),
        ],
        monto_neto=0,
        monto_total=0,
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4753440-8",
            ),
            Referencia(
                nro_linea=2,
                tipo_doc_ref="61",
                folio_ref=str(folio_ref_nc5),
                fecha_ref=FECHA_EMISION,
                codigo_ref=1,
                razon_ref="ANULA NOTA DE CREDITO ELECTRONICA",
            ),
        ],
    )
    return dte


# ==================== SET GUÍA DE DESPACHO (4753441) ====================

def crear_caso_4753441_1(folio: int) -> DTE:
    """CASO 1: Guía de despacho - Traslado interno entre bodegas."""
    dte = DTE(
        tipo_dte=52,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=EMISOR.copy(),
        tipo_traslado=5,  # Traslado interno
        items=[
            ItemDetalle(nro_linea=1, nombre="ITEM 1", cantidad=53),
            ItemDetalle(nro_linea=2, nombre="ITEM 2", cantidad=48),
            ItemDetalle(nro_linea=3, nombre="ITEM 3", cantidad=17),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4753441-1",
            ),
        ],
    )
    # Receptor = emisor (traslado interno)
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


def crear_caso_4753441_2(folio: int) -> DTE:
    """CASO 2: Guía de despacho - Venta, traslado por emisor al local del cliente."""
    dte = DTE(
        tipo_dte=52,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=RECEPTOR,
        tipo_traslado=1,  # Operación constituye venta
        tipo_despacho=2,  # Por cuenta del emisor a instalaciones del receptor
        items=[
            ItemDetalle(nro_linea=1, nombre="ITEM 1", cantidad=47, precio_unitario=1546),
            ItemDetalle(nro_linea=2, nombre="ITEM 2", cantidad=76, precio_unitario=874),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4753441-2",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4753441_3(folio: int) -> DTE:
    """CASO 3: Guía de despacho - Venta, traslado por cliente."""
    dte = DTE(
        tipo_dte=52,
        folio=folio,
        fecha_emision=FECHA_EMISION,
        emisor=EMISOR,
        receptor=RECEPTOR,
        tipo_traslado=1,  # Operación constituye venta
        tipo_despacho=1,  # Por cuenta del receptor (cliente retira)
        items=[
            ItemDetalle(nro_linea=1, nombre="ITEM 1", cantidad=76, precio_unitario=1026),
            ItemDetalle(nro_linea=2, nombre="ITEM 2", cantidad=71, precio_unitario=1270),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4753441-3",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


# ==================== SET FACTURA EXENTA (4753442) ====================

def crear_caso_4753442_1(folio: int) -> DTE:
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
                cantidad=15, precio_unitario=8462, unidad_medida="Hora",
            ),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4753442-1",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4753442_2(folio: int, folio_ref_exenta1: int) -> DTE:
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
                cantidad=15, precio_unitario=1058, unidad_medida="Hora",
                exento=True,
            ),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4753442-2",
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


def crear_caso_4753442_3(folio: int) -> DTE:
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
                cantidad=1, precio_unitario=420352,
            ),
            ItemDetalle(
                nro_linea=2, nombre="SERV CONSULTORIA GUIA DESPACHO ELECT",
                cantidad=1, precio_unitario=283879,
            ),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4753442-3",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4753442_4(folio: int, folio_ref_exenta3: int) -> DTE:
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
                razon_ref="CASO 4753442-4",
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


def crear_caso_4753442_5(folio: int, folio_ref_nc4: int) -> DTE:
    """CASO 5: ND - Anula NC (ref NC caso 4).
    NC caso 4 es CodRef=2 (MntTotal=0), la ND lo anula con MntTotal=0."""
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
                razon_ref="CASO 4753442-5",
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


def crear_caso_4753442_6(folio: int) -> DTE:
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
                cantidad=1, precio_unitario=376141,
            ),
            ItemDetalle(
                nro_linea=2, nombre="CAPACITACION USO PLC's CNC",
                cantidad=1, precio_unitario=262140,
            ),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4753442-6",
            ),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4753442_7(folio: int, folio_ref_exenta6: int) -> DTE:
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
                cantidad=1, precio_unitario=188070,
                exento=True,
            ),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4753442-7",
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


def crear_caso_4753442_8(folio: int, folio_ref_exenta6: int) -> DTE:
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
                cantidad=1, precio_unitario=52428,
                exento=True,
            ),
        ],
        referencias=[
            Referencia(
                nro_linea=1,
                tipo_doc_ref="SET",
                folio_ref="0",
                fecha_ref=FECHA_EMISION,
                razon_ref="CASO 4753442-8",
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


# ==================== PROCESADOR PRINCIPAL ====================

def procesar_guias():
    """Procesa solo el Set de Guías (4753441) - tenemos folios T52 disponibles."""
    output_dir = settings.OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    caf_manager = CAFManager(settings.CAF_DIR)
    print("CAFs cargados:")
    caf_manager.info()
    print()

    # Saltar folios ya usados/quemados en envíos anteriores
    # T52: F41-43 quemados (old session) + F44-46 quemados (set con reparos)
    FOLIOS_USADOS = {52: 6}
    for tipo_dte, cantidad in FOLIOS_USADOS.items():
        for _ in range(cantidad):
            try:
                caf_manager.siguiente_folio(tipo_dte)
            except ValueError:
                pass

    print("Folios disponibles para guías:")
    folio_actual = caf_manager._folio_actual.get(52, "N/A")
    print(f"  Tipo 52: siguiente folio = {folio_actual}")
    print()

    # ==================== SET GUÍA DE DESPACHO (4753441) ====================
    print("=" * 60)
    print("SET GUIA DE DESPACHO - 4753441")
    print("=" * 60)

    dtes_guias = []

    folio_g1 = caf_manager.siguiente_folio(52)
    guia1 = crear_caso_4753441_1(folio_g1)
    print(f"  Caso 1: Guia {folio_g1} - Traslado interno - Total: {guia1.monto_total}")

    folio_g2 = caf_manager.siguiente_folio(52)
    guia2 = crear_caso_4753441_2(folio_g2)
    print(f"  Caso 2: Guia {folio_g2} - Venta emisor - Neto: {guia2.monto_neto}, Total: {guia2.monto_total}")

    folio_g3 = caf_manager.siguiente_folio(52)
    guia3 = crear_caso_4753441_3(folio_g3)
    print(f"  Caso 3: Guia {folio_g3} - Venta cliente - Neto: {guia3.monto_neto}, Total: {guia3.monto_total}")

    for guia in [guia1, guia2, guia3]:
        caf = caf_manager.obtener_caf(guia.tipo_dte, guia.folio)
        if caf is None:
            print(f"  ERROR: No hay CAF para tipo {guia.tipo_dte} folio {guia.folio}")
            continue
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

    # Guardar datos de guías para Libro de Guías
    guias_data = {
        "guia1": {"folio": folio_g1, "dte": guia1, "tipo_oper": 5},  # Traslado interno
        "guia2": {"folio": folio_g2, "dte": guia2, "tipo_oper": 2},  # Facturada en periodo
        "guia3": {"folio": folio_g3, "dte": guia3, "tipo_oper": 0},  # Anulada
    }

    print(f"\n  Folios utilizados: T52 F{folio_g1}-{folio_g3}")
    print("  NOTA: Ejecutar firmar_set.py para firmar el sobre")

    return guias_data


def procesar_basico(caf_manager=None):
    """Procesa el Set Básico (4753440) con folios T33 F35+, T61 F34+, T56 F22."""
    output_dir = settings.OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    if caf_manager is None:
        caf_manager = CAFManager(settings.CAF_DIR)
        # Setear directamente los folios iniciales (todos los anteriores están quemados)
        # T33: F26-34 quemados → siguiente F35 (CAF 31-36 tiene F35-36, CAF 37-38 tiene F37-38)
        caf_manager._folio_actual[33] = 35
        # T56: F16-21 quemados → siguiente F22 (CAF nuevo 22-22)
        caf_manager._folio_actual[56] = 22
        # T61: F16-21 + F28-33 quemados → siguiente F34 (CAF 28-37 tiene F34-37)
        caf_manager._folio_actual[61] = 34

    print("=" * 60)
    print("SET BASICO - 4753440")
    print("=" * 60)

    dtes = []

    # Caso 1: Factura T33
    f1 = caf_manager.siguiente_folio(33)
    caso1 = crear_caso_4753440_1(f1)
    print(f"  Caso 1: T33 F{f1} - Factura simple - Neto: {caso1.monto_neto}, Total: {caso1.monto_total}")

    # Caso 2: Factura T33
    f2 = caf_manager.siguiente_folio(33)
    caso2 = crear_caso_4753440_2(f2)
    print(f"  Caso 2: T33 F{f2} - Factura con dctos - Neto: {caso2.monto_neto}, Total: {caso2.monto_total}")

    # Caso 3: Factura T33
    f3 = caf_manager.siguiente_folio(33)
    caso3 = crear_caso_4753440_3(f3)
    print(f"  Caso 3: T33 F{f3} - Factura mixta - Neto: {caso3.monto_neto}, Exe: {caso3.monto_exento}, Total: {caso3.monto_total}")

    # Caso 4: Factura T33
    f4 = caf_manager.siguiente_folio(33)
    caso4 = crear_caso_4753440_4(f4)
    print(f"  Caso 4: T33 F{f4} - Factura dcto global - Neto: {caso4.monto_neto}, Exe: {caso4.monto_exento}, Total: {caso4.monto_total}")

    # Caso 5: NC T61 (ref caso 1)
    f5 = caf_manager.siguiente_folio(61)
    caso5 = crear_caso_4753440_5(f5, f1)
    print(f"  Caso 5: T61 F{f5} - NC corrige giro - Total: {caso5.monto_total}")

    # Caso 6: NC T61 (ref caso 2)
    f6 = caf_manager.siguiente_folio(61)
    caso6 = crear_caso_4753440_6(f6, f2, caso2)
    print(f"  Caso 6: T61 F{f6} - NC devolucion - Neto: {caso6.monto_neto}, Total: {caso6.monto_total}")

    # Caso 7: NC T61 (ref caso 3)
    f7 = caf_manager.siguiente_folio(61)
    caso7 = crear_caso_4753440_7(f7, f3, caso3)
    print(f"  Caso 7: T61 F{f7} - NC anula factura - Neto: {caso7.monto_neto}, Exe: {caso7.monto_exento}, Total: {caso7.monto_total}")

    # Caso 8: ND T56 (ref caso 5)
    f8 = caf_manager.siguiente_folio(56)
    caso8 = crear_caso_4753440_8(f8, f5)
    print(f"  Caso 8: T56 F{f8} - ND anula NC - Total: {caso8.monto_total}")

    # Generar XMLs individuales y sobre
    for caso in [caso1, caso2, caso3, caso4, caso5, caso6, caso7, caso8]:
        caf = caf_manager.obtener_caf(caso.tipo_dte, caso.folio)
        if caf is None:
            print(f"  ERROR: No hay CAF para tipo {caso.tipo_dte} folio {caso.folio}")
            return None
        doc_xml = generar_documento_xml(caso, caf, timestamp=TIMESTAMP)
        dte_xml = generar_dte_xml(doc_xml)
        dtes.append(dte_xml)

        filename = f"DTE_T{caso.tipo_dte}_F{caso.folio}.xml"
        filepath = output_dir / "basico" / filename
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "wb") as f:
            f.write(xml_to_string(dte_xml))
        print(f"    -> {filename}")

    envio = generar_envio_dte(
        dtes=dtes,
        rut_emisor=EMISOR["RUTEmisor"],
        rut_envia=RUT_FIRMANTE,
        rut_receptor=settings.RUT_SII,
        fecha_resolucion=FECHA_RESOLUCION,
        nro_resolucion=NUMERO_RESOLUCION,
        timestamp=TIMESTAMP,
    )
    envio_path = output_dir / "EnvioDTE_SetBasico.xml"
    with open(envio_path, "wb") as f:
        f.write(xml_to_string(envio))
    print(f"\n  Sobre: {envio_path.name}")
    print(f"  Folios usados - T33: F{f1},F{f2},F{f3},F{f4} | T61: F{f5},F{f6},F{f7} | T56: F{f8}")

    return {
        "casos": [caso1, caso2, caso3, caso4, caso5, caso6, caso7, caso8],
        "folios": {"f1": f1, "f2": f2, "f3": f3, "f4": f4,
                   "f5": f5, "f6": f6, "f7": f7, "f8": f8},
    }


def procesar_exentas(caf_manager=None):
    """Procesa el Set Factura Exenta (4753442) con nuevos folios T34 F21+, T61, T56."""
    output_dir = settings.OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    if caf_manager is None:
        caf_manager = CAFManager(settings.CAF_DIR)
        # Saltar folios quemados
        for _ in range(5):
            caf_manager.siguiente_folio(33)
        for _ in range(3):
            caf_manager.siguiente_folio(56)
        for _ in range(6):
            caf_manager.siguiente_folio(61)
        # Si se ejecuta solo (sin basico previo), saltar los folios usados por basico
        # T33: +4, T61: +3, T56: +1
        for _ in range(4):
            caf_manager.siguiente_folio(33)
        for _ in range(3):
            caf_manager.siguiente_folio(61)
        for _ in range(1):
            caf_manager.siguiente_folio(56)

    # T34: F11-20 quemados (10 folios de CAF viejo). Nuevos empiezan en F21.
    if caf_manager._folio_actual.get(34, 0) < 21:
        skip = 10 - (caf_manager._folio_actual.get(34, 0) - 11 + 1) if caf_manager._folio_actual.get(34, 0) >= 11 else 10
        for _ in range(skip):
            try:
                caf_manager.siguiente_folio(34)
            except ValueError:
                break

    print(f"\n{'=' * 60}")
    print("SET FACTURA EXENTA - 4753442")
    print("=" * 60)

    dtes = []

    # Caso 1: Factura exenta T34
    fe1 = caf_manager.siguiente_folio(34)
    caso1 = crear_caso_4753442_1(fe1)
    print(f"  Caso 1: T34 F{fe1} - Factura exenta - Exe: {caso1.monto_exento}, Total: {caso1.monto_total}")

    # Caso 2: NC T61 (ref caso 1)
    nc2 = caf_manager.siguiente_folio(61)
    caso2 = crear_caso_4753442_2(nc2, fe1)
    print(f"  Caso 2: T61 F{nc2} - NC modifica monto - Exe: {caso2.monto_exento}, Total: {caso2.monto_total}")

    # Caso 3: Factura exenta T34
    fe3 = caf_manager.siguiente_folio(34)
    caso3 = crear_caso_4753442_3(fe3)
    print(f"  Caso 3: T34 F{fe3} - Factura exenta - Exe: {caso3.monto_exento}, Total: {caso3.monto_total}")

    # Caso 4: NC T61 (ref caso 3) - corrige giro
    nc4 = caf_manager.siguiente_folio(61)
    caso4 = crear_caso_4753442_4(nc4, fe3)
    print(f"  Caso 4: T61 F{nc4} - NC corrige giro - Total: {caso4.monto_total}")

    # Caso 5: ND T56 (ref caso 4) - anula NC
    nd5 = caf_manager.siguiente_folio(56)
    caso5 = crear_caso_4753442_5(nd5, nc4)
    print(f"  Caso 5: T56 F{nd5} - ND anula NC - Total: {caso5.monto_total}")

    # Caso 6: Factura exenta T34
    fe6 = caf_manager.siguiente_folio(34)
    caso6 = crear_caso_4753442_6(fe6)
    print(f"  Caso 6: T34 F{fe6} - Factura exenta - Exe: {caso6.monto_exento}, Total: {caso6.monto_total}")

    # Caso 7: NC T61 (ref caso 6) - modifica monto
    nc7 = caf_manager.siguiente_folio(61)
    caso7 = crear_caso_4753442_7(nc7, fe6)
    print(f"  Caso 7: T61 F{nc7} - NC modifica monto - Exe: {caso7.monto_exento}, Total: {caso7.monto_total}")

    # Caso 8: ND T56 (ref caso 6) - modifica monto
    nd8 = caf_manager.siguiente_folio(56)
    caso8 = crear_caso_4753442_8(nd8, fe6)
    print(f"  Caso 8: T56 F{nd8} - ND modifica monto - Exe: {caso8.monto_exento}, Total: {caso8.monto_total}")

    # Generar XMLs individuales y sobre
    for caso in [caso1, caso2, caso3, caso4, caso5, caso6, caso7, caso8]:
        caf = caf_manager.obtener_caf(caso.tipo_dte, caso.folio)
        if caf is None:
            print(f"  ERROR: No hay CAF para tipo {caso.tipo_dte} folio {caso.folio}")
            return None
        doc_xml = generar_documento_xml(caso, caf, timestamp=TIMESTAMP)
        dte_xml = generar_dte_xml(doc_xml)
        dtes.append(dte_xml)

        filename = f"DTE_T{caso.tipo_dte}_F{caso.folio}.xml"
        filepath = output_dir / "exentas" / filename
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "wb") as f:
            f.write(xml_to_string(dte_xml))
        print(f"    -> {filename}")

    envio = generar_envio_dte(
        dtes=dtes,
        rut_emisor=EMISOR["RUTEmisor"],
        rut_envia=RUT_FIRMANTE,
        rut_receptor=settings.RUT_SII,
        fecha_resolucion=FECHA_RESOLUCION,
        nro_resolucion=NUMERO_RESOLUCION,
        timestamp=TIMESTAMP,
    )
    envio_path = output_dir / "EnvioDTE_SetExentas.xml"
    with open(envio_path, "wb") as f:
        f.write(xml_to_string(envio))
    print(f"\n  Sobre: {envio_path.name}")
    print(f"  Folios T34: F{fe1},F{fe3},F{fe6}, T61: F{nc2},F{nc4},F{nc7}, T56: F{nd5},F{nd8}")

    return {
        "casos": [caso1, caso2, caso3, caso4, caso5, caso6, caso7, caso8],
    }


def procesar_todos():
    """Procesa Set Básico Y Set Exentas en secuencia, compartiendo el CAFManager."""
    caf_manager = CAFManager(settings.CAF_DIR)
    print("CAFs cargados:")
    caf_manager.info()
    print()

    # Saltar folios quemados de CAFs antiguos
    # T33: F26-30 quemados (5 folios)
    for _ in range(5):
        caf_manager.siguiente_folio(33)
    # T34: F11-20 quemados (10 folios)
    for _ in range(10):
        caf_manager.siguiente_folio(34)
    # T56: F16-18 quemados (3 folios)
    for _ in range(3):
        caf_manager.siguiente_folio(56)
    # T61: F16-21 quemados (6 folios)
    for _ in range(6):
        caf_manager.siguiente_folio(61)

    print("Folios disponibles (después de saltar quemados):")
    for t in [33, 34, 56, 61]:
        folio = caf_manager._folio_actual.get(t, "N/A")
        print(f"  T{t}: siguiente = F{folio}")
    print()

    basico_data = procesar_basico(caf_manager)
    exentas_data = procesar_exentas(caf_manager)

    return basico_data, exentas_data


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Procesar nuevo set de pruebas SII")
    parser.add_argument("--set", choices=["guias", "basico", "exentas", "todos"],
                       default="todos", help="Set a procesar (default: todos)")
    args = parser.parse_args()

    if args.set == "guias":
        procesar_guias()
    elif args.set == "basico":
        procesar_basico()
    elif args.set == "exentas":
        procesar_exentas()
    elif args.set == "todos":
        procesar_todos()

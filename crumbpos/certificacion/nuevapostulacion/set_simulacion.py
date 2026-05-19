"""
Set de Simulación — Paso 2 de Certificación SII.

Genera 16 DTEs con datos representativos de operación real
de TRESTRES PUBLICIDAD SPA (panadería/pastelería).

IMPORTANTE: En ambiente de certificación, el receptor DEBE ser
un RUT registrado. Se usa el mismo RUT del emisor (77051056-2).

Distribución: T33×9, T34×3, T52×2, T56×1, T61×1 = 16 DTEs

Los folios se leen de folios_consumidos.json (NUNCA hardcodear valores).
"""
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
FECHA_RESOLUCION = "2026-03-26"
NUMERO_RESOLUCION = 0
RUT_FIRMANTE = "17586255-2"

CAF_DIR = COMPANY_DIR / "CAF"
OUTPUT_DIR = COMPANY_DIR / "output"

# En ambiente de certificación, receptor = emisor (único RUT válido)
RECEPTOR = {
    "RUTRecep": "77051056-2",
    "RznSocRecep": "TRESTRES PUBLICIDAD SPA",
    "GiroRecep": "PASTELERIA Y PANADERIA",
    "DirRecep": "CAMINO DEL ALBA 11969 LT",
    "CmnaRecep": "LAS CONDES",
    "CiudadRecep": "SANTIAGO",
}


# ==================== FACTURAS ELECTRÓNICAS (T33) x9 ====================

def crear_facturas(caf_manager: CAFManager) -> list[DTE]:
    """Genera 9 facturas electrónicas con productos de panadería."""
    facturas = []

    # F56: Pan y pasteles
    facturas.append(DTE(
        tipo_dte=33, folio=caf_manager.siguiente_folio(33),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Pan de molde integral 500g", cantidad=50, precio_unitario=1890),
            ItemDetalle(nro_linea=2, nombre="Croissant mantequilla unidad", cantidad=100, precio_unitario=950),
            ItemDetalle(nro_linea=3, nombre="Torta de chocolate 1kg", cantidad=5, precio_unitario=12500),
        ],
    ))

    # F57: Venta grande
    facturas.append(DTE(
        tipo_dte=33, folio=caf_manager.siguiente_folio(33),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Baguette artesanal unidad", cantidad=200, precio_unitario=1350),
            ItemDetalle(nro_linea=2, nombre="Empanada de pino horneada", cantidad=150, precio_unitario=2200),
            ItemDetalle(nro_linea=3, nombre="Kuchen de nuez porcion", cantidad=80, precio_unitario=3500),
            ItemDetalle(nro_linea=4, nombre="Galleta de avena 100g", cantidad=120, precio_unitario=890),
        ],
    ))

    # F58: Con descuento por ítem
    facturas.append(DTE(
        tipo_dte=33, folio=caf_manager.siguiente_folio(33),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Pan hallulla bolsa 6 un", cantidad=300, precio_unitario=2100, descuento_pct=10),
            ItemDetalle(nro_linea=2, nombre="Pan marraqueta bolsa 6 un", cantidad=400, precio_unitario=1800, descuento_pct=10),
        ],
    ))

    # F59: Berlines y pasteles
    facturas.append(DTE(
        tipo_dte=33, folio=caf_manager.siguiente_folio(33),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Berlines rellenos caja 12 un", cantidad=40, precio_unitario=5400),
            ItemDetalle(nro_linea=2, nombre="Pie de limon entero", cantidad=15, precio_unitario=9800),
            ItemDetalle(nro_linea=3, nombre="Alfajor manjar unidad", cantidad=200, precio_unitario=750),
        ],
    ))

    # F60: Con descuento global
    facturas.append(DTE(
        tipo_dte=33, folio=caf_manager.siguiente_folio(33),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Torta tres leches 1.5kg", cantidad=8, precio_unitario=15900),
            ItemDetalle(nro_linea=2, nombre="Cheesecake frambuesa 1kg", cantidad=6, precio_unitario=14500),
        ],
        descuentos_globales=[
            DescuentoGlobal(
                nro_linea=1, tipo="D",
                descripcion="Descuento cliente frecuente",
                tipo_valor="%", valor=5,
            ),
        ],
    ))

    # F61: Servicio de catering
    facturas.append(DTE(
        tipo_dte=33, folio=caf_manager.siguiente_folio(33),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        indicador_servicio=3,
        items=[
            ItemDetalle(nro_linea=1, nombre="Servicio catering evento 50 personas", cantidad=1, precio_unitario=450000),
        ],
    ))

    # F62: Mixta (afecto + exento)
    facturas.append(DTE(
        tipo_dte=33, folio=caf_manager.siguiente_folio(33),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Pan integral especial 1kg", cantidad=100, precio_unitario=3200),
            ItemDetalle(nro_linea=2, nombre="Envases biodegradables", cantidad=100, precio_unitario=250, exento=True),
        ],
    ))

    # F63: Strudel
    facturas.append(DTE(
        tipo_dte=33, folio=caf_manager.siguiente_folio(33),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Strudel de manzana porcion", cantidad=60, precio_unitario=2800),
        ],
    ))

    # F64: Donas y muffins (para NC)
    facturas.append(DTE(
        tipo_dte=33, folio=caf_manager.siguiente_folio(33),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Dona glaseada unidad", cantidad=200, precio_unitario=890),
            ItemDetalle(nro_linea=2, nombre="Muffin arandano unidad", cantidad=150, precio_unitario=1200),
        ],
    ))

    for f in facturas:
        f.calcular_totales()

    return facturas


# ==================== FACTURAS EXENTAS (T34) x3 ====================

def crear_facturas_exentas(caf_manager: CAFManager) -> list[DTE]:
    """Genera 3 facturas exentas."""
    exentas = []

    exentas.append(DTE(
        tipo_dte=34, folio=caf_manager.siguiente_folio(34),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Diseno packaging caja pasteleria", cantidad=1, precio_unitario=180000),
            ItemDetalle(nro_linea=2, nombre="Diseno etiqueta producto", cantidad=5, precio_unitario=35000),
        ],
    ))

    exentas.append(DTE(
        tipo_dte=34, folio=caf_manager.siguiente_folio(34),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Capacitacion manipulacion alimentos", cantidad=1, precio_unitario=250000),
        ],
    ))

    exentas.append(DTE(
        tipo_dte=34, folio=caf_manager.siguiente_folio(34),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Asesoria desarrollo nuevos productos", cantidad=1, precio_unitario=320000),
        ],
    ))

    for fe in exentas:
        fe.calcular_totales()
    return exentas


# ==================== GUÍAS DE DESPACHO (T52) x2 ====================

def crear_guias(caf_manager: CAFManager) -> list[DTE]:
    """Genera 2 guías de despacho."""
    guias = []

    # G1: Despacho con venta
    guias.append(DTE(
        tipo_dte=52, folio=caf_manager.siguiente_folio(52),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        tipo_despacho=2, tipo_traslado=1,
        items=[
            ItemDetalle(nro_linea=1, nombre="Pan de molde blanco 500g", cantidad=80, precio_unitario=1690),
            ItemDetalle(nro_linea=2, nombre="Rol de canela unidad", cantidad=60, precio_unitario=1450),
        ],
    ))

    # G2: Traslado interno
    guias.append(DTE(
        tipo_dte=52, folio=caf_manager.siguiente_folio(52),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        tipo_despacho=1, tipo_traslado=5,
        items=[
            ItemDetalle(nro_linea=1, nombre="Harina 25kg", cantidad=10, precio_unitario=0, monto_item=0),
            ItemDetalle(nro_linea=2, nombre="Mantequilla 5kg", cantidad=8, precio_unitario=0, monto_item=0),
        ],
    ))

    for g in guias:
        g.calcular_totales()
    return guias


# ==================== NOTA DE CRÉDITO (T61) x1 ====================

def crear_notas_credito(caf_manager: CAFManager, facturas: list[DTE]) -> list[DTE]:
    """Genera 1 nota de crédito referenciando factura de donas y muffins."""
    notas = []

    f_ref = facturas[8]  # 9na factura: donas y muffins
    notas.append(DTE(
        tipo_dte=61, folio=caf_manager.siguiente_folio(61),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Dona glaseada unidad", cantidad=50, precio_unitario=890),
        ],
        referencias=[
            Referencia(
                nro_linea=1, tipo_doc_ref="33",
                folio_ref=str(f_ref.folio),
                fecha_ref=f_ref.fecha_emision,
                codigo_ref=3,
                razon_ref="Devolucion parcial mercaderia",
            ),
        ],
    ))

    for nc in notas:
        nc.calcular_totales()
    return notas


# ==================== NOTA DE DÉBITO (T56) x1 ====================

def crear_notas_debito(caf_manager: CAFManager, facturas: list[DTE]) -> list[DTE]:
    """Genera 1 nota de débito referenciando factura con descuentos."""
    notas_db = []

    f_ref = facturas[2]  # 3ra factura: con descuentos
    notas_db.append(DTE(
        tipo_dte=56, folio=caf_manager.siguiente_folio(56),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Interes por mora 30 dias", cantidad=1, precio_unitario=15000),
        ],
        referencias=[
            Referencia(
                nro_linea=1, tipo_doc_ref="33",
                folio_ref=str(f_ref.folio),
                fecha_ref=f_ref.fecha_emision,
                codigo_ref=3,
                razon_ref="Cobro interes por mora en pago",
            ),
        ],
    ))

    for nd in notas_db:
        nd.calcular_totales()
    return notas_db


# ==================== GENERADOR PRINCIPAL ====================

def generar_set_simulacion():
    """Genera Set de Simulación: 20 DTEs en un solo EnvioDTE."""
    output_dir = OUTPUT_DIR / "simulacion"
    output_dir.mkdir(parents=True, exist_ok=True)

    caf_manager = CAFManager(CAF_DIR)
    # Folios se leen automáticamente de folios_consumidos.json
    # NUNCA hardcodear valores — causaría retroceso y reutilización de folios

    print("=" * 60)
    print("GENERANDO SET DE SIMULACIÓN — Crumb")
    print(f"Fecha: {FECHA_EMISION}")
    print(f"Receptor: 77051056-2 (mismo emisor, certificación)")
    print("=" * 60)

    # Generar documentos
    print("\n--- Facturas Electrónicas (T33) x9 ---")
    facturas = crear_facturas(caf_manager)
    for f in facturas:
        print(f"  F{f.folio}: Neto={f.monto_neto} Exe={f.monto_exento} IVA={f.iva} Total={f.monto_total}")

    print("\n--- Facturas Exentas (T34) x3 ---")
    exentas = crear_facturas_exentas(caf_manager)
    for fe in exentas:
        print(f"  F{fe.folio}: Exe={fe.monto_exento} Total={fe.monto_total}")

    print("\n--- Guías de Despacho (T52) x2 ---")
    guias = crear_guias(caf_manager)
    for g in guias:
        print(f"  F{g.folio}: Traslado={g.tipo_traslado} Neto={g.monto_neto} Total={g.monto_total}")

    print("\n--- Notas de Crédito (T61) x1 ---")
    notas_credito = crear_notas_credito(caf_manager, facturas)
    for nc in notas_credito:
        ref = nc.referencias[0]
        print(f"  F{nc.folio}: Ref T33-F{ref.folio_ref} CodRef={ref.codigo_ref} Total={nc.monto_total}")

    print("\n--- Notas de Débito (T56) x1 ---")
    notas_debito = crear_notas_debito(caf_manager, facturas)
    for nd in notas_debito:
        ref = nd.referencias[0]
        print(f"  F{nd.folio}: Ref T33-F{ref.folio_ref} CodRef={ref.codigo_ref} Total={nd.monto_total}")

    # Juntar todos
    todos_dtes = facturas + exentas + guias + notas_credito + notas_debito
    print(f"\n{'=' * 60}")
    print(f"TOTAL: {len(todos_dtes)} DTEs")
    print(f"  T33: {len(facturas)}")
    print(f"  T34: {len(exentas)}")
    print(f"  T52: {len(guias)}")
    print(f"  T61: {len(notas_credito)}")
    print(f"  T56: {len(notas_debito)}")

    # Generar XMLs
    dtes_xml = []
    for dte_obj in todos_dtes:
        caf = caf_manager.obtener_caf(dte_obj.tipo_dte, dte_obj.folio)
        if caf is None:
            raise ValueError(f"No se encontró CAF para T{dte_obj.tipo_dte} F{dte_obj.folio}")
        doc_xml = generar_documento_xml(dte_obj, caf, TIMESTAMP)
        dte_xml = generar_dte_xml(doc_xml)
        dtes_xml.append(dte_xml)

    # EnvioDTE
    envio = generar_envio_dte(
        dtes=dtes_xml,
        rut_emisor=EMISOR["RUTEmisor"],
        rut_envia=RUT_FIRMANTE,
        rut_receptor="60803000-K",
        fecha_resolucion=FECHA_RESOLUCION,
        nro_resolucion=NUMERO_RESOLUCION,
        timestamp=TIMESTAMP,
    )

    xml_bytes = xml_to_string(envio)
    output_file = output_dir / "SetSimulacion.xml"
    output_file.write_bytes(xml_bytes)
    print(f"\nXML: {output_file}")
    print(f"Tamaño: {len(xml_bytes):,} bytes")
    print(f"{'=' * 60}")

    return str(output_file)


if __name__ == "__main__":
    generar_set_simulacion()

"""
Procesador del Set de Pruebas — GRUPO TRESTRES SPA (77829149-5).
Fecha Resolución: 2026-03-30, Resolución: 0

Números de atención:
- Set Básico:      4757724
- Libro de Ventas: 4757725
- Libro de Compras: 4757726

Solo facturación básica: T33, T61, T56.
No se requieren guías (T52), exentas (T34) ni boletas (T39).
"""
import sys
from datetime import datetime
from pathlib import Path

COMPANY_DIR = Path(__file__).resolve().parent.parent.parent.parent / "grupotrestres"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

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

# === DATOS GRUPO TRESTRES SPA ===
EMISOR = {
    "RUTEmisor": "77829149-5",
    "RznSoc": "GRUPO TRESTRES SPA",
    "GiroEmis": "SERVICIOS DE PUBLICIDAD Y MARKETING",
    "Acteco": 731001,
    "DirOrigen": "Los Militares 5620 OF 905 PS 9",
    "CmnaOrigen": "Las Condes",
    "CiudadOrigen": "Santiago",
}

RECEPTOR = {
    "RUTRecep": "77829149-5",
    "RznSocRecep": "GRUPO TRESTRES SPA",
    "GiroRecep": "SERVICIOS DE PUBLICIDAD Y MARKETING",
    "DirRecep": "Los Militares 5620 OF 905 PS 9",
    "CmnaRecep": "Las Condes",
    "CiudadRecep": "Santiago",
}

RUT_FIRMANTE = "17586255-2"
RUT_SII = "60803000-K"
FECHA_RESOLUCION = "2026-03-30"
NUMERO_RESOLUCION = 0

CAF_DIR = COMPANY_DIR / "CAF"
OUTPUT_DIR = COMPANY_DIR / "output"

# Folios T33 disponibles (nunca recibidos por el SII).
# No usar siguiente_folio() para T33 — el CAFManager es secuencial
# y los folios disponibles no son consecutivos.
FOLIOS_T33_DISPONIBLES = [9, 14, 15, 16, 17, 27]


# ==================== SET BÁSICO (4757724) ====================

def crear_caso_4757724_1(folio: int) -> DTE:
    """CASO 1: Factura electrónica simple."""
    dte = DTE(
        tipo_dte=33, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Cajón AFECTO", cantidad=168, precio_unitario=3507),
            ItemDetalle(nro_linea=2, nombre="Relleno AFECTO", cantidad=71, precio_unitario=5841),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4757724-1"),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4757724_2(folio: int) -> DTE:
    """CASO 2: Factura electrónica con descuentos por ítem."""
    dte = DTE(
        tipo_dte=33, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Pañuelo AFECTO",
                        cantidad=762, precio_unitario=5900, descuento_pct=10),
            ItemDetalle(nro_linea=2, nombre="ITEM 2 AFECTO",
                        cantidad=707, precio_unitario=4951, descuento_pct=23),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4757724-2"),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4757724_3(folio: int) -> DTE:
    """CASO 3: Factura electrónica con ítem exento (servicio)."""
    dte = DTE(
        tipo_dte=33, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Pintura B&W AFECTO", cantidad=64, precio_unitario=6896),
            ItemDetalle(nro_linea=2, nombre="ITEM 2 AFECTO", cantidad=237, precio_unitario=4029),
            ItemDetalle(nro_linea=3, nombre="ITEM 3 SERVICIO EXENTO", cantidad=1, precio_unitario=35296, exento=True),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4757724-3"),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4757724_4(folio: int) -> DTE:
    """CASO 4: Factura electrónica con descuento global 22% sobre afectos."""
    dte = DTE(
        tipo_dte=33, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="ITEM 1 AFECTO", cantidad=416, precio_unitario=5946),
            ItemDetalle(nro_linea=2, nombre="ITEM 2 AFECTO", cantidad=176, precio_unitario=7242),
            ItemDetalle(nro_linea=3, nombre="ITEM 3 SERVICIO EXENTO", cantidad=2, precio_unitario=6833, exento=True),
        ],
        descuentos_globales=[
            DescuentoGlobal(
                nro_linea=1, tipo="D",
                descripcion="Descuento global 22%",
                tipo_valor="%", valor=22,
            ),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4757724-4"),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4757724_5(folio: int, folio_ref_f1: int) -> DTE:
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
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4757724-5"),
            Referencia(nro_linea=2, tipo_doc_ref="33", folio_ref=str(folio_ref_f1),
                       fecha_ref=FECHA_EMISION, codigo_ref=2,
                       razon_ref="CORRIGE GIRO DEL RECEPTOR"),
        ],
    )
    return dte


def crear_caso_4757724_6(folio: int, folio_ref_f2: int) -> DTE:
    """CASO 6: NC - Devolución de mercaderías (ref factura caso 2).
    Devuelve 280 de Pañuelo y 479 de ITEM 2 con mismos precios y descuentos."""
    dte = DTE(
        tipo_dte=61, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Pañuelo AFECTO",
                        cantidad=280, precio_unitario=5900, descuento_pct=10),
            ItemDetalle(nro_linea=2, nombre="ITEM 2 AFECTO",
                        cantidad=479, precio_unitario=4951, descuento_pct=23),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4757724-6"),
            Referencia(nro_linea=2, tipo_doc_ref="33", folio_ref=str(folio_ref_f2),
                       fecha_ref=FECHA_EMISION, codigo_ref=3,
                       razon_ref="DEVOLUCION DE MERCADERIAS"),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4757724_7(folio: int, folio_ref_f3: int) -> DTE:
    """CASO 7: NC - Anula factura completa (ref factura caso 3).
    CodRef=1: anula el documento completo. Montos = mismos que factura original."""
    dte = DTE(
        tipo_dte=61, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Pintura B&W AFECTO", cantidad=64, precio_unitario=6896),
            ItemDetalle(nro_linea=2, nombre="ITEM 2 AFECTO", cantidad=237, precio_unitario=4029),
            ItemDetalle(nro_linea=3, nombre="ITEM 3 SERVICIO EXENTO", cantidad=1, precio_unitario=35296, exento=True),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4757724-7"),
            Referencia(nro_linea=2, tipo_doc_ref="33", folio_ref=str(folio_ref_f3),
                       fecha_ref=FECHA_EMISION, codigo_ref=1,
                       razon_ref="ANULA FACTURA"),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso_4757724_8(folio: int, folio_ref_nc5: int) -> DTE:
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
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4757724-8"),
            Referencia(nro_linea=2, tipo_doc_ref="61", folio_ref=str(folio_ref_nc5),
                       fecha_ref=FECHA_EMISION, codigo_ref=1,
                       razon_ref="ANULA NOTA DE CREDITO ELECTRONICA"),
        ],
    )
    return dte


# ==================== PROCESADORES ====================

def _generar_y_guardar(casos: list, caf_manager, subdir: str, sobre_nombre: str) -> list:
    """Genera XMLs individuales y el sobre EnvioDTE."""
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

    envio = generar_envio_dte(
        dtes=dtes_xml,
        rut_emisor=EMISOR["RUTEmisor"],
        rut_envia=RUT_FIRMANTE,
        rut_receptor=RUT_SII,
        fecha_resolucion=FECHA_RESOLUCION,
        nro_resolucion=NUMERO_RESOLUCION,
        timestamp=TIMESTAMP,
    )

    envio_path = OUTPUT_DIR / sobre_nombre
    with open(envio_path, "wb") as f:
        f.write(xml_to_string(envio))
    print(f"  Sobre: {envio_path.name}")

    return dtes_xml


def _siguiente_t33(folios_pendientes: list, caf_manager) -> int:
    """Toma el siguiente folio T33 de la lista de disponibles.
    Verifica que existe CAF para ese folio antes de retornarlo."""
    if not folios_pendientes:
        raise ValueError("No quedan folios T33 disponibles en FOLIOS_T33_DISPONIBLES")
    folio = folios_pendientes.pop(0)
    caf = caf_manager.obtener_caf(33, folio)
    if caf is None:
        raise ValueError(f"No hay CAF cargado para T33 F{folio}. Verifica el directorio CAF/")
    print(f"    [T33] Usando folio F{folio} (CAF: {caf.folio_desde}-{caf.folio_hasta})")
    return folio


def procesar_basico(caf_manager):
    """Procesa el Set Básico (4757724)."""
    print("=" * 60)
    print("SET BÁSICO — 4757724")
    print("=" * 60)

    folios_t33 = list(FOLIOS_T33_DISPONIBLES)  # copia local para no mutar la lista global

    f1 = _siguiente_t33(folios_t33, caf_manager)
    caso1 = crear_caso_4757724_1(f1)
    print(f"  Caso 1: T33 F{f1} — Neto: {caso1.monto_neto}, Total: {caso1.monto_total}")

    f2 = _siguiente_t33(folios_t33, caf_manager)
    caso2 = crear_caso_4757724_2(f2)
    print(f"  Caso 2: T33 F{f2} — Neto: {caso2.monto_neto}, Total: {caso2.monto_total}")

    f3 = _siguiente_t33(folios_t33, caf_manager)
    caso3 = crear_caso_4757724_3(f3)
    print(f"  Caso 3: T33 F{f3} — Neto: {caso3.monto_neto}, Exe: {caso3.monto_exento}, Total: {caso3.monto_total}")

    f4 = _siguiente_t33(folios_t33, caf_manager)
    caso4 = crear_caso_4757724_4(f4)
    print(f"  Caso 4: T33 F{f4} — Neto: {caso4.monto_neto}, Exe: {caso4.monto_exento}, Total: {caso4.monto_total}")

    f5 = caf_manager.siguiente_folio(61)
    caso5 = crear_caso_4757724_5(f5, f1)
    print(f"  Caso 5: T61 F{f5} — NC corrige giro — Total: {caso5.monto_total}")

    f6 = caf_manager.siguiente_folio(61)
    caso6 = crear_caso_4757724_6(f6, f2)
    print(f"  Caso 6: T61 F{f6} — NC devolución — Neto: {caso6.monto_neto}, Total: {caso6.monto_total}")

    f7 = caf_manager.siguiente_folio(61)
    caso7 = crear_caso_4757724_7(f7, f3)
    print(f"  Caso 7: T61 F{f7} — NC anula factura — Neto: {caso7.monto_neto}, Exe: {caso7.monto_exento}, Total: {caso7.monto_total}")

    f8 = caf_manager.siguiente_folio(56)
    caso8 = crear_caso_4757724_8(f8, f5)
    print(f"  Caso 8: T56 F{f8} — ND anula NC — Total: {caso8.monto_total}")

    casos = [caso1, caso2, caso3, caso4, caso5, caso6, caso7, caso8]
    _generar_y_guardar(casos, caf_manager, "basico", "EnvioDTE_SetBasico.xml")

    print(f"  Folios usados: T33 F{f1}-F{f4} | T61 F{f5}-F{f7} | T56 F{f8}")
    return {
        "casos": casos,
        "folios": {
            "f1": f1, "f2": f2, "f3": f3, "f4": f4,
            "f5": f5, "f6": f6, "f7": f7, "f8": f8,
        }
    }


def procesar_todos():
    """Procesa el set de pruebas completo."""
    caf_manager = CAFManager(CAF_DIR)
    print("CAFs cargados:")
    caf_manager.info()
    print()

    print("Folios disponibles:")
    for t in [33, 56, 61]:
        folio = caf_manager._folio_actual.get(t, "N/A")
        print(f"  T{t}: siguiente = F{folio}")
    print()

    basico_data = procesar_basico(caf_manager)

    print(f"\n{'=' * 60}")
    print("SET PROCESADO CORRECTAMENTE")
    print(f"{'=' * 60}")
    print("Siguiente paso: ejecutar firmar_set.py")

    return basico_data


if __name__ == "__main__":
    procesar_todos()

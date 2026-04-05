"""
Set de Simulación — GRUPO TRESTRES SPA (77829149-5).

Genera DTEs con datos representativos de operación real
de GRUPO TRESTRES SPA (publicidad y marketing).

Receptor = mismo emisor (certificación).

Distribución: T33×9, T34×3, T52×2, T56×1, T61×1 = 16 DTEs
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

EMISOR = {
    "RUTEmisor": "77829149-5",
    "RznSoc": "GRUPO TRESTRES SPA",
    "GiroEmis": "SERVICIOS DE PUBLICIDAD Y MARKETING",
    "Acteco": 731001,
    "DirOrigen": "Los Militares 5620 OF 905 PS 9",
    "CmnaOrigen": "Las Condes",
    "CiudadOrigen": "Santiago",
}

RUT_FIRMANTE = "17586255-2"
FECHA_RESOLUCION = "2026-03-30"
NUMERO_RESOLUCION = 0

CAF_DIR = COMPANY_DIR / "CAF"
OUTPUT_DIR = COMPANY_DIR / "output"

# En ambiente de certificación, receptor = emisor
RECEPTOR = {
    "RUTRecep": "77829149-5",
    "RznSocRecep": "GRUPO TRESTRES SPA",
    "GiroRecep": "SERVICIOS DE PUBLICIDAD Y MARKETING",
    "DirRecep": "Los Militares 5620 OF 905 PS 9",
    "CmnaRecep": "Las Condes",
    "CiudadRecep": "Santiago",
}


# ==================== FACTURAS ELECTRÓNICAS (T33) x9 ====================

def crear_facturas(caf_manager: CAFManager) -> list:
    facturas = []

    # Campaña publicitaria
    facturas.append(DTE(
        tipo_dte=33, folio=caf_manager.siguiente_folio(33),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Campaña publicitaria digital mensual", cantidad=1, precio_unitario=850000),
            ItemDetalle(nro_linea=2, nombre="Diseño piezas graficas redes sociales", cantidad=10, precio_unitario=35000),
            ItemDetalle(nro_linea=3, nombre="Producción video promocional 30s", cantidad=2, precio_unitario=180000),
        ],
    ))

    # Servicio de branding
    facturas.append(DTE(
        tipo_dte=33, folio=caf_manager.siguiente_folio(33),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Desarrollo identidad corporativa", cantidad=1, precio_unitario=1200000),
            ItemDetalle(nro_linea=2, nombre="Manual de marca y aplicaciones", cantidad=1, precio_unitario=450000),
            ItemDetalle(nro_linea=3, nombre="Diseño papeleria corporativa", cantidad=1, precio_unitario=280000),
            ItemDetalle(nro_linea=4, nombre="Adaptación logo formatos digitales", cantidad=5, precio_unitario=45000),
        ],
    ))

    # Con descuento por ítem
    facturas.append(DTE(
        tipo_dte=33, folio=caf_manager.siguiente_folio(33),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Community management mensual", cantidad=3, precio_unitario=320000, descuento_pct=10),
            ItemDetalle(nro_linea=2, nombre="Pauta publicitaria Meta Ads", cantidad=3, precio_unitario=250000, descuento_pct=15),
        ],
    ))

    # Producción audiovisual
    facturas.append(DTE(
        tipo_dte=33, folio=caf_manager.siguiente_folio(33),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Producción spot TV 30 segundos", cantidad=1, precio_unitario=2500000),
            ItemDetalle(nro_linea=2, nombre="Edición y postproducción video", cantidad=1, precio_unitario=800000),
            ItemDetalle(nro_linea=3, nombre="Locución profesional", cantidad=1, precio_unitario=150000),
        ],
    ))

    # Con descuento global
    facturas.append(DTE(
        tipo_dte=33, folio=caf_manager.siguiente_folio(33),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Estrategia marketing digital anual", cantidad=1, precio_unitario=3600000),
            ItemDetalle(nro_linea=2, nombre="Consultoria posicionamiento SEO", cantidad=1, precio_unitario=1200000),
        ],
        descuentos_globales=[
            DescuentoGlobal(
                nro_linea=1, tipo="D",
                descripcion="Descuento contrato anual",
                tipo_valor="%", valor=8,
            ),
        ],
    ))

    # Servicio de evento
    facturas.append(DTE(
        tipo_dte=33, folio=caf_manager.siguiente_folio(33),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        indicador_servicio=3,
        items=[
            ItemDetalle(nro_linea=1, nombre="Producción evento corporativo", cantidad=1, precio_unitario=4500000),
        ],
    ))

    # Mixta (afecto + exento)
    facturas.append(DTE(
        tipo_dte=33, folio=caf_manager.siguiente_folio(33),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Diseño stand feria comercial", cantidad=1, precio_unitario=950000),
            ItemDetalle(nro_linea=2, nombre="Inscripción feria Expomarketing 2026", cantidad=1, precio_unitario=180000, exento=True),
        ],
    ))

    # Impresos
    facturas.append(DTE(
        tipo_dte=33, folio=caf_manager.siguiente_folio(33),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Impresión catálogo 32 paginas", cantidad=500, precio_unitario=2800),
        ],
    ))

    # Para NC después
    facturas.append(DTE(
        tipo_dte=33, folio=caf_manager.siguiente_folio(33),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Gestión redes sociales mensual", cantidad=2, precio_unitario=380000),
            ItemDetalle(nro_linea=2, nombre="Creación contenido fotografico", cantidad=1, precio_unitario=250000),
        ],
    ))

    for f in facturas:
        f.calcular_totales()
    return facturas


# ==================== FACTURAS EXENTAS (T34) x3 ====================

def crear_facturas_exentas(caf_manager: CAFManager) -> list:
    exentas = []

    exentas.append(DTE(
        tipo_dte=34, folio=caf_manager.siguiente_folio(34),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Consultoria estrategia comunicacional", cantidad=1, precio_unitario=480000),
            ItemDetalle(nro_linea=2, nombre="Informe analisis de mercado", cantidad=1, precio_unitario=320000),
        ],
    ))

    exentas.append(DTE(
        tipo_dte=34, folio=caf_manager.siguiente_folio(34),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Capacitacion marketing digital", cantidad=1, precio_unitario=350000),
        ],
    ))

    exentas.append(DTE(
        tipo_dte=34, folio=caf_manager.siguiente_folio(34),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Asesoria plan de medios anual", cantidad=1, precio_unitario=750000),
        ],
    ))

    for fe in exentas:
        fe.calcular_totales()
    return exentas


# ==================== GUÍAS DE DESPACHO (T52) x2 ====================

def crear_guias(caf_manager: CAFManager) -> list:
    guias = []

    # Despacho con venta
    guias.append(DTE(
        tipo_dte=52, folio=caf_manager.siguiente_folio(52),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        tipo_despacho=2, tipo_traslado=1,
        items=[
            ItemDetalle(nro_linea=1, nombre="Material POP exhibidor", cantidad=20, precio_unitario=45000),
            ItemDetalle(nro_linea=2, nombre="Pendones impresos 2x1m", cantidad=10, precio_unitario=32000),
        ],
    ))

    # Traslado interno
    guias.append(DTE(
        tipo_dte=52, folio=caf_manager.siguiente_folio(52),
        fecha_emision=FECHA_EMISION, emisor=EMISOR,
        receptor={
            "RUTRecep": EMISOR["RUTEmisor"],
            "RznSocRecep": EMISOR["RznSoc"],
            "GiroRecep": EMISOR["GiroEmis"],
            "DirRecep": EMISOR["DirOrigen"],
            "CmnaRecep": EMISOR["CmnaOrigen"],
            "CiudadRecep": EMISOR["CiudadOrigen"],
        },
        tipo_despacho=1, tipo_traslado=5,
        items=[
            ItemDetalle(nro_linea=1, nombre="Equipos audiovisuales", cantidad=3, precio_unitario=0, monto_item=0),
            ItemDetalle(nro_linea=2, nombre="Estructuras montaje evento", cantidad=5, precio_unitario=0, monto_item=0),
        ],
    ))

    for g in guias:
        g.calcular_totales()
    return guias


# ==================== NOTA DE CRÉDITO (T61) x1 ====================

def crear_notas_credito(caf_manager: CAFManager, facturas: list) -> list:
    notas = []

    f_ref = facturas[8]  # 9na factura: redes sociales
    notas.append(DTE(
        tipo_dte=61, folio=caf_manager.siguiente_folio(61),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Gestión redes sociales mensual", cantidad=1, precio_unitario=380000),
        ],
        referencias=[
            Referencia(
                nro_linea=1, tipo_doc_ref="33",
                folio_ref=str(f_ref.folio),
                fecha_ref=f_ref.fecha_emision,
                codigo_ref=3,
                razon_ref="Devolucion parcial servicio no prestado",
            ),
        ],
    ))

    for nc in notas:
        nc.calcular_totales()
    return notas


# ==================== NOTA DE DÉBITO (T56) x1 ====================

def crear_notas_debito(caf_manager: CAFManager, facturas: list) -> list:
    notas_db = []

    f_ref = facturas[2]  # 3ra factura: con descuentos
    notas_db.append(DTE(
        tipo_dte=56, folio=caf_manager.siguiente_folio(56),
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(nro_linea=1, nombre="Interes por mora 30 dias", cantidad=1, precio_unitario=25000),
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
    """Genera Set de Simulación en un solo EnvioDTE."""
    output_dir = OUTPUT_DIR / "simulacion"
    output_dir.mkdir(parents=True, exist_ok=True)

    caf_manager = CAFManager(CAF_DIR)

    print("=" * 60)
    print("GENERANDO SET DE SIMULACIÓN — GRUPO TRESTRES SPA")
    print(f"Fecha: {FECHA_EMISION}")
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

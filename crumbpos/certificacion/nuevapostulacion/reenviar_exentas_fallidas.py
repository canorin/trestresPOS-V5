"""
Re-emisión de los 3 casos fallidos del SET EXENTA (4758677).

Casos rechazados (detalle no cuadra):
- Caso 2: T61 NC modifica monto → faltaba QtyItem/PrcItem + monto incorrecto
- Caso 7: T61 NC modifica monto → faltaba QtyItem/PrcItem
- Caso 8: T56 ND modifica monto → faltaba QtyItem/PrcItem

Referencias a folios originales ya aceptados:
- Caso 2 → ref T34 F52 (caso 1)
- Caso 7 → ref T34 F54 (caso 6)
- Caso 8 → ref T34 F54 (caso 6)
"""
import sys
import base64
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from lxml import etree
from crumbpos.config import settings
from crumbpos.core.caf.caf_manager import CAFManager
from crumbpos.core.dte.generador_xml import (
    generar_documento_xml,
    generar_dte_xml,
    generar_envio_dte,
    xml_to_string,
)
from crumbpos.core.firma.firma_digital import cargar_certificado_pfx
from crumbpos.core.sii_client.autenticacion import obtener_token
from crumbpos.core.sii_client.envio import enviar_dte
from crumbpos.models.dte_models import DTE, ItemDetalle, Referencia
from facturacion_electronica.firma import Firma


# --- Configuración ---
FECHA_EMISION = datetime.now().strftime("%Y-%m-%d")
TIMESTAMP = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

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
    "GiroRecep": "SERVICIOS INTEGRALES DE PUBLICIDAD",
    "DirRecep": "LAS VERBENAS 9069",
    "CmnaRecep": "LA FLORIDA",
    "CiudadRecep": "SANTIAGO",
}

FECHA_RESOLUCION = "2026-03-30"
NUMERO_RESOLUCION = 0
RUT_FIRMANTE = "17586255-2"
RUT_EMISOR = "77051056-2"

PFX_PATH = settings.CERT_DIR / "17586255-2.pfx"
PFX_PASSWORD = "2656"

COMPANY_DIR = Path(__file__).resolve().parent.parent.parent.parent / "nuevapostulacion"
CAF_DIR = COMPANY_DIR / "CAF"
OUTPUT_DIR = settings.OUTPUT_DIR

SII_NS = "http://www.sii.cl/SiiDte"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"


# --- Folios de referencia (originales ya aceptados) ---
FOLIO_CASO1_T34 = 52   # Caso 1: T34 F52 (factura exenta original)
FOLIO_CASO6_T34 = 54   # Caso 6: T34 F54 (factura exenta original)


def crear_caso2_nc(folio: int) -> DTE:
    """CASO 4758677-2: NC modifica monto ref caso 1.
    Original: 8 horas a 4951 = 39608. Nuevo valor unitario: 619."""
    dte = DTE(
        tipo_dte=61, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(
                nro_linea=1, nombre="HORAS PROGRAMADOR",
                cantidad=8, precio_unitario=619, unidad_medida="Hora",
                exento=True,
            ),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4758677-2"),
            Referencia(nro_linea=2, tipo_doc_ref="34",
                       folio_ref=str(FOLIO_CASO1_T34),
                       fecha_ref=FECHA_EMISION, codigo_ref=3,
                       razon_ref="MODIFICA MONTO"),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso7_nc(folio: int) -> DTE:
    """CASO 4758677-7: NC modifica monto ref caso 6.
    Original: CAPACITACION USO CIGUEÑALES qty 1 @ 314752. Nuevo valor: 157376."""
    dte = DTE(
        tipo_dte=61, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(
                nro_linea=1, nombre="CAPACITACION USO CIGUEÑALES",
                cantidad=1, precio_unitario=157376,
                exento=True,
            ),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4758677-7"),
            Referencia(nro_linea=2, tipo_doc_ref="34",
                       folio_ref=str(FOLIO_CASO6_T34),
                       fecha_ref=FECHA_EMISION, codigo_ref=3,
                       razon_ref="MODIFICA MONTO"),
        ],
    )
    dte.calcular_totales()
    return dte


def crear_caso8_nd(folio: int) -> DTE:
    """CASO 4758677-8: ND modifica monto ref caso 6.
    Original: CAPACITACION USO PLC's CNC qty 1 @ 208306. Nuevo valor: 41661."""
    dte = DTE(
        tipo_dte=56, folio=folio,
        fecha_emision=FECHA_EMISION, emisor=EMISOR, receptor=RECEPTOR,
        items=[
            ItemDetalle(
                nro_linea=1, nombre="CAPACITACION USO PLC's CNC",
                cantidad=1, precio_unitario=41661,
                exento=True,
            ),
        ],
        referencias=[
            Referencia(nro_linea=1, tipo_doc_ref="SET", folio_ref="0",
                       fecha_ref=FECHA_EMISION, razon_ref="CASO 4758677-8"),
            Referencia(nro_linea=2, tipo_doc_ref="34",
                       folio_ref=str(FOLIO_CASO6_T34),
                       fecha_ref=FECHA_EMISION, codigo_ref=3,
                       razon_ref="MODIFICA MONTO"),
        ],
    )
    dte.calcular_totales()
    return dte


def limpiar_namespaces(xml_str: str) -> str:
    """Elimina xmlns:xsi y xsi:schemaLocation heredados."""
    import re
    xml_str = re.sub(r'\s+xmlns:xsi="[^"]*"', '', xml_str)
    xml_str = re.sub(r'\s+xsi:schemaLocation="[^"]*"', '', xml_str)
    return xml_str


def main():
    print("=" * 60)
    print("RE-EMISIÓN SET EXENTA — Casos 2, 7, 8")
    print("=" * 60)

    # Cargar CAF manager
    caf_manager = CAFManager(CAF_DIR)
    print("\nFolios disponibles:")
    for t in [56, 61]:
        folio = caf_manager._folio_actual.get(t, "N/A")
        print(f"  T{t}: siguiente = F{folio}")

    # Asignar folios
    f_caso2 = caf_manager.siguiente_folio(61)
    f_caso7 = caf_manager.siguiente_folio(61)
    f_caso8 = caf_manager.siguiente_folio(56)

    print(f"\nFolios asignados:")
    print(f"  Caso 2: T61 F{f_caso2}")
    print(f"  Caso 7: T61 F{f_caso7}")
    print(f"  Caso 8: T56 F{f_caso8}")

    # Crear DTEs
    caso2 = crear_caso2_nc(f_caso2)
    caso7 = crear_caso7_nc(f_caso7)
    caso8 = crear_caso8_nd(f_caso8)

    print(f"\nMontos calculados:")
    print(f"  Caso 2: T61 F{f_caso2} - Exe: {caso2.monto_exento}, Total: {caso2.monto_total}")
    print(f"  Caso 7: T61 F{f_caso7} - Exe: {caso7.monto_exento}, Total: {caso7.monto_total}")
    print(f"  Caso 8: T56 F{f_caso8} - Exe: {caso8.monto_exento}, Total: {caso8.monto_total}")

    # Generar XMLs
    print(f"\nGenerando XMLs...")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_exentas = OUTPUT_DIR / "exentas_reenvio"
    out_exentas.mkdir(parents=True, exist_ok=True)

    dtes_xml = []
    for caso in [caso2, caso7, caso8]:
        caf = caf_manager.obtener_caf(caso.tipo_dte, caso.folio)
        if caf is None:
            print(f"  ERROR: No hay CAF para T{caso.tipo_dte} F{caso.folio}")
            return
        doc_xml = generar_documento_xml(caso, caf, timestamp=TIMESTAMP)
        dte_xml = generar_dte_xml(doc_xml)
        dtes_xml.append(dte_xml)

        filename = f"DTE_T{caso.tipo_dte}_F{caso.folio}.xml"
        filepath = out_exentas / filename
        with open(filepath, "wb") as f:
            f.write(xml_to_string(dte_xml))
        print(f"    -> {filename}")

    # Generar sobre EnvioDTE
    envio = generar_envio_dte(
        dtes=dtes_xml,
        rut_emisor=EMISOR["RUTEmisor"],
        rut_envia=RUT_FIRMANTE,
        rut_receptor=settings.RUT_SII,
        fecha_resolucion=FECHA_RESOLUCION,
        nro_resolucion=NUMERO_RESOLUCION,
        timestamp=TIMESTAMP,
    )

    sobre_path = OUTPUT_DIR / "EnvioDTE_SetExentas_reenvio.xml"
    with open(sobre_path, "wb") as f:
        f.write(xml_to_string(envio))
    print(f"  Sobre: {sobre_path.name}")

    # Firmar
    print(f"\nFirmando...")
    pfx_data = open(PFX_PATH, "rb").read()
    firma = Firma({
        "string_firma": base64.b64encode(pfx_data).decode(),
        "string_password": PFX_PASSWORD,
        "init_signature": True,
        "rut_firmante": RUT_FIRMANTE,
    })
    if not firma.firma_electronica:
        raise RuntimeError(f"Error cargando certificado: {firma.errores}")
    firma.verify = False

    # Parsear el sobre y firmar
    tree = etree.parse(str(sobre_path))
    root = tree.getroot()

    set_dte = None
    for child in root:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == "SetDTE":
            set_dte = child
            break

    caratula = None
    dtes_elements = []
    for child in list(set_dte):
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == "Caratula":
            caratula = child
        elif tag == "DTE":
            dtes_elements.append(child)

    # Firmar cada DTE individual
    import re
    signed_dtes = []
    for dte_el in dtes_elements:
        doc_el = None
        for child in dte_el:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "Documento":
                doc_el = child
                break
        doc_id = doc_el.get("ID", "")
        doc_str = etree.tostring(doc_el, encoding="ISO-8859-1", xml_declaration=False).decode("ISO-8859-1")
        doc_str = limpiar_namespaces(doc_str)
        doc_str = re.sub(r'\s+xmlns=""', '', doc_str)
        doc_str = re.sub(r'\s+xmlns="http://www\.sii\.cl/SiiDte"', '', doc_str)
        dte_str = f'<DTE xmlns="{SII_NS}" version="1.0">{doc_str}</DTE>'
        signed_dte = firma.firmar(dte_str, doc_id, type="doc")
        if not signed_dte:
            raise RuntimeError(f"Error firmando DTE {doc_id}")
        signed_dtes.append(signed_dte)
        print(f"    DTE {doc_id} firmado OK")

    # Firmar el sobre
    car_str = etree.tostring(caratula, encoding="ISO-8859-1", xml_declaration=False).decode("ISO-8859-1")
    car_str = limpiar_namespaces(car_str)
    car_str = re.sub(r'\s+xmlns="[^"]*"', '', car_str)
    car_str = re.sub(r'\s+xmlns=""', '', car_str)

    schema_loc = f"{SII_NS} EnvioDTE_v10.xsd"
    env_str = (
        f'<EnvioDTE xmlns="{SII_NS}" '
        f'xmlns:xsi="{XSI_NS}" '
        f'xsi:schemaLocation="{schema_loc}" '
        f'version="1.0">'
        f'<SetDTE ID="SetDoc">'
        f'{car_str}'
        f'{"".join(signed_dtes)}'
        f'</SetDTE>'
        f'</EnvioDTE>'
    )

    signed_env = firma.firmar(env_str, "SetDoc", type="env")
    if not signed_env:
        raise RuntimeError("Error firmando sobre")

    final_xml = '<?xml version="1.0" encoding="ISO-8859-1"?>\n' + signed_env

    firmado_path = OUTPUT_DIR / "EnvioDTE_SetExentas_reenvio_firmado.xml"
    with open(firmado_path, "w", encoding="ISO-8859-1") as f:
        f.write(final_xml)
    print(f"  Sobre firmado: {firmado_path.name}")

    # Enviar al SII
    print(f"\nEnviando al SII...")
    private_key_pem, cert_pem, cert_der = cargar_certificado_pfx(str(PFX_PATH), PFX_PASSWORD)
    token = obtener_token(private_key_pem, cert_der)
    print(f"  Token: {token}")

    with open(firmado_path, "rb") as f:
        xml_bytes = f.read()

    resultado = enviar_dte(
        xml_bytes=xml_bytes,
        token=token,
        rut_emisor=RUT_EMISOR,
        rut_envia=RUT_FIRMANTE,
    )

    print(f"\n{'=' * 60}")
    print(f"RESULTADO ENVÍO")
    print(f"{'=' * 60}")
    print(f"  Status: {resultado['status']}")
    print(f"  TrackID: {resultado['track_id']}")
    print(f"  Glosa: {resultado['glosa']}")
    print(f"\n  Folios usados: T61 F{f_caso2}, T61 F{f_caso7}, T56 F{f_caso8}")
    print(f"  Refs: Caso2→T34 F{FOLIO_CASO1_T34} | Caso7→T34 F{FOLIO_CASO6_T34} | Caso8→T34 F{FOLIO_CASO6_T34}")


if __name__ == "__main__":
    main()

"""
Firma todos los XMLs del set de pruebas — Nueva Postulación trestresPOS.

Proceso:
1. Cargar certificado PFX
2. Para cada sobre (EnvioDTE/EnvioBOLETA):
   a. Extraer cada DTE, limpiar namespaces heredados, firmar individualmente
   b. Reconstruir el sobre con DTEs firmados
   c. Firmar el SetDTE del sobre
3. Guardar el sobre firmado listo para enviar al SII
"""
import sys
import re
import base64
from pathlib import Path

COMPANY_DIR = Path(__file__).resolve().parent.parent.parent.parent / "nuevapostulacion"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from lxml import etree
from crumbpos.core.firma.sii_firma import Firma
from crumbpos.config import settings


PFX_PATH = settings.CERT_DIR / "17586255-2.pfx"
PFX_PASSWORD = "2656"
RUT_FIRMANTE = "17586255-2"

SII_NS = "http://www.sii.cl/SiiDte"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

OUTPUT_DIR = COMPANY_DIR / "output"


def cargar_firma():
    """Carga la firma electrónica usando la librería facturacion_electronica."""
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
    return firma


def limpiar_namespaces(xml_str: str) -> str:
    """Elimina xmlns:xsi y xsi:schemaLocation heredados del sobre."""
    xml_str = re.sub(r'\s+xmlns:xsi="[^"]*"', '', xml_str)
    xml_str = re.sub(r'\s+xsi:schemaLocation="[^"]*"', '', xml_str)
    return xml_str


def firmar_sobre(xml_path: Path, firma: Firma) -> Path:
    """Firma un sobre EnvioDTE o EnvioBOLETA completo."""
    tree = etree.parse(str(xml_path))
    root = tree.getroot()

    root_tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    es_boleta = root_tag == "EnvioBOLETA"

    # Encontrar SetDTE
    set_dte = None
    for child in root:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == "SetDTE":
            set_dte = child
            break
    if set_dte is None:
        raise ValueError(f"No se encontró SetDTE en {xml_path}")

    # Encontrar Caratula
    caratula = None
    for child in set_dte:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == "Caratula":
            caratula = child
            break

    # Extraer DTEs
    dtes = []
    for child in list(set_dte):
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == "DTE":
            dtes.append(child)

    print(f"  Encontrados {len(dtes)} DTEs")

    # === PASO 1: Firmar cada DTE individual ===
    signed_dtes = []
    for dte_el in dtes:
        doc_el = None
        for child in dte_el:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag in ("Documento", "Liquidacion", "Exportaciones"):
                doc_el = child
                break

        if doc_el is None:
            print("    WARN: DTE sin Documento, saltando")
            continue

        doc_id = doc_el.get("ID", "")

        doc_str = etree.tostring(
            doc_el, encoding="ISO-8859-1", xml_declaration=False
        ).decode("ISO-8859-1")
        doc_str = limpiar_namespaces(doc_str)
        doc_str = re.sub(r'\s+xmlns=""', '', doc_str)
        doc_str = re.sub(r'\s+xmlns="http://www\.sii\.cl/SiiDte"', '', doc_str)

        dte_str = f'<DTE xmlns="{SII_NS}" version="1.0">{doc_str}</DTE>'

        tipo_firma = "bol" if es_boleta else "doc"
        signed_dte = firma.firmar(dte_str, doc_id, type=tipo_firma)

        if not signed_dte:
            raise RuntimeError(f"Error firmando DTE {doc_id}")

        signed_dtes.append(signed_dte)
        print(f"    DTE {doc_id} firmado OK")

    # === PASO 2: Reconstruir y firmar el sobre ===
    car_str = etree.tostring(
        caratula, encoding="ISO-8859-1", xml_declaration=False
    ).decode("ISO-8859-1")
    car_str = limpiar_namespaces(car_str)
    car_str = re.sub(r'\s+xmlns="[^"]*"', '', car_str)
    car_str = re.sub(r'\s+xmlns=""', '', car_str)

    if es_boleta:
        schema_loc = f"{SII_NS} EnvioBOLETA_v11.xsd"
        env_open = (
            f'<EnvioBOLETA xmlns="{SII_NS}" '
            f'xmlns:xsi="{XSI_NS}" '
            f'xsi:schemaLocation="{schema_loc}" '
            f'version="1.0">'
        )
        env_close = "</EnvioBOLETA>"
    else:
        schema_loc = f"{SII_NS} EnvioDTE_v10.xsd"
        env_open = (
            f'<EnvioDTE xmlns="{SII_NS}" '
            f'xmlns:xsi="{XSI_NS}" '
            f'xsi:schemaLocation="{schema_loc}" '
            f'version="1.0">'
        )
        env_close = "</EnvioDTE>"

    all_signed_dtes = "\n".join(signed_dtes)
    env_str = (
        f'{env_open}'
        f'<SetDTE ID="SetDoc">'
        f'{car_str}'
        f'{all_signed_dtes}'
        f'</SetDTE>'
        f'{env_close}'
    )

    tipo_sobre = "libro_boleta" if es_boleta else "env"
    signed_env = firma.firmar(env_str, "SetDoc", type=tipo_sobre)

    if not signed_env:
        raise RuntimeError(f"Error firmando sobre {xml_path.name}")

    final_xml = '<?xml version="1.0" encoding="ISO-8859-1"?>\n' + signed_env

    output_path = xml_path.parent / f"{xml_path.stem}_firmado{xml_path.suffix}"
    with open(output_path, "w", encoding="ISO-8859-1") as f:
        f.write(final_xml)

    print(f"  Sobre firmado: {output_path.name}")
    return output_path


def main():
    print("Cargando certificado digital...")
    firma = cargar_firma()
    print(f"  RUT firmante: {firma.rut_firmante}")
    print(f"  Cert OK\n")

    # SET GUÍA (4758675) ya aprobado SOK — NO re-firmar para preservar XML firmado válido
    sobres = sorted([
        f for f in OUTPUT_DIR.glob("Envio*.xml")
        if "_firmado" not in f.stem and "Guias" not in f.stem
    ])
    print(f"Firmando {len(sobres)} sobres...")
    for sobre in sobres:
        print(f"\n{'='*50}")
        print(f"Procesando: {sobre.name}")
        print(f"{'='*50}")
        firmar_sobre(sobre, firma)

    print(f"\n{'='*50}")
    print("Todos los sobres firmados exitosamente.")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()

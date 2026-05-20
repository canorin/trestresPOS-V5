"""Valida XMLs firmados contra los XSD del SII.

Rutas configurables por variables de entorno (con fallback al repo):
  · CRUMBPOS_SCHEMA_DIR → carpeta con los XSD del SII (default: schemas/).
  · CRUMBPOS_OUTPUT_DIR → carpeta con los XML firmados (default: output/).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from lxml import etree

from crumbpos.config import settings

SCHEMA_DIR = Path(os.getenv("CRUMBPOS_SCHEMA_DIR", str(settings.BASE_DIR / "schemas")))
OUTPUT_DIR = Path(os.getenv("CRUMBPOS_OUTPUT_DIR", str(settings.OUTPUT_DIR)))

SII_NS = "http://www.sii.cl/SiiDte"
DSIG_NS = "http://www.w3.org/2000/09/xmldsig#"


def validate_envio(xml_path: Path, xsd_name: str):
    """Valida un XML de envío contra su XSD."""
    print(f"\n{'='*60}")
    print(f"Validando: {xml_path.name}")
    print(f"{'='*60}")

    # Parse XML
    tree = etree.parse(str(xml_path))
    root = tree.getroot()

    # Parse XSD
    xsd_path = SCHEMA_DIR / xsd_name
    if not xsd_path.exists():
        print(f"  ⚠️ XSD no encontrado: {xsd_path}")
        return

    try:
        xsd_tree = etree.parse(str(xsd_path))
        schema = etree.XMLSchema(xsd_tree)
    except Exception as e:
        print(f"  ⚠️ Error cargando XSD: {e}")
        # Try without schema, just analyze structure
        schema = None

    if schema:
        is_valid = schema.validate(tree)
        if is_valid:
            print(f"  ✅ XML válido contra {xsd_name}")
        else:
            print(f"  ❌ XML NO válido contra {xsd_name}")
            for error in schema.error_log:
                print(f"     Línea {error.line}: {error.message}")
    else:
        print("  (Sin validación XSD, analizando estructura...)")

    # Analizar estructura del XML
    ns = {"sii": SII_NS, "ds": DSIG_NS}

    # Check Caratula
    set_dte = root.find(f"{{{SII_NS}}}SetDTE")
    if set_dte is not None:
        caratula = set_dte.find(f"{{{SII_NS}}}Caratula")
        if caratula is not None:
            print(f"\n  Caratula:")
            for child in caratula:
                tag = child.tag.split('}')[-1]
                print(f"    {tag}: {child.text}")

    # Check each DTE
    dtes = root.findall(f".//{{{SII_NS}}}DTE")
    for i, dte in enumerate(dtes):
        doc = None
        sig = None
        for child in dte:
            tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
            if tag == "Documento":
                doc = child
            elif tag == "Signature":
                sig = child

        if doc is None:
            continue

        doc_id = doc.get("ID", "?")
        enc = doc.find(f"{{{SII_NS}}}Encabezado")
        if enc is None:
            enc = doc.find("Encabezado")

        print(f"\n  DTE {i}: {doc_id}")

        if enc is not None:
            id_doc = enc.find(f"{{{SII_NS}}}IdDoc") or enc.find("IdDoc")
            emisor = enc.find(f"{{{SII_NS}}}Emisor") or enc.find("Emisor")
            receptor = enc.find(f"{{{SII_NS}}}Receptor") or enc.find("Receptor")
            totales = enc.find(f"{{{SII_NS}}}Totales") or enc.find("Totales")

            if id_doc is not None:
                tipo = (id_doc.find(f"{{{SII_NS}}}TipoDTE") or id_doc.find("TipoDTE"))
                folio = (id_doc.find(f"{{{SII_NS}}}Folio") or id_doc.find("Folio"))
                print(f"    TipoDTE: {tipo.text if tipo is not None else '?'}")
                print(f"    Folio: {folio.text if folio is not None else '?'}")

            if totales is not None:
                for t in totales:
                    tag = t.tag.split('}')[-1] if '}' in t.tag else t.tag
                    print(f"    {tag}: {t.text}")

            if emisor is not None:
                rut = emisor.find(f"{{{SII_NS}}}RUTEmisor") or emisor.find("RUTEmisor")
                print(f"    RUTEmisor: {rut.text if rut is not None else '?'}")

            if receptor is not None:
                rut = receptor.find(f"{{{SII_NS}}}RUTRecep") or receptor.find("RUTRecep")
                print(f"    RUTRecep: {rut.text if rut is not None else '?'}")

        # Check References
        refs = doc.findall(f".//{{{SII_NS}}}Referencia") or doc.findall(".//Referencia")
        for ref in refs:
            nro = ref.find(f"{{{SII_NS}}}NroLinRef") or ref.find("NroLinRef")
            tipo_ref = ref.find(f"{{{SII_NS}}}TpoDocRef") or ref.find("TpoDocRef")
            folio_ref = ref.find(f"{{{SII_NS}}}FolioRef") or ref.find("FolioRef")
            razon = ref.find(f"{{{SII_NS}}}RazonRef") or ref.find("RazonRef")
            cod_ref = ref.find(f"{{{SII_NS}}}CodRef") or ref.find("CodRef")
            print(f"    Ref {nro.text if nro is not None else '?'}: "
                  f"TpoDoc={tipo_ref.text if tipo_ref is not None else '?'} "
                  f"Folio={folio_ref.text if folio_ref is not None else '?'} "
                  f"CodRef={cod_ref.text if cod_ref is not None else 'N/A'} "
                  f"Razon={razon.text if razon is not None else '?'}")

        # Check TED
        ted = doc.find(f".//{{{SII_NS}}}TED") or doc.find(".//TED")
        print(f"    TED: {'presente' if ted is not None else '❌ FALTA'}")

        # Check TmstFirma
        tmst = doc.find(f"{{{SII_NS}}}TmstFirma") or doc.find("TmstFirma")
        print(f"    TmstFirma: {tmst.text if tmst is not None else '❌ FALTA'}")

        # Check Signature
        print(f"    Signature: {'presente' if sig is not None else '❌ FALTA'}")

        # Check if DTE has version attribute
        print(f"    DTE version: {dte.get('version', '❌ FALTA')}")


if __name__ == "__main__":
    # Validate each signed XML
    files = {
        "EnvioDTE_SetBasico_firmado.xml": "EnvioDTE_v10.xsd",
        "EnvioDTE_SetExentas_firmado.xml": "EnvioDTE_v10.xsd",
        "EnvioDTE_SetGuias_firmado.xml": "EnvioDTE_v10.xsd",
        "EnvioBOLETA_Set_firmado.xml": "EnvioBOLETA_v11.xsd",
    }

    for xml_name, xsd_name in files.items():
        xml_path = OUTPUT_DIR / xml_name
        if xml_path.exists():
            validate_envio(xml_path, xsd_name)
